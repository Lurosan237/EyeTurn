"""Fullscreen calibration: follow the dot, move your head a little, then check the live result."""
import time
import tkinter as tk

import numpy as np

from .filters import GazeSmoother
from .gaze_model import HEAD_GROUP, GazeModel
from .tracker import FAR_EYE_SPAN, FEATURE_NAMES, NEAR_EYE_SPAN, OPENNESS

try:  # the camera preview in the positioning guide; calibration works without it
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

PREVIEW = (384, 216)  # camera preview size on the intro screen
# Eye corner distance (share of the picture width) that counts as a good distance.
GOOD_SPAN = (FAR_EYE_SPAN * 1.4, NEAR_EYE_SPAN * 0.8)

_HEAD_X, _HEAD_Y = FEATURE_NAMES.index("head_x"), FEATURE_NAMES.index("head_y")

# A light screen by default: reading white pages shrinks the pupils and changes
# the camera's exposure, so calibrating on a dark screen would not match.
THEMES = {
    "light": {"bg": "#f2f1ed", "fg": "#222222", "dot": "#e0453a", "mark": "#b8b8b8", "gaze": "#1a9e55"},
    "dark": {"bg": "#14161a", "fg": "#e8e8e8", "dot": "#ff5a4f", "mark": "#666666", "gaze": "#3ddc84"},
}
# Points where you keep looking at the dot while moving your head, so the
# model learns to tell head movement apart from eye movement.
HEAD_POINTS = [(0.5, 0.5), (0.25, 0.75), (0.75, 0.25)]


def calibration_points(count, margin):
    lo, hi = margin, 1 - margin
    grid = [(x, y) for y in (lo, 0.5, hi) for x in (lo, 0.5, hi)]
    if count >= 13:
        grid += [(0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7)]
    # Center first, then the rest; a fixed order keeps sessions comparable.
    grid.sort(key=lambda p: (p != (0.5, 0.5),))
    return grid


def drop_blinks(samples):
    """Blinks are judged against this dot's own typical openness: looking at a
    low dot narrows the eyes, and that must not count as a blink."""
    X = np.asarray(samples)
    return X[X[:, OPENNESS] >= 0.6 * np.median(X[:, OPENNESS])] if len(X) else X


def drop_extreme_head(samples, reference, limit=0.15):
    """Keep head-movement samples within about 9 degrees of the usual pose;
    bigger turns are not how anyone reads and only distort the fit."""
    X = np.asarray(samples)
    if not len(X):
        return X
    dev = np.abs(X[:, [_HEAD_X, _HEAD_Y]] - reference)
    return X[(dev <= limit).all(axis=1)]


def clean_samples(samples, keep=0.75):
    """Drop blinks and glances away from the dot so they don't skew the fit."""
    X = np.asarray(samples)
    if len(X) < 5:
        return X
    X = drop_blinks(X)
    eye = X[:, :3]  # iris x/y and openness; head features barely move within a point
    med = np.median(eye, axis=0)
    mad = np.median(np.abs(eye - med), axis=0) + 1e-6
    dist = np.linalg.norm((eye - med) / mad, axis=1)
    return X[np.argsort(dist)[: max(5, int(len(X) * keep))]]


