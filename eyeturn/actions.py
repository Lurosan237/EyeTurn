"""What gaze does: turn pages when you finish them, or scroll at your reading pace."""
import math
import os

import numpy as np
from pynput import keyboard, mouse

WHEEL_DELTA = 120


def _inside(point, zone):
    x0, y0, x1, y1 = zone
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


class InputSender:
    """Sends key presses and fine-grained wheel scrolls to whatever window has focus."""

    def __init__(self, min_wheel_step=15, follow_focus=True):
        self.kb = keyboard.Controller()
        self.mouse = mouse.Controller()
        self.min_step = min_wheel_step
        self.follow_focus = follow_focus
        self._acc = 0.0
        self._win = os.name == "nt"
        if self._win:
            from . import winput

            self._send_wheel = winput.send_wheel

    def press(self, name):
        key = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)
        self.kb.press(key)
        self.kb.release(key)

    def scroll(self, wheel_units, gaze_px=None, anchor=False):
        """Positive = scroll down. Accumulates fractions until a step is worth sending.

        `gaze_px` is where you're looking in screen pixels; on Windows the wheel
        event is sent from there when the pointer isn't over the focused window.
        """
        self._acc += wheel_units
        if self._win:
            # Sub-notch deltas give smooth scrolling in browsers and most modern viewers.
            if abs(self._acc) >= self.min_step:
                delta = int(self._acc)
                self._acc -= delta
                self._send_wheel(-delta, gaze_px, self.follow_focus, anchor)
        elif abs(self._acc) >= WHEEL_DELTA:
            steps = int(self._acc / WHEEL_DELTA)
            self._acc -= steps * WHEEL_DELTA
            self.mouse.scroll(0, -steps)

    def reset(self):
        self._acc = 0.0



