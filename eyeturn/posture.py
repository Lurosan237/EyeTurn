"""Keeps the calibration right when you sit differently.

A calibration is only exact for the posture it was made in. Lean back, slide
sideways or move the chair, and every gaze point shifts and stretches. So:

- PostureMonitor watches where your head is (left/right, up/down, distance
  from the camera, from MediaPipe's head position in cm) and says when you
  have settled somewhere clearly different from before.
- Each posture keeps its own small correction on top of the calibration: a
  shift per axis (gaze' = gaze + shift). A few points pin that down (a 5-dot
  touch-up, or clicks), unlike the full calibration, which needs 13 dots.
  It's a shift only: in Len's first tests, touch-ups also fitted a stretch of
  0.6-0.67x, which squeezed the gaze and changed how far a glance past the
  edge had to go, so turns stopped feeling predictable.
- PostureStore remembers every posture that got a correction, so coming back
  to one switches to its correction at once, with no dots.

Clicks only teach a correction when the gaze was steady just before the
click and not far from it: people's eyes reach a target 0.1-0.25 s before
they click, and often they click without looking (Huang et al., "PACE",
CHI 2016).
"""
import time
from collections import deque

import numpy as np

from .gaze_model import write_json
from .tracker import FEATURE_NAMES

POSE = [FEATURE_NAMES.index(n) for n in ("tx", "ty", "tz")]
# How far (cm) the head must move in each direction to count as a new posture:
# sideways, up/down, and towards / away from the camera (which changes less per cm).
# Distance is the noisiest: MediaPipe's tz jumped 15-18 cm for a moment in
# Len's logs while the head stayed put, so it needs a wide margin.
MOVE_CM = np.array([4.0, 3.0, 12.0])
SAME_POSTURE = 0.6  # a stored posture this close (in MOVE_CM units) is the same one
SETTLE_S = 5.0  # the new posture must last this long (not just reaching for something)
WINDOW_S = 3.0  # head position is the median over this long
ON_SCREEN = (-0.3, 1.3)  # head position only counts while the gaze is on (or near) the screen;
# not tighter: after a big move the uncorrected gaze itself can land a bit off it


def pose_of(features):
    return np.asarray(features, dtype=float)[POSE]


def posture_distance(a, b):
    """0 = same place, 1 = just far enough to count as moved."""
    return float(np.max(np.abs((np.asarray(a) - np.asarray(b)) / MOVE_CM)))


class Correction:
    """Shift per axis, fitted to (predicted, true) screen points.

    `scale` stays 1 (see the module notes); it's kept so the saved format and
    `apply` don't change if a stretch is ever wanted again.

    The fit is pulled towards a starting guess (no change, or the correction
    of the closest posture), as if a few points already said so, so one or two
    samples can only nudge it, and a single bad one can't throw it far off.
    """

    PRIOR = 2.0  # weight of the "no change" assumption, in samples
    MAX_SHIFT = 0.35  # screen fractions

    def __init__(self, pose, samples=(), start=None):
        self.pose = np.asarray(pose, dtype=float)
        self.samples = [(np.asarray(p, float), np.asarray(t, float), float(w)) for p, t, w in samples]
        start = start or (np.ones(2), np.zeros(2))
        self.start = (np.ones(2), np.array(start[1], float))
        self.scale, self.shift = self.start[0].copy(), self.start[1].copy()
        self.updated = time.time()
        self._fit()

    def apply(self, point):
        return self.scale * (np.asarray(point) - 0.5) + 0.5 + self.shift

    def add(self, predicted, target, weight=1.0, max_samples=40):
        self.samples.append((np.asarray(predicted, float), np.asarray(target, float), float(weight)))
        del self.samples[:-max_samples]
        self.updated = time.time()
        self._fit()

    def _fit(self):
        a0, b0 = self.start
        if not self.samples:
            self.scale, self.shift = np.ones(2), b0.copy()
            return
        P = np.array([s[0] for s in self.samples]) - 0.5
        T = np.array([s[1] for s in self.samples]) - 0.5
        w = np.array([s[2] for s in self.samples])
        # Newer samples describe this posture better than older ones.
        w = w * np.linspace(0.5, 1.0, len(w))
        for _ in range(2):  # refit once without samples that disagree wildly
            # Weighted mean of (true - predicted), pulled towards the start.
            b = ((w[:, None] * (T - P)).sum(axis=0) + self.PRIOR * b0) / (w.sum() + self.PRIOR)
            self.scale = np.ones(2)
            self.shift = np.clip(b, -self.MAX_SHIFT, self.MAX_SHIFT)
            residual = np.linalg.norm(self.scale * P + self.shift - T, axis=1)
            limit = max(0.12, 3 * np.median(residual))
            if len(w) < 4 or (residual <= limit).all():
                break
            w = np.where(residual <= limit, w, 0.0)

    def to_json(self):
        return {
            "pose": self.pose.tolist(),
            "samples": [[p.tolist(), t.tolist(), w] for p, t, w in self.samples],
            "start": [self.start[0].tolist(), self.start[1].tolist()],
            "updated": self.updated,
        }

    @classmethod
    def from_json(cls, d):
        c = cls(d["pose"], d["samples"], d.get("start"))
        c.updated = d.get("updated", c.updated)
        return c


