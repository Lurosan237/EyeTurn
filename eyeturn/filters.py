"""Gaze smoothing: a short median to drop single-frame spikes, a fixation hold, then a One Euro filter."""
import math
from collections import deque

import numpy as np


class OneEuroFilter:
    """Heavy smoothing while the signal is still, low lag while it moves fast."""

    def __init__(self, min_cutoff=1.0, beta=0.0, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self):
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.x_prev is None:
            self.x_prev, self.t_prev = x, t
            return x
        dt = max(t - self.t_prev, 1e-3)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat


class BlinkDetector:
    """A blink is a brief dip in eye openness.

    Looking down narrows the eyes too, but for as long as you look down, so
    openness is compared with its own recent level rather than a fixed
    threshold (a fixed one mistook reading the bottom of the screen for a
    long blink).
    """

    def __init__(self, ratio=0.55, window_s=2.0):
        self.ratio = ratio
        self.window_s = window_s
        self.history = deque()

    def __call__(self, openness, t):
        self.history.append((t, openness))
        while self.history[0][0] < t - self.window_s:
            self.history.popleft()
        return openness < self.ratio * np.median([o for _, o in self.history])


class FixationHold:
    """Holds the gaze point still while your eyes rest on one spot.

    Your eyes don't glide: they rest on a spot (a fixation), then jump to the
    next. Webcam noise makes a resting gaze wander by a hundred pixels or so,
    so while new points stay within `radius_px` of the current fixation, the
    output is the average of that fixation's points. It moves only after
    `confirm` frames in a row land outside it, so one noisy frame can't move it.
    """

    def __init__(self, radius_px, confirm, screen, max_points=30):
        self.radius = np.array([radius_px / screen[0], radius_px / screen[1]])
        self.confirm = max(1, confirm)
        self.max_points = max_points
        self.reset()

    def reset(self):
        self.points = deque(maxlen=self.max_points)
        self.outside = []

    def __call__(self, point):
        if not self.points:
            self.points.append(point)
            return point
        center = np.mean(self.points, axis=0)
        if np.linalg.norm((point - center) / self.radius) <= 1.0:
            self.points.append(point)
            self.outside.clear()
        else:
            self.outside.append(point)
            if len(self.outside) >= self.confirm:
                self.points = deque(self.outside, maxlen=self.max_points)
                self.outside = []
        return np.mean(self.points, axis=0)


class GazeSmoother:
    """Smooths a stream of 2D gaze points (normalized screen coordinates).

    Median (drops one-frame spikes) -> fixation hold (keeps a resting gaze
    still) -> One Euro filter (moves smoothly to the next spot instead of
    teleporting).
    """

    def __init__(self, min_cutoff=0.35, beta=0.3, median_window=5,
                 fixation_radius_px=0, fixation_confirm=3, screen=(1920, 1080)):
        self.window = deque(maxlen=max(1, median_window))
        self.filters = (OneEuroFilter(min_cutoff, beta), OneEuroFilter(min_cutoff, beta))
        self.hold = FixationHold(fixation_radius_px, fixation_confirm, screen) if fixation_radius_px > 0 else None

    def reset(self):
        self.window.clear()
        for f in self.filters:
            f.reset()
        if self.hold:
            self.hold.reset()

    def __call__(self, point, t):
        self.window.append(point)
        p = np.median(self.window, axis=0)
        if self.hold:
            p = self.hold(p)
        return np.array([f(v, t) for f, v in zip(self.filters, p)])
