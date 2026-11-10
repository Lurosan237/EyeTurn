"""Recognizes reading from the gaze signal.

Reading has a distinctive rhythm: the eyes drift rightwards along a line, then
make one big jump back to the left to start the next line (a "return sweep").
Those sweeps are large compared to webcam noise, so they are easy to spot
even when the absolute gaze position is a few centimetres off. A run of them
means the user is reading; when they stop (looking at a figure, thinking,
looking away) the user isn't.

A large jump from the bottom of the screen back to the top-left is the other
signal: that is where your eyes go when you finish a page.
"""
from collections import deque

import numpy as np


class ReadingDetector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.history = deque()  # (t, x, y) of unsmoothed gaze points
        self.sweeps = deque()  # (t, y_landed) of detected line changes
        self.last_event_t = -1e9
        self.page_jump_t = -1e9
        self.margins = None  # (landing x, launch x) of the text being read

    def _window(self, t0, t1):
        pts = [(x, y) for t, x, y in self.history if t0 <= t <= t1]
        return np.array(pts) if pts else np.empty((0, 2))

    def update(self, point, t):
        """Feed one new gaze point; returns "sweep", "page_jump" or None."""
        c = self.cfg
        self.history.append((t, point[0], point[1]))
        while self.history and self.history[0][0] < t - 2.0:
            self.history.popleft()
        while self.sweeps and self.sweeps[0][0] < t - 120:
            self.sweeps.popleft()

        if t - self.last_event_t < c["min_line_s"]:
            return None
        # Compare where the eyes were a moment ago with where they are now;
        # averaging a few frames on each side keeps noise from looking like a jump.
        before = self._window(t - 0.75, t - 0.25)
        after = self._window(t - 0.2, t)
        if len(before) < 2 or len(after) < 2:
            return None
        b, a = before.mean(axis=0), after.mean(axis=0)
        dx, dy = a - b

        if dy < -c["page_jump_min"] and b[1] > c["page_bottom"] and dx < c["page_jump_max_dx"]:
            self.last_event_t = self.page_jump_t = t
            self.history.clear()
            return "page_jump"
        # A line change follows a rightward drift along the line, unlike a
        # random leftward glance while looking at a picture.
        earlier = self._window(t - 1.4, t - 0.8)
        drifted_right = len(earlier) < 2 or b[0] - earlier[:, 0].mean() > -0.02
        if dx < -c["sweep_min"] and abs(dy) < c["sweep_max_dy"] and drifted_right:
            self.last_event_t = t
            self.sweeps.append((t, a[1], a[0], b[0]))
            self.history.clear()
            return "sweep"
        return None

    def is_reading(self, now):
        """True after a few line changes, until the line changes stop for a while."""
        c = self.cfg
        recent = [s for s in self.sweeps if now - s[0] <= c["reading_window_s"]][-4:]
        if not recent or now - recent[-1][0] > c["max_line_s"]:
            self.margins = None
            return False
        ends = np.array([(s[2], s[3]) for s in recent])
        tol = c["margin_tolerance"]
        if self.margins is not None:
            # Already reading: keep going while line changes still hit the same margins.
            hits = (np.abs(ends[-3:] - self.margins) <= tol).all(axis=1)
            if hits.sum() >= min(2, len(hits)):
                return True
            self.margins = None
        if len(recent) < c["min_sweeps"]:
            return False
        # Starting to read: line changes start at the same right edge, land on
        # the same left margin, and each lands a line lower (or level, while
        # scrolling). Random glances around a picture don't line up like that.
        median = np.median(ends, axis=0)
        aligned = (np.abs(ends - median) <= tol).all(axis=1)
        lo, hi = c["line_step_range"]
        steps = sum(
            1
            for prev, cur, ok_prev, ok_cur in zip(recent, recent[1:], aligned, aligned[1:])
            if ok_prev and ok_cur and lo <= cur[1] - prev[1] <= hi
        )
        if steps >= c["min_sweeps"] - 1:
            self.margins = median
            return True
        return False

    def lines_read_below(self, y, since):
        """Number of line changes landing below `y` since time `since`."""
        return sum(1 for t, ly, _, _ in self.sweeps if t >= since and ly >= y)