class CalibrationScreen:
    def __init__(self, root, tracker, cfg, screen, on_done, first_run=False):
        self.first_run = first_run
        self.preview_img = None
        self.tracker = tracker
        self.cfg = cfg["calibration"]
        self.colors = THEMES.get(self.cfg["theme"], THEMES["light"])
        self.smoother = GazeSmoother(**cfg["filter"], screen=screen)
        self.screen = screen
        self.on_done = on_done
        # (target, group id, is head-movement step)
        self.steps = [(p, i, False) for i, p in enumerate(
            calibration_points(self.cfg["points"], self.cfg["margin"]))]
        if self.cfg["head_movement"]:
            self.steps += [(p, HEAD_GROUP + i, True) for i, p in enumerate(HEAD_POINTS)]
        self.model = None
        self.phase = "intro"

        c = self.colors
        self.win = tk.Toplevel(root)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=c["bg"], cursor="none")
        self.canvas = tk.Canvas(self.win, bg=c["bg"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.win.bind("<space>", self._on_space)
        self.win.bind("<Escape>", lambda e: self._close(None))
        self.win.bind("<KeyPress-r>", self._on_redo)
        self.win.focus_force()
        self._intro()

    def _xy(self, p):
        return p[0] * self.screen[0], p[1] * self.screen[1]

    def _text(self, text, y=0.5, size=20, tag="text"):
        self.canvas.create_text(
            self.screen[0] / 2, self.screen[1] * y, text=text, fill=self.colors["fg"],
            font=("Segoe UI", size), justify="center", tag=tag,
        )

    # --- intro ---------------------------------------------------------------
    def _intro(self):
        self.canvas.delete("all")
        welcome = (
            "Welcome to EyeTurn\n\nEyeTurn turns pages and scrolls while you read, using your webcam.\n"
            "First it needs to learn your eyes. This takes about a minute.\n\n"
        ) if self.first_run else "Calibration\n\n"
        self._text(
            welcome
            + "Sit where you normally read, about an arm's length away, and check the picture below.\n"
            "Then look at each red dot until it moves, keeping your head still.\n"
            "At the end, keep looking at the dot while gently moving your head.\n\n"
            "Space: start    Esc: cancel",
            y=0.24, size=18,
        )
        self._intro_status()

    def _position(self):
        """(ok, message, sample) for the positioning guide."""
        _, s = self.tracker.latest()
        status = self.tracker.status
        if status:
            return False, status, None
        if s is None or time.monotonic() - s.t > 0.5 or s.eyes is None:
            return False, self.tracker.hint or "Looking for your face... make sure it's in the picture", s
        if s.span < GOOD_SPAN[0]:
            return False, "A little closer to the screen, please", s
        if s.span > GOOD_SPAN[1]:
            return False, "A little further back from the screen, please", s
        cx = (s.eyes[0][0] + s.eyes[1][0]) / 2
        cy = (s.eyes[0][1] + s.eyes[1][1]) / 2
        if not (0.25 < cx < 0.75 and 0.15 < cy < 0.7):
            return False, "Move so your face is in the middle of the picture", s
        if self.tracker.hint:
            return False, self.tracker.hint, s
        return True, "Good position - press Space to start", s

    def _intro_status(self):
        """Live camera picture with your eyes marked and a distance bar, like the
        track status of commercial eye trackers: green means you sit right."""
        if self.phase != "intro":
            return
        ok, message, s = self._position()
        c = self.canvas
        c.delete("status")
        w, h = PREVIEW
        x0, y0 = self.screen[0] / 2 - w / 2, self.screen[1] * 0.5
        cam = self.tracker.camera
        got = cam.latest() if cam is not None else None
        if got is not None and ImageTk is not None:
            try:
                frame = got[2][:, ::-1, ::-1]  # mirrored, like a mirror; BGR -> RGB
                img = Image.fromarray(np.ascontiguousarray(frame)).resize(PREVIEW)
                self.preview_img = ImageTk.PhotoImage(img)  # kept, or Tk drops the picture
                c.create_image(x0, y0, image=self.preview_img, anchor="nw", tag="status")
            except Exception:
                self.preview_img = None
        else:
            c.create_rectangle(x0, y0, x0 + w, y0 + h, fill="#2c3038", outline="", tag="status")
        color = "#1a9e55" if ok else "#e8a33a"
        c.create_rectangle(x0, y0, x0 + w, y0 + h, outline=color, width=3, tag="status")
        if s is not None and s.eyes is not None:
            for ex, ey in s.eyes:
                px, py = x0 + (1 - ex) * w, y0 + ey * h
                c.create_oval(px - 5, py - 5, px + 5, py + 5, fill=color, outline="white", tag="status")
        # Distance bar: the arrow sits in the green middle when you're at a good distance.
        bx, top, bottom = x0 + w + 24, y0, y0 + h
        lo, hi = GOOD_SPAN
        span_range = (lo * 0.5, hi * 1.3)  # bar bottom = far away, top = very close

        def bar_y(span):
            f = (span - span_range[0]) / (span_range[1] - span_range[0])
            return bottom - max(0.0, min(f, 1.0)) * (bottom - top)

        c.create_rectangle(bx, top, bx + 14, bottom, fill="#d9534f", outline="", tag="status")
        c.create_rectangle(bx, bar_y(hi), bx + 14, bar_y(lo), fill="#1a9e55", outline="", tag="status")
        if s is not None and s.span is not None:
            ay = bar_y(s.span)
            c.create_polygon(bx + 18, ay, bx + 30, ay - 7, bx + 30, ay + 7, fill=self.colors["fg"], tag="status")
        c.create_text(bx + 7, bottom + 14, text="distance", fill=self.colors["fg"],
                      font=("Segoe UI", 10), tag="status")
        self._text(message, y=0.5 + (h + 40) / self.screen[1], size=16, tag="status")
        self.win.after(100, self._intro_status)

    # --- collection ----------------------------------------------------------
    def _start(self):
        self.phase = "collect"
        self.idx = 0
        self.features, self.targets, self.groups = [], [], []
        self.points_done = 0
        self.retried = False
        self._next_point()

    def _next_point(self):
        if self.idx >= len(self.steps):
            self._fit()
            return
        self.samples = []
        self.last_seq = None
        self.t_start = time.monotonic()
        self._collect_tick()

    def _collect_tick(self):
        if self.phase != "collect":
            return
        target, group, head_step = self.steps[self.idx]
        settle = self.cfg["settle_ms"] / 1000
        collect = self.cfg["head_ms" if head_step else "collect_ms"] / 1000
        el = time.monotonic() - self.t_start
        x, y = self._xy(target)
        r = 6 + 22 * max(0.0, 1 - el / settle)
        self.canvas.delete("all")
        if head_step:
            self._text(
                "Keep looking at the dot and gently shift your head a little,\n"
                "the way you move around while reading - small movements are enough",
                y=0.12 if target[1] > 0.4 else 0.88, size=18,
            )
        self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=self.colors["dot"], outline="")
        self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="white", outline="")

        if el > settle:
            seq, s = self.tracker.latest()
            if seq != self.last_seq and s is not None and s.features is not None:
                self.samples.append(s.features)
            self.last_seq = seq
        if el < settle + collect:
            self.win.after(16, self._collect_tick)
            return
        if len(self.samples) < 5 and not self.retried:
            self.retried = True  # couldn't see the eyes; try this point once more
            self._next_point()
            return
        # Head-movement samples are meant to vary, so only blinks are removed there.
        if head_step and not self.features:
            samples = drop_blinks(self.samples)  # no still dots to compare with; the fit will fail anyway
        elif head_step:
            reference = np.median(np.asarray(self.features)[:, [_HEAD_X, _HEAD_Y]], axis=0)
            samples = drop_extreme_head(drop_blinks(self.samples), reference)
        else:
            samples = clean_samples(self.samples)
        if len(samples):
            self.features.extend(samples)
            self.targets.extend([target] * len(samples))
            self.groups.extend([group] * len(samples))
            self.points_done += not head_step
        self.retried = False
        self.idx += 1
        self._next_point()

    # --- result --------------------------------------------------------------
    def _fit(self):
        self.canvas.delete("all")
        dots = sum(1 for _, _, head_step in self.steps if not head_step)
        if self.points_done < max(6, round(0.8 * dots)):
            # A model missing part of the screen guesses wildly there, so don't offer it.
            self.phase = "failed"
            self._text(
                f"Your eyes were only visible for {self.points_done} of {dots} dots.\n"
                "Light your face from the front and keep it in view of the camera.\n\n"
                "R: retry    Esc: cancel"
            )
            return
        self._text("Finding the best model for your eyes...", size=18)
        self.win.update()
        groups = np.array(self.groups)
        with_head = len(set(groups[groups >= HEAD_GROUP])) >= 2
        self.model = GazeModel.calibrate(
            self.screen, self.features, self.targets, self.groups, with_head
        )
        # Jitter: how much single-frame predictions scatter while holding still on a dot.
        scale = np.array(self.screen)
        X = np.asarray(self.features)
        jitter = []
        for g in set(self.groups):
            if g < HEAD_GROUP:
                pred = self.model.predict(X[groups == g]) * scale
                jitter.append(np.linalg.norm(pred - pred.mean(axis=0), axis=1).mean())
        self.jitter_px = float(np.mean(jitter))
        self.phase = "result"
        self.smoother.reset()
        self.preview = None
        self.preview_seq = None
        self._result_tick()

    def _result_tick(self):
        if self.phase != "result":
            return
        self.canvas.delete("all")
        for p, _, _ in self.steps:
            x, y = self._xy(p)
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline=self.colors["mark"])
        self._text(
            f"Expected accuracy: {self.model.cv_error:.0f} px"
            + (f"  ({self.model.cv_head_error:.0f} px while moving your head)"
               if self.model.cv_head_error is not None else "")
            + f"    Raw jitter: {self.jitter_px:.0f} px\n"
            f"(measured on dots the model wasn't trained on; using {self.model.spec['name']})\n\n"
            "Look around - the green dot should follow your eyes\n"
            "(smoothed the same way as during reading).\n"
            "Space: accept    R: redo    Esc: cancel",
            y=0.5, size=18,
        )
        seq, s = self.tracker.latest()
        if seq != self.preview_seq and s is not None and s.features is not None:
            self.preview = self.smoother(self.model.predict(s.features), s.t)
        self.preview_seq = seq
        if self.preview is not None:
            x, y = self._xy(self.preview)
            self.canvas.create_oval(x - 10, y - 10, x + 10, y + 10, fill=self.colors["gaze"], outline="")
        self.win.after(33, self._result_tick)

    def _on_space(self, _event):
        if self.phase == "intro":
            self._start()
        elif self.phase == "result":
            self._close(self.model)

    def _on_redo(self, _event):
        if self.phase in ("result", "failed"):
            self._start()

    def _close(self, model):
        self.phase = "closed"
        self.win.destroy()
        self.on_done(model)