class PostureStore:
    """Corrections for every posture seen, saved next to the calibration."""

    def __init__(self, path, calibration_id, home_pose):
        self.path = path
        self.calibration_id = calibration_id
        self.home = np.asarray(home_pose, dtype=float)  # where you sat while calibrating
        self.corrections = [Correction(self.home)]
        self._load()

    def _load(self):
        import json

        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("calibration") != self.calibration_id or d.get("version") != 2:
                return  # made for an older calibration, or with stretch (version 1)
            self.corrections = [Correction.from_json(c) for c in d["postures"]] or self.corrections
        except FileNotFoundError:
            pass
        except Exception as e:  # damaged file: start over rather than crash
            print(f"Ignoring {self.path}: {type(e).__name__}: {e}", flush=True)

    def save(self):
        try:
            write_json(self.path, {
                "calibration": self.calibration_id,
                "version": 2,
                "postures": [c.to_json() for c in self.corrections],
            })
        except OSError as e:
            print(f"Couldn't save postures: {e}", flush=True)

    def nearest(self, pose):
        """(correction, distance) of the stored posture closest to `pose`."""
        best = min(self.corrections, key=lambda c: posture_distance(c.pose, pose))
        return best, posture_distance(best.pose, pose)

    def add(self, correction, max_postures=12):
        self.corrections.append(correction)
        if len(self.corrections) > max_postures:  # forget the posture used longest ago
            oldest = min(self.corrections[1:], key=lambda c: c.updated)
            self.corrections.remove(oldest)


class PostureMonitor:
    """Follows your head position and picks the right correction for it.

    `current` is the correction in use. `needs_touchup` is True once you've
    settled in a posture that has no correction of its own yet.
    """

    def __init__(self, store):
        self.store = store
        self.history = deque()  # (time, pose)
        self.current = store.corrections[0]
        self.candidate_since = None
        self.candidate_pose = None
        self.needs_touchup = False
        self.pose = None

    def update(self, features, t, gaze=None):
        """Feeds one frame; returns a message when you've settled in another posture.

        `gaze` is the predicted point (screen fractions). Frames where it is off
        the screen are left out: looking at the keyboard, the phone or away
        turns the head and throws MediaPipe's head position off for a moment,
        which in Len's first week counted as 28 moves in half an hour.
        """
        if gaze is not None and not all(ON_SCREEN[0] <= g <= ON_SCREEN[1] for g in gaze):
            return None
        self.history.append((t, pose_of(features)))
        while self.history and self.history[0][0] < t - WINDOW_S:
            self.history.popleft()
        if len(self.history) < 10:
            return None
        self.pose = np.median([p for _, p in self.history], axis=0)
        if posture_distance(self.current.pose, self.pose) < 1.0:
            self.candidate_since = self.candidate_pose = None
            return None
        # Clearly away from the posture in use: wait until it lasts, in one
        # place (still drifting around means you're moving, not settled).
        if self.candidate_pose is None or posture_distance(self.candidate_pose, self.pose) > SAME_POSTURE:
            self.candidate_since, self.candidate_pose = t, self.pose
        if t - self.candidate_since < SETTLE_S:
            return None
        self.candidate_since = self.candidate_pose = None
        best, dist = self.store.nearest(self.pose)
        if dist < SAME_POSTURE:
            self.current, self.needs_touchup = best, False
            return f"back to a known posture ({self._describe(best.pose)})"
        # A new posture: until a touch-up, use the closest known correction, and
        # start this posture's own from it. Being "here" now also means small
        # further moves don't count as moving again.
        self.current = Correction(self.pose, start=(best.scale, best.shift))
        self.needs_touchup = True
        return f"moved to a new posture ({self._describe(self.pose)})"

    def _describe(self, pose):
        d = np.asarray(pose) - self.store.home
        return f"{d[0]:+.0f} cm sideways, {d[1]:+.0f} cm up/down, {d[2]:+.0f} cm distance vs. calibration"

    def adopt_current(self):
        """Keeps the current correction for this posture from now on (after it learned something)."""
        if self.current not in self.store.corrections:
            self.store.add(self.current)


class ClickValidator:
    """Finds the gaze that belongs to a click, or None if the click can't be trusted."""

    def __init__(self, max_error, steady=0.12):
        self.max_error = max_error  # screen fractions between gaze and click
        self.steady = steady  # how still the gaze must be (screen fractions)
        self.frames = deque()  # (time, predicted point before correction)

    def add(self, t, predicted):
        self.frames.append((t, np.asarray(predicted, float)))
        while self.frames and self.frames[0][0] < t - 1.5:
            self.frames.popleft()

    def gaze_for_click(self, t_click):
        """Mean predicted gaze over the steady moment just before the click.

        Looks at 0.6 s to 0.03 s before the click for the latest run of at least
        3 frames that stay within `steady` of their mean.
        """
        pts = [(t, p) for t, p in self.frames if t_click - 0.6 <= t <= t_click - 0.03]
        if len(pts) < 3:
            return None
        P = np.array([p for _, p in pts])
        # Single webcam frames scatter by about a tenth of the screen even while
        # the eyes rest, so judge steadiness on a 3-frame running median.
        P = np.array([np.median(P[max(0, i - 1): i + 2], axis=0) for i in range(len(P))])
        for end in range(len(P), 2, -1):
            start = end - 1
            while start > 0:
                window = P[start - 1:end]
                if (np.linalg.norm(window - window.mean(axis=0), axis=1) > self.steady).any():
                    break
                start -= 1
            if end - start >= 3:
                return P[start:end].mean(axis=0)
        return None

    def accept(self, predicted, target, correction):
        return np.linalg.norm(correction.apply(predicted) - target) <= self.max_error
