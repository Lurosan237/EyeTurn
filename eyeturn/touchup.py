"""A quick calibration touch-up after you've moved: look at 5 dots, about 6 seconds.

Unlike the full calibration this doesn't take over the screen: the dots and
the note are small click-through windows that never take keyboard focus, so
your document stays the active window and page turns keep reaching it.
Press Esc to skip.

The dots sit in the four corners and the middle. Tested on Len's saved
calibration, corners + middle gave a correction that never made things worse
and roughly halved the error after a big move, while 3 dots near the middle
sometimes made it worse: the calibration's own error at any one dot is large,
and spreading the dots out lets those errors cancel.
"""
import time
import tkinter as tk

import numpy as np
from pynput import keyboard

from .calibration import clean_samples
from .ui import TRANSPARENT, _make_click_through

POINTS = [(0.5, 0.5), (0.07, 0.07), (0.93, 0.07), (0.93, 0.93), (0.07, 0.93)]
SETTLE_S = 0.6
COLLECT_S = 0.8
MIN_DOTS = 3  # fewer captured dots and the touch-up is dropped
SIZE = 64


class _Overlay:
    """A small always-on-top window that clicks and focus go straight through."""

    def __init__(self, root, width, height):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANSPARENT)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:  # not on Windows
            pass
        self.canvas = tk.Canvas(self.win, width=width, height=height, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        self.win.update_idletasks()
        _make_click_through(self.win)

    def place(self, x, y):
        self.win.geometry(f"+{int(x)}+{int(y)}")

    def destroy(self):
        self.win.destroy()


class TouchUp:
    """Shows the dots one after another and hands back (predicted, target) pairs."""

    def __init__(self, root, tracker, model, screen, on_done):
        self.root = root
        self.tracker = tracker
        self.model = model
        self.screen = screen
        self.on_done = on_done
        self.idx = 0
        self.pairs = []
        self.skipped = False
        self.closed = False

        self.dot = _Overlay(root, SIZE, SIZE)
        self.note = _Overlay(root, 620, 40)
        self.note.canvas.create_rectangle(0, 0, 620, 40, fill="#1d2026", outline="")
        self.note.canvas.create_text(
            310, 20, fill="#e8e8e8", font=("Segoe UI", 12),
            text="EyeTurn: you moved - look at the red dots for a few seconds (Esc skips)",
        )
        self.note.place(screen[0] / 2 - 310, screen[1] * 0.2)
        self.keys = keyboard.Listener(on_press=self._on_key)
        self.keys.start()
        self._next()

    def _on_key(self, key):
        if key == keyboard.Key.esc:
            self.skipped = True

    def _next(self):
        if self.idx >= len(POINTS):
            self._finish()
            return
        self.samples = []
        self.last_seq = None
        self.t0 = time.monotonic()
        x, y = POINTS[self.idx]
        self.dot.place(x * self.screen[0] - SIZE / 2, y * self.screen[1] - SIZE / 2)
        self._tick()

    def _tick(self):
        if self.closed:
            return
        if self.skipped:
            self._finish()
            return
        el = time.monotonic() - self.t0
        r = 6 + 22 * max(0.0, 1 - el / SETTLE_S)
        c = self.dot.canvas
        c.delete("all")
        c.create_oval(SIZE / 2 - r, SIZE / 2 - r, SIZE / 2 + r, SIZE / 2 + r, fill="#e0453a", outline="")
        c.create_oval(SIZE / 2 - 2, SIZE / 2 - 2, SIZE / 2 + 2, SIZE / 2 + 2, fill="white", outline="")
        if el > SETTLE_S:
            seq, s = self.tracker.latest()
            if seq != self.last_seq and s is not None and s.features is not None:
                self.samples.append(s.features)
            self.last_seq = seq
        if el < SETTLE_S + COLLECT_S:
            self.root.after(16, self._tick)
            return
        if len(self.samples) >= 5:
            X = clean_samples(self.samples)
            pred = self.model.predict(X)
            if np.isfinite(pred).all():
                self.pairs.append((np.median(pred, axis=0), np.array(POINTS[self.idx])))
        self.idx += 1
        self._next()

    def _finish(self):
        if self.closed:
            return
        self.closed = True
        self.keys.stop()
        self.dot.destroy()
        self.note.destroy()
        ok = not self.skipped and len(self.pairs) >= MIN_DOTS
        self.on_done(self.pairs if ok else None, self.skipped)

    def cancel(self):
        self.skipped = True
        self._finish()