class PageTurner:
    """Turns the page when you finish it.

    Automatic: once you have read a couple of lines near the bottom of the
    page, the jump your eyes make back up to the top-left (to start the next
    page) turns it. Manual: glance quickly past the right edge of the screen
    (next) or the left edge (previous). One glance turns only one page; look
    back at the page and glance again to turn another, or keep looking past the
    edge to keep scrolling (hold_scroll). The old corner dwell
    (bottom-right / top-left) can be turned back on with corner_turn.

    A turn either glides the text up by one screenful (smooth, works in any
    viewer that scrolls) or presses a key (next_key / prev_key), set by
    turn_style. To know how far one screenful is, a glide starts with a small
    nudge and measures how far the text moved on screen (see screenshift.py).
    """

    def __init__(self, cfg, sender):
        self.cfg = cfg
        self.sender = sender
        self.with_scroll = False  # True in "both" mode: auto-scroll follows the reading
        self.screenshift = None
        self.good_height = None  # last trustworthy visible height, in pixels
        self.good_px_per_unit = None  # last trustworthy scroll per wheel unit, in pixels
        self.foreground_exe = None
        if os.name == "nt":
            from . import winput

            self.foreground_exe = winput.foreground_exe
        if os.name == "nt" and cfg["measure_screen"]:
            try:
                from . import screenshift

                self.screenshift = screenshift
            except ImportError as e:
                print(f"Page turns use glide_wheel: can't measure the screen ({e})")
        self.reset()

    def reset(self):
        self.zone = None
        self.enter_t = 0.0
        self.fired = None  # zone that last turned a page and hasn't been left yet
        self.cooldown_until = 0.0
        self.page_start_t = 0.0  # lines read before this belong to an earlier page
        self.glide = None  # (start time, wheel units to scroll, wheel units sent so far)
        self.read_to = None  # screen y of the lowest line read before an automatic turn
        self.probe = None  # (start, last check, direction, window rect, snapshot before, latest snapshot)
        self.armed = True  # after a turn, wait until your eyes go back up before the next one
        self.glance_side = None  # side of the screen you're glancing past right now
        self.glance_t = 0.0  # when that glance started
        self.glance_ready = True  # False after a glance turn until you look back at the page
        self.inside_t = {"next": -1e9, "prev": -1e9}  # last time the gaze was clear of each edge
        self.hold = None  # glance turn you're still holding: side, turn time, last time out, speed, last update
        self.sender.reset()

    def _zone_at(self, gaze):
        if _inside(gaze, self.cfg["next_zone"]):
            return "next"
        if self.cfg["prev_enabled"] and _inside(gaze, self.cfg["prev_zone"]):
            return "prev"
        return None

    def _turn(self, which, now, read_to=None):
        c = self.cfg
        self.read_to = read_to
        exe = self.foreground_exe() if self.foreground_exe else ""
        if exe and exe in c["ignore_apps"]:
            print(f"Page turn skipped: {exe} is in ignore_apps", flush=True)
            self.cooldown_until = now + c["cooldown_ms"] / 1000
            return False
        if c["turn_style"] == "glide":
            direction = 1 if which == "next" else -1
            if not self._start_probe(direction, now):
                self.glide = (now, direction * c["glide_wheel"], 0.0)
        else:
            self.sender.press(c[f"{which}_key"])
        self.cooldown_until = now + max(c["cooldown_ms"], c["glide_ms"]) / 1000
        self.page_start_t = now
        self.armed = False
        return True

    def swipe_turn(self, which, now):
        """A hand swipe turns like a glance, whatever you're looking at."""
        if self.probe or self.glide:
            return False  # still busy with the last turn
        self.hold = None
        return self._turn(which, now)

    def _hold_scroll(self, gaze, now):
        """Keeps scrolling while you keep looking past the edge after a glance turn."""
        c = self.cfg
        side, turn_t, out_t, speed, last_t = self.hold
        dt = min(now - last_t, 0.1)
        if gaze is not None:
            x, y = gaze
            y_min, y_max = c["glance_y_range"]
            if y_min < y < y_max and (
                x > 1 + c["hold_scroll_out_right"] if side == "next" else x < -c["hold_scroll_out_left"]
            ):
                out_t = now
        if now - out_t > c["hold_scroll_grace_ms"] / 1000:
            if speed:
                print(f"hold scroll: stopped ({side})", flush=True)
                # Lines read before this were on another part of the document.
                self.page_start_t = now
                self.sender.reset()
            self.hold = None
            return None
        if self.probe or self.glide or now - turn_t < c["hold_scroll_after_ms"] / 1000:
            self.hold = (side, turn_t, out_t, speed, now)
            return None
        if not speed:
            print(f"hold scroll: {side}", flush=True)
        f = min((now - turn_t - c["hold_scroll_after_ms"] / 1000) / c["hold_scroll_ramp_s"], 1.0)
        target = c["hold_scroll_speed"] + f * (c["hold_scroll_max_speed"] - c["hold_scroll_speed"])
        speed = min(speed + c["hold_scroll_accel"] * dt, target)
        direction = 1 if side == "next" else -1
        self.sender.scroll(direction * speed * dt, anchor=True)
        self.hold = (side, turn_t, out_t, speed, now)
        # No other turn right as you look back.
        self.cooldown_until = max(self.cooldown_until, now + 0.5)
        return {"zone": side, "progress": speed / c["hold_scroll_max_speed"]}

    def _start_probe(self, direction, now):
        """Scrolls a small nudge and photographs the window before it."""
        if self.screenshift is None:
            return False
        try:
            rect = self.screenshift.reading_window()
            if rect is None:
                return False
            before = self.screenshift.grab(rect)
        except Exception as e:  # a failed screenshot shouldn't stop page turns
            print(f"Screen measurement failed: {e}", flush=True)
            return False
        self.sender.scroll(direction * self.cfg["probe_wheel"], anchor=True)
        self.probe = (now, now, direction, rect, before, None)
        return True

    def _finish_probe(self, now):
        """Once the nudge has finished moving, glides the rest of one screenful."""
        c = self.cfg
        start, checked, direction, rect, before, last = self.probe
        if now - checked < c["probe_wait_ms"] / 1000:
            return
        result = None
        try:
            snap = self.screenshift.grab(rect)
            # Viewers with smooth scrolling animate the nudge: wait until the
            # window stops changing so the whole nudge is measured.
            settled = last is not None and np.abs(snap - last).mean() < 0.5
            if not settled and now - start < c["probe_max_wait_ms"] / 1000:
                self.probe = (start, now, direction, rect, before, snap)
                return
            result = self.screenshift.measure(before, snap)
        except Exception as e:
            print(f"Screen measurement failed: {e}", flush=True)
        self.probe = None
        remaining = c["glide_wheel"] - c["probe_wheel"]
        if result is not None:
            moved, top, bottom = result
            height = bottom - top
            if height < 0.45 * (rect[3] - rect[1]):
                # Too little of the window seemed to scroll (blank page, picture): use
                # the last good measurement, or a cautious share of the window.
                height = self.good_height or int(0.6 * (rect[3] - rect[1]))
            else:
                self.good_height = height
            px_per_unit = abs(moved) / c["probe_wheel"]
            if abs(moved) < c["min_probe_px"]:
                # A nudge that moved only a few pixels is a mismeasurement (one wheel
                # notch scrolls far more); trusting it would scroll many screens. Use
                # the last good measurement, or glide_wheel if there's none yet.
                px_per_unit = self.good_px_per_unit
            else:
                self.good_px_per_unit = px_per_unit
            # One screenful, minus the bottom `overlap` so your last lines end up at the top.
            want = height * (1 - c["overlap"])
            note = ""
            if direction > 0 and self.read_to is not None:
                # Automatic turn: the lowest line read goes to the top, minus a margin
                # for gaze error, but always at least min_turn of a screen.
                read_px = self.read_to * self.screenshift.screen_height() - (rect[1] + top)
                limit = read_px - c["read_margin"] * height
                want = max(min(want, limit), c["min_turn"] * height)
                note = f", read to {read_px:.0f} px"
            if px_per_unit:
                remaining = max(0.0, min((want - abs(moved)) / px_per_unit, c["max_glide_wheel"]))
            print(f"Page turn: {abs(moved)} px per {c['probe_wheel']} wheel units, "
                  f"{height} px visible{note} -> scrolling {want:.0f} px "
                  f"({remaining:.0f} more units)", flush=True)
        elif self.good_height and self.good_px_per_unit:
            # Every turn should move one page. The window looks the same as at the
            # last turn that could be measured, so scroll what one page was then.
            want = self.good_height * (1 - c["overlap"])
            nudge = c["probe_wheel"] * self.good_px_per_unit
            remaining = max(0.0, min((want - nudge) / self.good_px_per_unit, c["max_glide_wheel"]))
            print(f"Page turn: couldn't measure the scroll, using the last page size "
                  f"({want:.0f} px, {remaining:.0f} more units)", flush=True)
            self._save_failed(before, locals().get("snap"))
        else:
            print("Page turn: couldn't measure the scroll, using glide_wheel", flush=True)
            self._save_failed(before, locals().get("snap"))
        self.glide = (now, direction * remaining, 0.0)
        self.cooldown_until = max(self.cooldown_until, now + c["glide_ms"] / 1000)

    def _save_failed(self, before, after):
        """Keeps the snapshots of a turn that couldn't be measured, to find out why."""
        try:
            import time
            from PIL import Image
            from .config import PROJECT_ROOT
            folder = PROJECT_ROOT / "debug_turns"
            folder.mkdir(exist_ok=True)
            stamp = time.strftime("%H%M%S")
            for name, img in (("before", before), ("after", after)):
                if img is not None:
                    Image.fromarray(img.clip(0, 255).astype("uint8")).save(folder / f"{stamp}_{name}.png")
        except Exception as e:
            print(f"Couldn't save the snapshots: {e}", flush=True)

    def _advance_glide(self, now):
        start, distance, sent = self.glide
        f = min((now - start) / (self.cfg["glide_ms"] / 1000), 1.0)
        eased = (1 - math.cos(math.pi * f)) / 2  # slow start, fast middle, slow stop
        self.sender.scroll(distance * eased - sent, anchor=True)
        self.glide = None if f >= 1.0 else (start, distance, distance * eased)

    def update(self, gaze, now, reading, event):
        c = self.cfg
        if event == "page_jump":
            below = reading.lines_read_below(c["end_zone_y"], self.page_start_t)
            print(f"page_jump seen: {below} lines read below {c['end_zone_y']}, cooldown {'on' if now < self.cooldown_until else 'off'}", flush=True)
        if self.probe:
            self._finish_probe(now)
        if self.glide:
            self._advance_glide(now)
        if self.hold:
            held = self._hold_scroll(gaze, now)
            if held:
                return held
        if not self.armed and gaze is not None and gaze[1] < c["rearm_y"]:
            self.armed = True
        # With auto-scroll running, the text already follows your reading: only
        # deliberate turns (glances, swipes, head turns) unless auto_turn_with_scroll.
        auto = not self.with_scroll or c["auto_turn_with_scroll"]
        if (
            event == "page_jump"
            and auto
            and c["auto_turn"]
            and now >= self.cooldown_until
            and reading.lines_read_below(c["end_zone_y"], self.page_start_t) >= c["end_lines"]
        ):
            # Scroll no further than the lowest line you actually read (on screen).
            landed = [ly for t, ly, _, _ in reading.sweeps if t >= self.page_start_t and ly <= 1.15]
            self._turn("next", now, max(landed) if landed else None)
            return {"zone": None, "progress": 1.0, "event": "next (finished page)"}

        if event == "sweep" and auto and c["line_turn"] and self.armed and now >= self.cooldown_until:
            # You've started line_turn_count lines in a row near the bottom (and at
            # least line_turn_min_lines on this page): glide so the line you're on
            # moves up to the top, like turning the page.
            landed = [ly for t, ly, _, _ in reading.sweeps if t >= self.page_start_t]
            last = landed[-c["line_turn_count"]:]
            if (
                len(landed) >= c["line_turn_min_lines"]
                and len(last) == c["line_turn_count"]
                and min(last) >= c["line_turn_y"]
                and max(last) <= 1.15
            ):
                print(f"line turn: landed at {[round(y, 2) for y in last]}", flush=True)
                self._turn("next", now, max(y for y in landed if y <= 1.15))
                return {"zone": None, "progress": 1.0, "event": "next (last lines)"}

        if gaze is None:
            self.zone = None
            return {"zone": None, "progress": 0.0}

        # Glance: a quick look past the right edge turns to the next screen, past
        # the left edge to the previous one. It has to start from the page within
        # glance_window_ms, so a slow drift off the screen doesn't count.
        x, y = gaze
        if x < 0.85:
            self.inside_t["next"] = now
        if x > 0.15:
            self.inside_t["prev"] = now
        if 0.15 < x < 0.85:
            self.glance_ready = True  # looked back at the page: another glance can turn again
        side = None
        y_min, y_max = c["glance_y_range"]
        if y_min < y < y_max:  # looking away from the whole screen isn't a glance
            if x > 1 + c["glance_out_right"]:
                side = "next"
            elif x < -c["glance_out_left"]:
                side = "prev"
        if side != self.glance_side:
            self.glance_side, self.glance_t = side, now
        if (
            c["glance_turn"]
            and side
            and self.glance_ready
            and now >= self.cooldown_until
            and now - self.glance_t >= c["glance_hold_ms"] / 1000
            and self.glance_t - self.inside_t[side] <= c["glance_window_ms"] / 1000
        ):
            print(f"glance turn: {side}", flush=True)
            if self._turn(side, now) and c["hold_scroll"]:
                self.hold = (side, now, now, 0.0, now)
            self.glance_ready = False
            return {"zone": side, "progress": 1.0, "event": f"{side} (glance)"}

        if not c["corner_turn"]:
            return {"zone": None, "progress": 0.0}
        zone = self._zone_at(gaze)
        if zone != self.fired:
            self.fired = None
        armed = zone is not None and zone != self.fired and self.armed and now >= self.cooldown_until
        if zone != self.zone or not armed:
            self.zone = zone
            self.enter_t = now
        if not armed:
            return {"zone": zone, "progress": 0.0}
        progress = (now - self.enter_t) / (c[f"{zone}_dwell_ms"] / 1000)
        if progress >= 1.0:
            print(f"corner turn: {zone}", flush=True)
            self._turn(zone, now)
            self.fired = zone
            return {"zone": zone, "progress": 1.0, "event": zone}
        return {"zone": zone, "progress": progress}


