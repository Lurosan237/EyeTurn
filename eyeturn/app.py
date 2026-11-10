"""EyeTurn: hands-free page turning and auto-scrolling with a webcam."""
import argparse
import os
import queue
import sys
import time
import tkinter as tk
import traceback

import numpy as np
from pynput import keyboard, mouse

from .actions import AirScroller, AutoScroller, EyeBoth, InputSender, PageTurner
from .calibration import CalibrationScreen
from . import log as logging_setup
from .config import INPUTS, load_config, resolve_path
from .filters import BlinkDetector, GazeSmoother
from .gaze_model import HEAD_GROUP, GazeModel, write_json
from .head import HeadScroller
from .log import log
from .posture import ClickValidator, PostureMonitor, PostureStore, pose_of
from .touchup import TouchUp
from .reading import ReadingDetector
from .tracker import OPENNESS, Tracker
from .ui import GazeDot, StatusPanel

MODES = ("both", "page", "scroll")  # Mode button / F10 goes through them in this order
TOUCHUP_GAP_S = 300  # at least this long between two automatic touch-ups


def _single_instance():
    """True if no other EyeTurn is running. Otherwise brings that one to the front.

    Two copies would fight over the camera and both send scrolls.
    """
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.windll.user32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    # Kept in a global so the mutex lives as long as this process.
    global _MUTEX
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\EyeTurnSingleInstance")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    hwnd = user32.FindWindowW(None, "EyeTurn")
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return False


