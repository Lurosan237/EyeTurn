"""Head control: tip your head down or up to scroll, turn it to turn a page.

Uses the head direction the eye tracker already measures (MediaPipe's face
transformation matrix), so it needs no extra model and no calibration. When
head control is switched on, EyeTurn first learns how you normally hold your
head (hold still for about a second). From then on:

- Tip your head down past the dead zone to scroll down, up to scroll up. The
  further you tip it, the faster it scrolls. Back to normal = stop.
- Turn your head right and hold it there for a moment = next page, left =
  previous page (one full page, like a glance or a hand swipe). Look back
  ahead before the next turn.

Small movements while reading stay inside the dead zone, and the normal
position slowly follows you while you're inside it, so leaning a little
differently over time doesn't start scrolling. F7 learns it again.
"""
import math
import os

import numpy as np

HEAD_X, HEAD_Y = 3, 4  # head direction (left-right, up-down) in the tracker's features


class HeadScroller:
    def __init__(self, cfg, sender, turner, ignore_apps):
        self.cfg = cfg
        self.sender = sender
        self.turner = turner  # PageTurner: head turns use its one-page glide
        self.ignore_apps = ignore_apps
        self.foreground_exe = None
        if os.name == "nt":
            from . import winput

            self.foreground_exe = winput.foreground_exe
        self.neutral = None
        self.reset()

    def reset(self):
        """Stops scrolling. The learned normal head position is kept."""
        self.speed = 0.0
        self.last_t = None
        self.pose = None  # smoothed (head_x, head_y)
        self.learning = []  # samples while learning the normal position
        self.learn_t = None  # when learning started
        self.turn_side = None
        self.turn_t = 0.0
        self.turn_armed = True
        self.exe_t = -1e9
        self.exe_ok = True

    def recenter(self):
        """Learn the normal head position again (F7, or switching head control on)."""
        self.neutral = None
        self.reset()

    def _allowed(self, now):
        if now - self.exe_t > 0.5:  # asking Windows is cheap, but not every frame
            self.exe_t = now
            exe = self.foreground_exe() if self.foreground_exe else ""
            self.exe_ok = not (exe and exe in self.ignore_apps)
        return self.exe_ok

    def offsets(self):
        """(turn, tip) away from the normal position; right and down are positive."""
        if self.pose is None or self.neutral is None:
            return None
        d = self.pose - self.neutral
        return d[0] * self.cfg["turn_sign"], d[1] * self.cfg["tilt_sign"]

    def update(self, feats, now):
        """feats: the tracker's latest features, or None when no face is seen.
        Returns {"state": text for the panel, "progress": 0..1, "event": turn or None}."""
        c = self.cfg
        dt = 0.0 if self.last_t is None else min(now - self.last_t, 0.1)
        self.last_t = now
        if feats is None or not np.isfinite(feats[[HEAD_X, HEAD_Y]]).all():
            self.speed = 0.0
            self.pose = None
            self.turn_side = None
            return {"state": None, "progress": 0.0, "event": None}

        raw = np.array([feats[HEAD_X], feats[HEAD_Y]], dtype=float)
        if self.pose is None or dt == 0.0:
            self.pose = raw
        else:
            self.pose += (raw - self.pose) * (1 - math.exp(-dt / c["smooth_s"]))

        if self.neutral is None:
            if self.learn_t is None:
                self.learn_t = now
            self.learning.append(raw)
            elapsed = now - self.learn_t
            if elapsed < c["learn_s"] or len(self.learning) < 5:
                return {"state": "Head: hold still, learning your normal position",
                        "progress": min(elapsed / c["learn_s"], 1.0), "event": None}
            self.learn_t = None
            self.neutral = np.median(np.array(self.learning), axis=0)
            self.learning = []
            print(f"head: normal position x={self.neutral[0]:.3f} y={self.neutral[1]:.3f}", flush=True)

        turn, tip = self.offsets()
        event = None

        # Turning: hold the head turned past turn_at for turn_hold_ms.
        side = "next" if turn > c["turn_at"] else "prev" if turn < -c["turn_at"] else None
        if abs(turn) < c["turn_rearm"]:
            self.turn_armed = True
        if side != self.turn_side:
            self.turn_side, self.turn_t = side, now
        if (c["head_turn"] and side and self.turn_armed
                and now - self.turn_t >= c["turn_hold_ms"] / 1000):
            self.turn_armed = False
            if self.turner.swipe_turn(side, now):
                event = f"{side} (head turn)"
                print(f"head turn: {side} (turn {turn:+.3f})", flush=True)

        # Scrolling: tip past the dead zone; not while the head is turned or a page glides.
        target = 0.0
        busy = self.turner.probe or self.turner.glide
        if abs(turn) < c["turn_rearm"] and not busy and c["head_scroll"]:
            off = abs(tip) - c["dead_zone"]
            if off > 0:
                f = min(off / (c["full_tilt"] - c["dead_zone"]), 1.0) ** c["exponent"]
                target = c["max_speed"] * f if tip > 0 else -c["up_max_speed"] * f
        if target == 0.0 or target * self.speed < 0:
            self.speed = 0.0  # stops at once when you look back up
        else:
            step = c["accel"] * dt
            self.speed += max(-step, min(target - self.speed, step))
        if self.speed and self._allowed(now):
            # Not anchored: with the pointer over the document the wheel goes straight
            # there and the pointer never moves (anchored, it was moved to the window's
            # middle and back for every step, many times a second).
            self.sender.scroll(self.speed * dt)

        # Inside the dead zone, the normal position slowly follows how you sit.
        if abs(tip) < c["dead_zone"] * 0.7 and abs(turn) < c["turn_rearm"] * 0.7 and dt:
            self.neutral += (self.pose - self.neutral) * (dt / c["adapt_s"])

        if self.speed:
            word = "down" if self.speed > 0 else "up"
            state = f"Head: scrolling {word}"
            progress = abs(self.speed) / c["max_speed"]
        elif side:
            state = f"Head: turned {'right' if side == 'next' else 'left'}"
            progress = min((now - self.turn_t) / (c["turn_hold_ms"] / 1000), 1.0) if self.turn_armed else 1.0
        else:
            state = "Head: tip to scroll, turn to change page"
            progress = 0.0
        return {"state": state, "progress": progress, "event": event}