class AirScroller:
    """Scrolls like a phone, with your hand in the air (see hands.py).

    Each hand step ("drag") is spread over smooth_s so the text moves smoothly
    although the hand is only seen about 15 times a second; a flick ("fling")
    keeps it gliding, slowing down over fling_tau_s. Another flick the same way
    adds to the glide; a drag the other way stops it, like touching the screen.
    """

    def __init__(self, cfg, sender, ignore_apps):
        self.cfg = cfg
        self.sender = sender
        self.ignore_apps = ignore_apps
        self.foreground_exe = None
        if os.name == "nt":
            from . import winput

            self.foreground_exe = winput.foreground_exe
        self.reset()

    def reset(self):
        self.pending = 0.0
        self.velocity = 0.0
        self.last_t = None

    def _allowed(self):
        exe = self.foreground_exe() if self.foreground_exe else ""
        return not (exe and exe in self.ignore_apps)

    def drag(self, units):
        if self._allowed():
            self.pending += units
            if units * self.velocity < 0:
                self.velocity = 0.0  # pushing the other way stops the glide

    def fling(self, speed):
        if self._allowed():
            if speed * self.velocity > 0:
                speed += self.velocity  # flicking again while it glides speeds it up, like on a phone
            self.velocity = max(-self.cfg["max_fling"], min(speed, self.cfg["max_fling"]))

    @property
    def active(self):
        return abs(self.pending) >= 1 or self.velocity != 0.0

    def update(self, now):
        dt = 0.0 if self.last_t is None else min(now - self.last_t, 0.1)
        self.last_t = now
        step = self.pending * (1 - math.exp(-dt / self.cfg["smooth_s"]))
        self.pending -= step
        step += self.velocity * dt
        self.velocity *= math.exp(-dt / self.cfg["fling_tau_s"])
        if abs(self.velocity) < 30:
            self.velocity = 0.0
        if step:
            self.sender.scroll(step, anchor=True)


