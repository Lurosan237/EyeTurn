"""Hand gestures in front of the webcam.

Air scroll, like scrolling on a phone: flick an open hand up and the text
follows it up (scrolls down), flick it down and the text comes back; a quick
flick keeps gliding for a moment. Bringing your hand back after a flick doesn't
scroll back. Swipe sideways to turn: right to left = next screen, left to right
= previous.

The hand model runs in its own thread on the newest camera frame, so a slow
hand model never slows down eye tracking: it just skips frames.
"""
import queue
import threading
import time
import traceback

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from .config import resolve_path
from .log import log

WRIST = 0
PALM = [0, 5, 9, 13, 17]
TIPS = [8, 12, 16, 20]  # index, middle, ring and little finger tips...
PIPS = [6, 10, 14, 18]  # ...and the middle joints of the same fingers


def hand_pose(landmarks):
    """(x, y, fingers) of a hand as you see it yourself (the camera image is mirrored).

    x and y are in camera-frame widths / heights; fingers is how many of the
    four fingers are stretched out (tip further from the wrist than the joint).
    """
    pts = np.array([(p.x, p.y) for p in landmarks])
    x, y = pts[PALM].mean(axis=0)
    reach = np.linalg.norm(pts[TIPS] - pts[WRIST], axis=1)
    joint = np.linalg.norm(pts[PIPS] - pts[WRIST], axis=1)
    return 1.0 - x, y, int((reach > 1.1 * joint).sum())


class SwipeDetector:
    """Turns hand positions over time into sideways "next" / "prev" swipes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.history = []  # (time, x, y, open) of the last swipe_ms
        self.cooldown_until = 0.0
        self.last = None  # (direction, time) of the last swipe

    def update(self, t, pose):
        c = self.cfg
        if pose is None:
            self.history.clear()  # a hand that disappears and comes back elsewhere isn't a swipe
            return None
        x, y, fingers = pose
        self.history.append((t, x, y, fingers >= c["open_fingers"]))
        self.history = [h for h in self.history if t - h[0] <= c["swipe_ms"] / 1000]
        if t < self.cooldown_until or len(self.history) < 3:
            return None
        if c["require_open_hand"] and sum(h[3] for h in self.history) * 2 < len(self.history):
            return None
        xs = np.array([h[1] for h in self.history])
        ys = np.array([h[2] for h in self.history])
        # Travel from the furthest point on either side to where the hand is now.
        for direction, i in (("next", int(xs.argmax())), ("prev", int(xs.argmin()))):
            dx = abs(xs[i] - x)
            dy = np.abs(ys[i:] - ys[i]).max()
            if dx < c["swipe_min_dx"] or dy > c["swipe_max_dy"] * dx:
                continue
            if self.last and self.last[0] != direction and t - self.last[1] < c["return_block_ms"] / 1000:
                continue  # your hand going back after a swipe
            self.last = (direction, t)
            self.cooldown_until = t + c["cooldown_ms"] / 1000
            self.history.clear()
            if c["swap_directions"]:
                return "prev" if direction == "next" else "next"
            return direction
        return None


class FlickDetector:
    """Turns up / down hand movement into phone-like drags and flicks.

    A stroke starts when an open hand moves up or down faster than start_speed
    (camera-frame heights per second) and mostly vertically. While it lasts the
    text follows the hand ("drag", in wheel units); when the hand slows down,
    turns, closes or leaves, the stroke ends with a "fling" at the hand's last
    speed. Moving the other way is then your hand going back, and doesn't
    scroll until it has stayed out of the way for reverse_pause_ms: to scroll
    back, pause a moment, then flick the other way.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.prev = None  # (time, x, y) of the last open-hand position
        self.stroke = 0  # +1 hand moving down, -1 up, 0 none
        self.speeds = []  # (time, vertical speed) during the stroke
        self.lock = (0, 0.0)  # (blocked direction, until)

    def _end(self, t):
        c = self.cfg
        recent = [v for s, v in self.speeds if t - s <= 0.2] or [0.0]
        v = max(recent, key=abs) if self.stroke * max(recent, key=abs) > 0 else 0.0
        self.lock = (-self.stroke, t + c["reverse_pause_ms"] / 1000)
        self.stroke, self.speeds = 0, []
        return ("fling", -v * c["scroll_gain"] * c["fling_gain"]) if v else None

    def update(self, t, pose):
        c = self.cfg
        if pose is None or pose[2] < c["open_fingers"]:
            self.prev = None
            return [self._end(t)] if self.stroke else []
        x, y, _ = pose
        prev, self.prev = self.prev, (t, x, y)
        if prev is None or t - prev[0] > 0.3:
            return [self._end(t)] if self.stroke else []
        dt = t - prev[0]
        if dt <= 0:
            return []
        dy = y - prev[2]
        vy, vx = dy / dt, (x - prev[1]) / dt
        direction = 1 if vy > 0 else -1
        events = []
        if self.stroke and (direction != self.stroke or abs(vy) < c["stop_speed"]):
            events.append(self._end(t))
        if not self.stroke:
            blocked = self.lock[0] == direction and t < self.lock[1]
            if blocked and abs(vy) >= c["stop_speed"]:
                # Still bringing the hand back, however long that takes.
                self.lock = (direction, max(self.lock[1], t + c["reverse_pause_ms"] / 2000))
            if abs(vy) < c["start_speed"] or abs(vy) < 1.5 * abs(vx) or blocked:
                return [e for e in events if e]
            self.stroke = direction
        self.speeds.append((t, vy))
        # The hand going up (dy < 0) pushes the text up: scroll down (positive).
        events.append(("drag", -dy * c["scroll_gain"]))
        return [e for e in events if e]


