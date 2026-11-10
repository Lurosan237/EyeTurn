"""Default settings, merged with an optional user config.json."""
import copy
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Zones are [x0, y0, x1, y1] in normalized screen coordinates: (0, 0) is the
# top-left corner of the screen, (1, 1) the bottom-right. Values outside 0..1
# are off-screen, so a zone can include "looking past the corner".
DEFAULTS = {
    "camera_index": 0,
    "camera_width": 1280,
    "camera_height": 720,
    "model_path": "models/face_landmarker.task",
    "calibration_file": "calibration.json",
    # What the eyes do: "both" = auto-scroll while you read and glance to turn a
    # page, "page" = page turning only, "scroll" = auto-scroll only. Mode button / F10.
    "mode": "both",
    # What controls reading, one at a time: "eyes", "hands" or "head". Chosen with
    # the Eyes / Hands / Head buttons on the status panel and saved in config.json.
    "input": "eyes",
    "eyes_enabled": True,  # older setting; "input" decides
    "show_gaze": False,
    "debug_features": False,  # log the gaze features once a second (for tuning)
    "blink_ratio": 0.5,  # eye openness below this fraction of normal = blink
    "hold_ms": 350,  # keep the last gaze point through blinks / dropped frames
    # Lower min_cutoff = steadier but laggier; higher beta = follows big eye jumps faster.
    # The gaze point stays put while your eyes stay within fixation_radius_px of
    # one spot, and moves after fixation_confirm frames in a row land outside it.
    "filter": {
        "min_cutoff": 1.0,
        "beta": 1.0,
        "median_window": 5,
        "fixation_radius_px": 110,
        "fixation_confirm": 3,
    },
    "calibration": {
        "points": 13,  # 9 or 13
        "margin": 0.07,
        "settle_ms": 700,
        "collect_ms": 900,
        "head_movement": True,  # finish with a "move your head, keep looking" step
        "head_ms": 4000,
        "theme": "light",  # match what you read: "light" or "dark"
    },
    "reading": {
        "sweep_min": 0.15,  # leftward jump (screen widths) that counts as a new line
        "sweep_max_dy": 0.15,
        "min_line_s": 0.6,  # ignore jumps closer together than this
        "min_sweeps": 3,  # line changes needed to count as "reading"...
        "margin_tolerance": 0.08,  # ...starting/landing within this distance of the same margins
        "line_step_range": [-0.06, 0.15],  # ...each landing this much lower than the last
        "reading_window_s": 12,
        "max_line_s": 6,  # no line change for this long = stopped reading
        "page_bottom": 0.6,  # a page-jump must start below this...
        "page_jump_min": 0.35,  # ...and rise at least this much...
        "page_jump_max_dx": 0.15,  # ...without moving right (that'd be the facing page)
    },
    # L2CS-Net adds a second gaze estimate from the whole face image (see README).
    # Calibration only uses it if it makes the expected accuracy better. Off by
    # default: it needs a fast CPU (about 1 s per frame on Len's PC).
    "gaze_net": {"enabled": False, "model_path": "models/l2cs_gaze360.tflite", "threads": 2},
    # Hand gestures in front of the webcam (see hands.py). Air scroll: flick an
    # open hand up to scroll down, down to scroll up, like on a phone. Swipe
    # sideways to turn: right to left = next screen, left to right = previous
    # (swap_directions flips them).
    "hands": {
        "enabled": True,  # older setting; "input" decides
        "model_path": "models/hand_landmarker.task",
        "detect_width": 480,  # frames are shrunk to this width for the hand model (faster)
        "max_fps": 15,  # hand frames per second; more makes eye tracking slower
        # With no hand in view, look for one only when the camera image changes by
        # more than this (average brightness change, 0-255), or twice a second.
        "motion_gate": 1.5,
        "open_fingers": 2,  # stretched-out fingers that make an open hand
        "debug": False,  # log the hand's position and fingers
        "air_scroll": True,
        "scroll_gain": 2500,  # wheel units the text moves per camera-frame height the hand moves
        "start_speed": 0.4,  # a stroke starts above this hand speed (frame heights per second)...
        "stop_speed": 0.2,  # ...and ends below this one
        "fling_gain": 0.8,  # after a flick, the text glides on at this share of the hand's speed...
        "fling_tau_s": 0.5,  # ...slowing down like on a phone (smaller = stops sooner)
        "max_fling": 8000,  # wheel units per second
        # Moving the other way after a flick is your hand going back, until it has
        # stayed out of the way this long (pause, then flick to scroll back).
        "reverse_pause_ms": 1000,
        "smooth_s": 0.08,  # spreads each hand step over this long so the text moves smoothly
        "swipe_turn": True,
        "swipe_min_dx": 0.18,  # how far the hand must travel, in camera-frame widths...
        "swipe_ms": 700,  # ...within this long
        "swipe_max_dy": 0.7,  # up/down travel allowed, as a share of the sideways travel
        "require_open_hand": True,  # fingers stretched out, so reaching for the mouse doesn't count
        "cooldown_ms": 1000,
        "return_block_ms": 1500,  # ignore your hand going back the other way after a swipe
        "swap_directions": False,
    },
    # Head control (see head.py): tip your head down / up to scroll, turn it right /
    # left and hold to turn a page. Angles are head direction offsets from your
    # normal position (0.1 is about 6 degrees).
    "head": {
        "head_scroll": True,
        "dead_zone": 0.07,  # tipping less than this does nothing
        "full_tilt": 0.22,  # full speed from this much tip
        "max_speed": 1500,  # wheel units per second scrolling down (120 = one notch)
        "up_max_speed": 1200,
        "exponent": 1.5,  # >1: gentle near the dead zone, fast near full tilt
        "accel": 3000,  # wheel units per second², so it starts softly (it stops at once)
        "head_turn": True,
        "turn_at": 0.20,  # turn the head this far...
        "turn_hold_ms": 350,  # ...for this long to turn one page
        "turn_rearm": 0.10,  # then back within this before the next turn (no scrolling while turned)
        "smooth_s": 0.12,  # smooths the head direction over about this long
        "learn_s": 1.0,  # time spent learning your normal position
        "adapt_s": 30.0,  # inside the dead zone the normal position follows you over about this long
        # The face tracker's head direction gets smaller tipping down and turning right
        # (camera picture not mirrored). Flip to 1 if tipping down scrolls up, or
        # turning right goes back a page.
        "tilt_sign": -1,
        "turn_sign": -1,
    },
    "learn_from_clicks": True,  # clicks you looked at refine the calibration for your current posture
    # After you move to a new posture (lean back, shift sideways, move the chair):
    # "auto" shows a 5-dot touch-up by itself once you pause reading; "ask" only
    # says so in the panel and waits for F6.
    "touchup": "auto",
    "page": {
        "auto_turn": True,  # turn when you finish the page and look back to the top
        # In "both" mode auto-scroll follows your reading, so only deliberate turns
        # (glances, hand swipes, head turns) turn by default. True = also auto_turn / line_turn.
        "auto_turn_with_scroll": False,
        "end_zone_y": 0.7,
        "end_lines": 2,  # lines read below end_zone_y before a jump to the top counts
        # "glide": scroll smoothly by one screenful over glide_ms. "key": press
        # next_key / prev_key instead (a jump in most viewers).
        "turn_style": "glide",
        "glide_ms": 700,
        # A glide starts with a probe_wheel nudge (120 = one mouse-wheel notch) and
        # measures on screen how far it moved, then scrolls the rest of the visible
        # height minus `overlap` (the last lines you read stay on screen).
        "measure_screen": True,
        "probe_wheel": 120,
        "probe_wait_ms": 120,  # then check this often until the window stops moving...
        "probe_max_wait_ms": 800,  # ...for at most this long
        "min_probe_px": 25,  # a nudge measured smaller than this is a mismeasurement
        "overlap": 0.10,
        # Automatic turns stop the lowest line you read read_margin of a screen below
        # the top (gaze is a few lines off), but scroll at least min_turn of a screen.
        "read_margin": 0.08,
        "min_turn": 0.4,
        # Also turn while reading: once line_turn_count lines in a row start below
        # line_turn_y, the line you're on glides up to the top.
        "line_turn": True,
        "line_turn_y": 0.72,
        "line_turn_count": 2,
        "line_turn_min_lines": 3,  # lines started on this page before a line turn
        # After any line or corner turn, the next one waits until you look above rearm_y.
        "rearm_y": 0.55,
        # Glance to turn: look further than glance_out_right past the right edge
        # (next screen) or glance_out_left past the left edge (previous screen) for
        # glance_hold_ms. The glance must start from inside the screen within
        # glance_window_ms: a quick glance, not a slow drift. Reading lands past
        # the left edge more often than past the right, so the left needs more.
        "glance_turn": True,
        "glance_out_right": 0.08,
        "glance_out_left": 0.12,
        # Gaze height that still counts as a glance. Looking right reads high
        # (y around -0.3 to -0.8), so the top allows more than the bottom.
        "glance_y_range": [-1.0, 1.3],
        "glance_hold_ms": 250,
        "glance_window_ms": 1000,
        # Hold a glance to keep scrolling: still looking past the edge hold_scroll_after_ms
        # after a glance turn (and once its glide has finished), the text keeps
        # scrolling (right = down, left = up), from hold_scroll_speed up to
        # hold_scroll_max_speed (wheel units per second) over hold_scroll_ramp_s, until
        # you look back. Holding needs less than a glance: hold_scroll_out_* past the
        # edge, with dips back inside up to hold_scroll_grace_ms forgiven.
        "hold_scroll": True,
        "hold_scroll_after_ms": 1000,
        "hold_scroll_out_right": 0.03,
        "hold_scroll_out_left": 0.06,
        "hold_scroll_grace_ms": 400,
        "hold_scroll_speed": 400,
        "hold_scroll_max_speed": 1200,
        "hold_scroll_ramp_s": 3.0,
        "hold_scroll_accel": 1500,  # wheel units per second², so it starts softly (it stops at once)
        "corner_turn": False,  # the old corner dwell (next_zone / prev_zone)
        # Never turn or scroll in these programs: only EyeTurn's own window, so
        # every other app (Claude, File Explorer, browsers, readers...) works.
        "ignore_apps": ["python.exe", "pythonw.exe", "python3.13.exe", "pythonw3.13.exe"],
        "max_glide_wheel": 3600,
        "glide_wheel": 960,  # used when the screen can't be measured and no turn has been yet (about one page)
        "next_key": "right",
        "prev_key": "left",
        "next_zone": [0.70, 0.85, 1.30, 1.40],
        "prev_zone": [-0.30, -0.40, 0.20, 0.12],
        "next_dwell_ms": 600,
        "prev_dwell_ms": 1500,
        "prev_enabled": True,
        "cooldown_ms": 2000,
    },
    "scroll": {
        # While reading: no scrolling above reading_anchor, full speed at reading_full_speed_y.
        "reading_anchor": 0.45,
        "reading_full_speed_y": 0.95,
        "max_wheel_per_sec": 480,  # 120 wheel units = one mouse-wheel notch
        "exponent": 1.4,
        # Not reading: hold your gaze past these edges to scroll on purpose.
        "edge_down": 0.88,
        "edge_up": 0.08,
        "edge_dwell_ms": 600,
        "up_max_wheel_per_sec": 360,
        "accel_per_sec2": 900,
        "offscreen_margin": 0.20,  # gaze further off-screen than this stops scrolling
        "min_wheel_step": 15,
        # Scroll the window with keyboard focus even when the mouse pointer is elsewhere.
        "follow_focus": True,
    },
    "hotkeys": {
        "pause": "<f8>",
        "recalibrate": "<f9>",
        "mode": "<f10>",
        "recenter": "<f7>",
        "touchup": "<f6>",
        "quit": "<ctrl>+<alt>+q",
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def resolve_path(path):
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


INPUTS = ("eyes", "hands", "head")


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = resolve_path(path or "config.json")
    user = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            _merge(cfg, user)
        except (OSError, ValueError) as e:  # a typo in config.json shouldn't stop EyeTurn
            print(f"Ignoring {path}: {e}", flush=True)
    if "input" not in user:  # saved by an older EyeTurn: eyes and hands had separate switches
        if cfg["eyes_enabled"]:
            cfg["input"] = "eyes"
        elif cfg["hands"]["enabled"]:
            cfg["input"] = "hands"
    if cfg["input"] not in INPUTS:
        cfg["input"] = "eyes"
    return cfg