class AutoScroller:
    """Scrolls at your reading pace.

    While you are reading, the text drifts up whenever your eyes get below the
    middle of the screen, faster the lower they are, so the line you are on
    settles at a comfortable height. When you stop reading (looking at a
    figure, thinking, looking away) the scrolling stops too. Outside of reading,
    holding your gaze at the bottom or top edge scrolls on purpose.
    """

    def __init__(self, cfg, sender, screen):
        self.cfg = cfg
        self.sender = sender
        self.screen = screen
        self.reset()

    def reset(self):
        self.last_t = None
        self.speed = 0.0
        self.edge = None
        self.edge_t = 0.0
        self.sender.reset()

    @staticmethod
    def _ramp(value, start, end):
        return max(0.0, min((value - start) / (end - start), 1.0))

    def _target_speed(self, gaze, now, reading):
        c = self.cfg
        m = c["offscreen_margin"]
        x, y = gaze
        if not (-m <= x <= 1 + m and -m <= y <= 1 + m):
            return 0.0  # looking away from the screen pauses scrolling
        if reading.is_reading(now):
            self.edge = None
            f = self._ramp(y, c["reading_anchor"], c["reading_full_speed_y"])
            return c["max_wheel_per_sec"] * f ** c["exponent"]

        edge = "down" if y > c["edge_down"] else "up" if y < c["edge_up"] else None
        if edge != self.edge:
            self.edge, self.edge_t = edge, now
        if edge is None or now - self.edge_t < c["edge_dwell_ms"] / 1000:
            return 0.0
        if edge == "down":
            return c["max_wheel_per_sec"] * self._ramp(y, c["edge_down"], 1.0) ** 0.5
        return -c["up_max_wheel_per_sec"] * self._ramp(-y, -c["edge_up"], 0.0) ** 0.5

    def update(self, gaze, now, reading, event):
        dt = 0.0 if self.last_t is None else min(now - self.last_t, 0.1)
        self.last_t = now
        target = 0.0 if gaze is None else self._target_speed(gaze, now, reading)
        # Ease towards the target speed so the text glides instead of lurching.
        step = self.cfg["accel_per_sec2"] * dt
        self.speed += max(-step, min(target - self.speed, step))
        if self.speed:
            gaze_px = None if gaze is None else (gaze[0] * self.screen[0], gaze[1] * self.screen[1])
            self.sender.scroll(self.speed * dt, gaze_px)
        return {"speed": self.speed}