class HandGestures(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.path = resolve_path(cfg["model_path"])
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found (see README)")
        self.swipe = SwipeDetector(cfg)
        self.flick = FlickDetector(cfg)
        self.error = None
        self.fps = 0.0
        self._frame = None
        self._new = threading.Event()
        self._quit = threading.Event()
        self._events = queue.Queue()

    def submit(self, frame_bgr, t):
        """Hands over the newest camera frame (older unprocessed ones are dropped)."""
        self._frame = (frame_bgr, t)
        self._new.set()

    def events(self):
        """("turn", "next"/"prev"), ("drag", wheel units) and ("fling", wheel units/s) since the last call."""
        out = []
        while not self._events.empty():
            out.append(self._events.get())
        return out

    def stop(self):
        self._quit.set()
        self._new.set()

    def run(self):
        """Restarts the hand model after a crash, so hand gestures come back by themselves."""
        delay = 2.0
        while not self._quit.is_set():
            try:
                self._loop()
                return
            except Exception as e:  # shown in the status window; eye tracking keeps going
                self.error = f"{type(e).__name__}: {e}"
                log(f"Hand gestures crashed, restarting in {delay:.0f} s: {self.error}\n{traceback.format_exc()}")
                self._quit.wait(delay)
                delay = min(delay * 2, 30.0)
                self.error = None

    def _on_pose(self, t, pose):
        c = self.cfg
        turn = self.swipe.update(t, pose) if c["swipe_turn"] else None
        if turn:
            print(f"hand swipe: {turn} (hand at x={pose[0]:.2f})", flush=True)
            self._events.put(("turn", turn))
            self.flick = FlickDetector(c)  # a sideways swipe isn't a scroll
            return
        if c["air_scroll"]:
            for event in self.flick.update(t, pose):
                if event[0] == "fling":
                    print(f"hand flick: {event[1]:.0f} wheel units/s", flush=True)
                self._events.put(event)

    def _loop(self):
        c = self.cfg
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
        )
        t0 = time.monotonic()
        last_ts = -1
        frames, fps_t = 0, t0
        prev_small, hand_seen, checked_t = None, False, -1e9
        motions, log_t, pose_log_t = [], t0, t0
        with vision.HandLandmarker.create_from_options(options) as landmarker:
            while not self._quit.is_set():
                # Leave CPU time for eye tracking: at most max_fps hand frames a second.
                wait = 1 / c["max_fps"] - (time.monotonic() - last_ts / 1000 - t0)
                if wait > 0:
                    time.sleep(wait)
                self._new.wait()
                self._new.clear()
                if self._quit.is_set() or self._frame is None:
                    break
                frame, t = self._frame
                # The hand model is costly, so with no hand in view it only runs when
                # something moves (a hand coming in), or twice a second.
                small = cv2.cvtColor(cv2.resize(frame, (80, 45), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
                motion = 0.0 if prev_small is None else float(cv2.absdiff(small, prev_small).mean())
                prev_small = small
                motions.append(motion)
                if not hand_seen and motion < c["motion_gate"] and t - checked_t < 0.5:
                    continue
                checked_t = t
                h, w = frame.shape[:2]
                width = c["detect_width"]
                if w > width:
                    frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ts = max(int((t - t0) * 1000), last_ts + 1)
                last_ts = ts
                result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
                pose = hand_pose(result.hand_landmarks[0]) if result.hand_landmarks else None
                hand_seen = pose is not None
                if c["debug"] and pose is not None and t - pose_log_t >= 0.2:
                    pose_log_t = t
                    print(f"hand x={pose[0]:.2f} y={pose[1]:.2f} fingers={pose[2]}", flush=True)
                self._on_pose(t, pose)
                frames += 1
                if t - fps_t >= 1.0:
                    self.fps = frames / (t - fps_t)
                    frames, fps_t = 0, t
                if t - log_t >= 30.0 and motions:
                    p = np.percentile(motions, [50, 90, 99])
                    print(f"hands: camera motion median {p[0]:.1f}, 90% {p[1]:.1f}, 99% {p[2]:.1f}", flush=True)
                    motions, log_t = [], t