def _set_dpi_aware():
    # Without this, Windows scaling makes Tk and the cursor disagree about pixels.
    if os.name == "nt":
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class App:
    def __init__(self, root, cfg, recalibrate=False):
        self.root = root
        self.cfg = cfg
        self.screen = (root.winfo_screenwidth(), root.winfo_screenheight())
        self.mode = cfg["mode"] if cfg["mode"] in MODES else "both"
        self.input = cfg["input"]  # what controls reading: "eyes", "hands" or "head", one at a time
        self.paused = False
        self.closed = False
        self.after_id = None
        self.calibration = None
        self.touchup = None
        self.posture = None  # PostureMonitor, once there's a calibration
        self.clicks_ok = None
        self.touchup_asked_t = -1e9  # last time a touch-up was shown or skipped
        self.touchup_misses = 0  # touch-ups in a row that couldn't be used
        self.commands = queue.Queue()

        self.tracker = Tracker(cfg)
        if self.input == "hands" and self.tracker.hands is None:
            self.input = "eyes"  # no hand model
        self._apply_input()
        self.tracker.start()

        sender = InputSender(cfg["scroll"]["min_wheel_step"], cfg["scroll"]["follow_focus"])
        turner = PageTurner(cfg["page"], sender)
        scroller = AutoScroller(cfg["scroll"], sender, self.screen)
        self.actions = {"page": turner, "scroll": scroller, "both": EyeBoth(turner, scroller, cfg["scroll"])}
        turner.with_scroll = self.mode == "both"
        self.air = AirScroller(cfg["hands"], sender, cfg["page"]["ignore_apps"])
        self.head = HeadScroller(cfg["head"], sender, turner, cfg["page"]["ignore_apps"])

        self.smoother = GazeSmoother(**cfg["filter"], screen=self.screen)
        self.blinks = BlinkDetector(cfg["blink_ratio"])
        self.reading = ReadingDetector(cfg["reading"])
        self.event = None  # reading event from the latest frame, handed to the action once
        self.offset = np.zeros(2)
        self.gaze = None  # filtered, offset-corrected, normalized screen coords
        self.raw_gaze = None  # latest unfiltered model output, before the posture correction
        self.last_feats = None  # (time, features) of the latest frame with open eyes
        self.unsaved_clicks = 0
        self.flash = ("", 0.0)  # (status message, show until)
        self.last_seq = None
        self.last_valid_t = 0.0
        self.head_feats = None  # latest features for head control

        self.calib_path = resolve_path(cfg["calibration_file"])
        self.model = None
        if not recalibrate and self.calib_path.exists():
            try:
                model = GazeModel.load(self.calib_path, net_available=self.tracker.net is not None)
            except Exception as e:  # a damaged or outdated file: calibrate again instead
                log(f"Can't use the saved calibration ({type(e).__name__}: {e})")
                model = None
            if model is not None and model.screen == self.screen:
                self.model = model
                self._start_posture()

        hk = cfg["hotkeys"]
        self.hotkeys = keyboard.GlobalHotKeys(
            {combo: (lambda name=name: self.commands.put(name)) for name, combo in hk.items()}
        )
        self.hotkeys.start()
        self.clicks = mouse.Listener(
            on_click=lambda x, y, button, pressed: pressed and self.commands.put(("click", x, y, time.monotonic()))
        )
        if cfg["learn_from_clicks"]:
            self.clicks.start()

        self.panel = StatusPanel(root, hk, self.commands, after_click=self._after_panel_click)
        self._show_toggles()
        self.dot = GazeDot(root) if cfg["show_gaze"] else None
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.report_callback_exception = self._callback_error
        self.errors = 0

        if self.model is None and self.input == "eyes":
            root.after(800, self.start_calibration)
        self.tick()

    # --- commands ------------------------------------------------------------
    def _handle(self, cmd):
        if isinstance(cmd, tuple) and cmd[0] == "click":
            self.learn_from_click(*cmd[1:])
        elif cmd == "pause":
            self.paused = not self.paused
            self._reset_actions()
        elif cmd == "mode":
            self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.actions["page"].with_scroll = self.mode == "both"
            self._reset_actions()
            self._save_switches()
            if self.input != "eyes":
                self.flash = ("Eye mode changed (switch Eyes on to use it)", time.monotonic() + 3.0)
        elif isinstance(cmd, str) and cmd.startswith("input_") and cmd[6:] in INPUTS:
            self.set_input(cmd[6:])
        elif cmd == "recalibrate":
            self.start_calibration()
        elif cmd == "touchup":
            if self.input == "head":
                self.recenter_head()
            else:
                self.start_touchup()
        elif cmd == "recenter":
            if self.input == "head":
                self.recenter_head()
            else:
                self.recenter()
        elif cmd == "quit":
            self.quit()

    def _after_panel_click(self, cmd):
        """Hands the keyboard focus back to what you were reading, so the next turn or
        scroll goes there and not to EyeTurn's panel. Not for calibration / touch-up,
        which need the keyboard (Space, Esc)."""
        needs_keys = cmd == "recalibrate" or (cmd == "touchup" and self.input != "head")
        if os.name == "nt" and not needs_keys and self.calibration is None:
            from . import winput

            winput.give_back_focus()

    def _apply_input(self):
        """Runs only the tracking the chosen input needs: the face tracker for eyes
        and head (head direction comes from it), the hand tracker for hands."""
        self.tracker.eyes_on = self.input in ("eyes", "head")
        self.tracker.hands_on = self.input == "hands" and self.tracker.hands is not None

    def set_input(self, name):
        """Eyes, hands or head: switching one on switches the other two off."""
        if name == "hands" and self.tracker.hands is None:
            self.flash = ("Hands unavailable: the hand model is missing", time.monotonic() + 3.0)
            return
        if name == self.input:
            if name == "head":
                self.recenter_head()  # clicking Head again learns your normal position again
            return
        self.input = name
        self._apply_input()
        if self.tracker.hands is not None:
            self.tracker.hands.events()  # nothing left over from before
        self.gaze = None
        self.smoother.reset()
        self._reset_actions()
        if name == "head":
            self.recenter_head()
        log(f"Input: {name}")
        self._save_switches()

    def recenter_head(self):
        self.head.recenter()
        self.flash = ("Head: hold still, learning your normal position", time.monotonic() + 1.5)

    def _show_toggles(self):
        self.panel.show_toggles(self.input, hands_available=self.tracker.hands is not None)

    def _save_switches(self):
        """Remembers the input (eyes / hands / head) and eye mode in config.json, for the next start."""
        self._show_toggles()
        path = resolve_path("config.json")
        try:
            import json

            user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            user["input"] = self.input
            user["mode"] = self.mode
            # Older EyeTurn versions read these two.
            user["eyes_enabled"] = self.input == "eyes"
            user.setdefault("hands", {})["enabled"] = self.input == "hands"
            write_json(path, user, indent=2)
        except (OSError, ValueError) as e:
            log(f"Couldn't save the input choice: {e}")

    def _reset_actions(self):
        for action in self.actions.values():
            action.reset()
        self.air.reset()
        self.head.reset()
        self.reading.reset()

    def start_calibration(self):
        if self.calibration is not None or self.tracker.error:
            return
        if self.touchup is not None:
            self.touchup.cancel()
        if self.input != "eyes":  # calibrating needs the eyes
            self.set_input("eyes")
        self._reset_actions()
        first_run = self.model is None and not self.calib_path.exists()
        self.calibration = CalibrationScreen(
            self.root, self.tracker, self.cfg, self.screen, self._calibration_done, first_run
        )

    def _calibration_done(self, model):
        self.calibration = None
        if self.tracker.hands:
            self.tracker.hands.events()  # waving during calibration shouldn't act now
        if model is not None:
            first = not self.calib_path.exists()
            self.model = model
            self.offset[:] = 0
            self._save_model(force=True)
            self._start_posture()
            if first:
                self.flash = ("Ready! Glance past the right edge to turn a page", time.monotonic() + 12.0)
        self.smoother.reset()

    # --- staying calibrated when you move --------------------------------------
    def _start_posture(self):
        """Per-posture corrections on top of the calibration (see posture.py)."""
        import hashlib

        m = self.model
        still = np.array([g < HEAD_GROUP for g in m.groups]) if m.groups else np.ones(len(m.base_x), bool)
        home = np.median([pose_of(x) for x in m.base_x[still]], axis=0)
        calibration_id = hashlib.md5(m.base_x.tobytes()).hexdigest()[:12]
        store = PostureStore(resolve_path("postures.json"), calibration_id, home)
        self.posture = PostureMonitor(store)
        # How far a click may be from the gaze and still count: about twice the
        # calibration's typical error, as a share of the screen.
        err = (m.cv_error or 200) / max(self.screen)
        self.clicks_ok = ClickValidator(max_error=float(np.clip(2 * err, 0.15, 0.35)))

    def _learn(self, predicted, target, weight, why):
        """Teaches the current posture's correction one (predicted, true) point pair."""
        mon = self.posture
        mon.adopt_current()
        mon.current.add(predicted, target, weight)
        if len(mon.current.samples) >= 3:  # one or two clicks can't stand in for a touch-up yet
            mon.needs_touchup = False
        mon.store.save()
        c = mon.current
        log(f"Correction learned from {why}: shift x {c.shift[0]:+.3f} y {c.shift[1]:+.3f}, "
            f"scale x {c.scale[0]:.2f} y {c.scale[1]:.2f} ({len(c.samples)} points in this posture)")

    def start_touchup(self):
        """The 5-dot touch-up (F6, or by itself after you've moved)."""
        if (self.touchup is not None or self.calibration is not None or self.model is None
                or self.posture is None or self.input != "eyes" or self.tracker.status):
            return
        self._reset_actions()
        self.touchup_asked_t = time.monotonic()
        log("Touch-up: showing the dots")
        self.touchup = TouchUp(self.root, self.tracker, self.model, self.screen, self._touchup_done)

    def _touchup_done(self, pairs, skipped):
        self.touchup = None
        self.touchup_asked_t = time.monotonic()
        why = "skipped" if skipped else "couldn't see your eyes on enough dots"
        if pairs:
            P = np.array([p for p, _ in pairs])
            T = np.array([t for _, t in pairs])
            # If the gaze hardly moved between dots, you weren't following them.
            # (With your calibration, gaze spreads about 0.8x as far as the dots.)
            if (P.std(axis=0) < 0.35 * T.std(axis=0)).any():
                why = f"the gaze didn't follow the dots (spread {P.std(axis=0).round(3)})"
                pairs = None
        if pairs:
            self.touchup_misses = 0
            mon = self.posture
            mon.adopt_current()
            for predicted, target in pairs:
                mon.current.samples.append((predicted, target, 1.0))
            mon.current.updated = time.time()
            mon.current._fit()
            mon.needs_touchup = False
            mon.store.save()
            c = mon.current
            log(f"Touch-up done ({len(pairs)} dots): shift x {c.shift[0]:+.3f} y {c.shift[1]:+.3f}, "
                f"scale x {c.scale[0]:.2f} y {c.scale[1]:.2f}")
            self.flash = ("Touch-up done", time.monotonic() + 2.0)
        else:
            log(f"Touch-up not used: {why}")
            if skipped:
                self.posture.needs_touchup = False  # don't ask again until you move again
            else:
                # Probably not a good moment (looking away, or eyes hard to see):
                # wait longer before the next one, and give up after 3 in a row.
                self.touchup_misses += 1
                if self.touchup_misses >= 3:
                    self.posture.needs_touchup = False
                    self.touchup_misses = 0
        self.smoother.reset()
        self.reading.reset()

    def _maybe_touchup(self, now):
        """After you've moved somewhere new, offers the touch-up at a quiet moment:
        not while reading, not during a turn, and at most every 5 minutes (10,
        then 20, after one that couldn't be used)."""
        mon = self.posture
        if mon is None or not mon.needs_touchup or self.touchup is not None or self.paused:
            return
        if self.cfg["touchup"] != "auto" or now - self.touchup_asked_t < TOUCHUP_GAP_S * 2 ** self.touchup_misses:
            return
        last_sweep = self.reading.sweeps[-1][0] if self.reading.sweeps else -1e9
        turner = self.actions["page"]
        if (self.gaze is None or self.reading.is_reading(now) or now - last_sweep < 3.0
                or turner.probe or turner.glide or turner.hold or self.air.active):
            return
        self.start_touchup()

    def recenter(self):
        """Look at the mouse pointer and press the hotkey to cancel out drift."""
        if self.gaze is None or self.posture is None:
            return
        cx, cy = mouse.Controller().position
        cursor = np.array([cx / self.screen[0], cy / self.screen[1]])
        predicted = self.clicks_ok.gaze_for_click(time.monotonic() + 0.03)
        if predicted is None:
            predicted = self.raw_gaze
        if predicted is not None:
            self._learn(predicted, cursor, 2.0, "F7")

    def learn_from_click(self, x, y, t_click=None):
        """You look at what you click, so a click can be a free calibration point.

        Only when your gaze was steady just before the click and not far from it:
        eyes reach a target a moment before the click, and many clicks happen
        without looking (Huang et al., PACE, CHI 2016).
        """
        if (self.model is None or self.posture is None or self.calibration is not None
                or self.touchup is not None or self.input != "eyes"):
            return
        if self._own_window_at(x, y):
            return  # clicking EyeTurn's own buttons, often without looking at them
        target = np.array([x / self.screen[0], y / self.screen[1]])
        if not ((0 <= target) & (target <= 1)).all():
            return
        predicted = self.clicks_ok.gaze_for_click(t_click or time.monotonic())
        if predicted is None:
            return  # eyes moving or not seen just before the click
        if not self.clicks_ok.accept(predicted, target, self.posture.current):
            return  # looking somewhere else while clicking
        self._learn(predicted, target, 1.0, "a click")

    @staticmethod
    def _own_window_at(x, y):
        if os.name != "nt":
            return False
        try:
            from . import winput

            hwnd = winput._root_at(x, y)
            return bool(hwnd) and winput._is_own(hwnd)
        except Exception:
            return False

    def _save_model(self, force=False):
        if self.model is not None and (self.unsaved_clicks or force):
            try:
                self.model.save(self.calib_path)
                self.unsaved_clicks = 0
            except OSError as e:  # e.g. a full disk; keep running with the model in memory
                log(f"Couldn't save the calibration: {e}")

    def _callback_error(self, exc_type, exc_value, exc_tb):
        """A Tk callback (a button, a timer) failed: log it and carry on."""
        log("Error in a callback:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def quit(self):
        self._save_model()
        self.closed = True
        if self.after_id:
            self.root.after_cancel(self.after_id)
        self.tracker.stop()
        self.tracker.join(timeout=2)
        self.hotkeys.stop()
        self.clicks.stop()
        self.root.destroy()

    # --- main loop -----------------------------------------------------------
    def _update_gaze(self, now):
        seq, sample = self.tracker.latest()
        if seq == self.last_seq:
            # No new frame; keep the current estimate unless it has gone stale.
            if self.gaze is not None and now - self.last_valid_t > self.cfg["hold_ms"] / 1000:
                self.gaze = None
            return
        self.last_seq = seq
        feats = sample.features if sample else None
        blink = feats is not None and self.blinks(feats[OPENNESS], sample.t)
        if feats is None or blink:
            if now - self.last_valid_t > self.cfg["hold_ms"] / 1000:
                self.gaze = None
                self.smoother.reset()
            return
        base = self.model.predict(feats)
        if self.posture is not None and np.isfinite(base).all():
            moved = self.posture.update(feats, sample.t, base)
            if moved:
                log(f"Posture: {moved}")
            self.clicks_ok.add(sample.t, base)
            self.raw_gaze = base
            raw = self.posture.current.apply(base)
        else:
            raw = base + self.offset
        if self.cfg["debug_features"] and now - getattr(self, "_feat_log_t", 0.0) >= 1.0:
            self._feat_log_t = now
            names = ("eye_x", "eye_y", "open", "head_x", "head_y", "tx", "ty", "tz", "look_x", "look_y", "fine_x", "fine_y")
            print("feat " + " ".join(f"{n}={v:.3f}" for n, v in zip(names, feats)) + f" -> x={raw[0]:.2f} y={raw[1]:.2f}", flush=True)
        if not np.isfinite(raw).all():  # the model uses L2CS-Net and it has no recent answer
            if now - self.last_valid_t > self.cfg["hold_ms"] / 1000:
                self.gaze = None
                self.smoother.reset()
            return
        self.gaze = self.smoother(raw, sample.t)
        ev = self.reading.update(raw, sample.t)
        if ev:
            print(f"{time.strftime('%H:%M:%S')} {ev}: gaze x={raw[0]:.2f} y={raw[1]:.2f}, reading={self.reading.is_reading(now)}", flush=True)
        self.event = ev or self.event
        self.last_feats = (now, feats)
        self.last_valid_t = now

    def _speed(self, now):
        """Frames per second of each tracker, for the panel (and the log every 30 s)."""
        parts = []
        if self.tracker.eyes_on:
            parts.append(f"{self.tracker.fps:.0f} fps")
            if self.tracker.net_fps is not None:
                parts.append(f"L2CS {self.tracker.net_fps:.0f} fps")
        hands = self.tracker.hands
        if hands is not None and self.tracker.hands_on:
            parts.append(f"hands off: {hands.error}" if hands.error else f"hands {hands.fps:.0f} fps")
        fps = ", ".join(parts)
        if now - getattr(self, "_fps_log_t", 0.0) >= 30.0:
            self._fps_log_t = now
            print(f"{time.strftime('%H:%M:%S')} speed: {fps}", flush=True)
        return fps

    EYE_MODES = {"both": "turn + scroll", "page": "page turning", "scroll": "auto-scroll"}

    def _title(self):
        if self.input == "hands":
            return "Hands: air scroll"
        if self.input == "head":
            return "Head: tip and turn"
        return "Eyes: " + self.EYE_MODES[self.mode]

    def _head_control(self, now, fps):
        """Head control (see head.py). Returns (state, progress, warn) for the panel."""
        seq, sample = self.tracker.latest()
        if seq != self.last_seq:
            self.last_seq = seq
            self.head_feats = sample.features if sample else None
            self.last_valid_t = now
        elif now - self.last_valid_t > 0.5:  # no new frame for a while
            self.head_feats = None
        if self.paused:
            self.head.reset()
            return f"Paused  ({fps})", 0.0, False
        result = self.head.update(self.head_feats, now)
        if result["event"]:
            self.flash = (f"Turned page: {result['event']}", now + 2.0)
        if result["state"] is None:
            return self.tracker.hint or "Can't see your face - look at the screen", 0.0, True
        return f"{result['state']}  ·  {fps}", result["progress"], False

    def _hand_gestures(self, now, eyes_acted):
        hands = self.tracker.hands
        if hands is None:
            return
        turner = self.actions["page"]
        for kind, value in hands.events():
            if self.paused:
                continue  # dropped, so it doesn't act the moment you unpause
            if kind == "turn" and turner.swipe_turn(value, now):
                self.flash = (f"Turned page: {value} (hand swipe)", now + 2.0)
            elif kind == "drag":
                self.air.drag(value)
            elif kind == "fling":
                self.air.fling(value)
        self.air.update(now)
        if not eyes_acted and (turner.probe or turner.glide):
            turner.update(None, now, self.reading, None)  # keep a swipe's glide moving

    def tick(self):
        """Runs about 60 times a second. An error in one tick is logged and the next
        tick runs anyway, so the panel never freezes."""
        self.after_id = None
        try:
            self._tick()
        except Exception as e:
            self.errors += 1
            if self.errors <= 20 or self.errors % 500 == 0:  # don't flood the log with a repeating error
                log(f"Error #{self.errors} in the main loop: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            self._reset_actions()
        if not self.closed:
            self.after_id = self.root.after(16, self.tick)

    def _tick(self):
        if os.name == "nt" and time.monotonic() - getattr(self, "_fg_t", 0.0) > 0.25:
            self._fg_t = time.monotonic()
            from . import winput

            winput.track_foreground()
        while not self.commands.empty():
            self._handle(self.commands.get())
        if self.closed:
            return

        now = time.monotonic()
        progress, warn = 0.0, False
        eyes_acted = False  # the page turner already ran this tick
        fps = self._speed(now)
        problem = self.tracker.status  # camera reconnecting, tracker restarting, missing model
        if problem:
            state, warn = problem, True
            self.gaze = None
        elif self.calibration is not None:
            state = "Calibrating..."
        elif self.touchup is not None:
            state = "Touch-up: look at the red dots (Esc skips)"
        elif self.input == "hands":
            self.gaze = None
            state = f"Paused  ({fps})" if self.paused else f"Hands  ·  {fps}"
        elif self.input == "head":
            self.gaze = None
            state, progress, warn = self._head_control(now, fps)
        elif self.model is None:
            state, warn = "Not calibrated - press Calibrate", True
        else:
            self._update_gaze(now)
            if self.paused:
                state = f"Paused  ({fps})"
            elif self.gaze is None:
                state, warn = self.tracker.hint or "Can't see your eyes - look at the screen", True
            else:
                activity = "Reading" if self.reading.is_reading(now) else "Watching"
                state = f"{activity}  ·  {fps}"
                if self.posture is not None and self.posture.needs_touchup:
                    state += ("  ·  you moved: touch-up soon" if self.cfg["touchup"] == "auto"
                              else "  ·  you moved: F6 to fix")
                self._maybe_touchup(now)
            if not self.paused and self.touchup is None:
                eyes_acted = self.mode in ("page", "both")
                event, self.event = self.event, None
                result = self.actions[self.mode].update(self.gaze, now, self.reading, event)
                if self.mode == "scroll":
                    progress = abs(result["speed"]) / self.cfg["scroll"]["max_wheel_per_sec"]
                else:
                    progress = result["progress"]
                    if result.get("event"):
                        self.flash = (f"Turned page: {result['event']}", now + 2.0)
        if not problem and self.calibration is None and self.touchup is None:
            self._hand_gestures(now, eyes_acted)
            if now < self.flash[1]:
                state = self.flash[0]

        self.panel.show(self._title(), state, progress, warn)
        if self.dot:
            if self.gaze is not None and self.calibration is None:
                self.dot.move(self.gaze[0] * self.screen[0], self.gaze[1] * self.screen[1])
            else:
                self.dot.hide()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to a JSON config (default: config.json)")
    parser.add_argument("--mode", choices=MODES, help="eye mode: both, page (turning) or scroll")
    parser.add_argument("--camera", type=int, help="camera index")
    parser.add_argument("--show-gaze", action="store_true", help="show a dot where you are looking")
    parser.add_argument("--recalibrate", action="store_true", help="ignore the saved calibration")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.camera is not None:
        cfg["camera_index"] = args.camera
    if args.show_gaze:
        cfg["show_gaze"] = True

    logging_setup.setup()
    if not _single_instance():
        log("EyeTurn is already running; brought it to the front")
        sys.exit(0)
    _set_dpi_aware()
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(resolve_path("eyeturn.ico")))  # also used by the calibration window
    except tk.TclError:
        pass
    App(root, cfg, recalibrate=args.recalibrate)
    root.mainloop()


if __name__ == "__main__":
    main()