class EyeBoth:
    """Page turning and auto-scroll at the same time ("both" mode).

    The text drifts up at your reading pace (AutoScroller), and a glance past
    the right or left edge still turns a whole page (PageTurner). While a turn
    is measuring, gliding or being held, auto-scroll pauses, so the two never
    push the page at once and the turn's measurement stays right.
    """

    QUIET_S = 0.6  # auto-scroll waits this long after a turn has finished

    def __init__(self, turner, scroller, scroll_cfg):
        self.turner = turner
        self.scroller = scroller
        self.max_speed = scroll_cfg["max_wheel_per_sec"]
        self.quiet_until = 0.0

    def reset(self):
        self.quiet_until = 0.0

    def update(self, gaze, now, reading, event):
        result = self.turner.update(gaze, now, reading, event)
        t = self.turner
        if t.probe or t.glide or t.hold or result.get("event"):
            self.scroller.speed = 0.0  # stop at once; it eases back in afterwards
            self.scroller.last_t = now
            self.quiet_until = now + self.QUIET_S
            return result
        if now < self.quiet_until:
            self.scroller.last_t = now
            return result
        speed = self.scroller.update(gaze, now, reading, None)["speed"]
        return {**result, "progress": max(result["progress"], abs(speed) / self.max_speed), "speed": speed}
