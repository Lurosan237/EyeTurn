"""Measures how far the window you're reading actually scrolled (Windows only).

Every viewer scrolls a different number of pixels per mouse-wheel notch, and
that changes with zoom. So before a page turn, a small nudge is scrolled and
the window is photographed before and after it: the image shift says how many
pixels one wheel unit moves the text, and the rows that changed say how tall
the scrolling part of the window is. The turn then scrolls exactly one
screenful minus a small overlap.
"""
import numpy as np
from PIL import ImageGrab

from . import winput

SCALE = 2  # analyse at half resolution; plenty for a shift measurement


def screen_height():
    return winput.user32.GetSystemMetrics(winput.SM_CYSCREEN)


def reading_window():
    """Client area (left, top, right, bottom) of the window you're reading, or None."""
    hwnd = winput.user32.GetForegroundWindow()
    if not hwnd or winput._is_own(hwnd):
        return None
    left, top, right, bottom = winput._client_rect(hwnd)
    if right - left < 200 or bottom - top < 200:
        return None
    return left, top, right, bottom


def grab(rect):
    """Grayscale snapshot of `rect`, downscaled."""
    img = ImageGrab.grab(bbox=rect, all_screens=True).convert("L")
    img = img.resize((img.width // SCALE, img.height // SCALE))
    return np.asarray(img, dtype=np.float32)


def _rows(img):
    """The middle columns (skips side panels and scrollbars)."""
    w = img.shape[1]
    return img[:, w // 5: w - w // 5]


def _profile(img):
    """Row brightness profile of the middle columns."""
    return _rows(img).mean(axis=1)


def measure(before, after, max_shift_frac=0.5):
    """Returns (pixels the text moved up, top and bottom of the scrolling area),
    in screen pixels relative to the snapshot, or None when the shift can't be
    measured (blank page, nothing moved).
    """
    if before.shape != after.shape:
        return None
    a, b = _profile(before), _profile(after)
    n = len(a)
    changed = np.flatnonzero(np.abs(a - b) > 1.0)
    if len(changed) < 10:
        return None
    lo, hi = changed[0], changed[-1] + 1
    a0, b0 = a[lo:hi] - a[lo:hi].mean(), b[lo:hi] - b[lo:hi].mean()
    m = len(a0)
    best, best_score = None, 0.0
    for s in range(-int(m * max_shift_frac), int(m * max_shift_frac) + 1):
        # The text moved up by s: after[i] matches before[i + s].
        x = a0[max(s, 0): m + min(s, 0)]
        y = b0[max(-s, 0): m - max(s, 0)]
        if len(x) < m // 3:
            continue
        denom = np.sqrt((x * x).sum() * (y * y).sum())
        if denom < 1e-6:
            continue
        score = (x * y).sum() / denom
        if score > best_score:
            best, best_score = s, score
    if best is None or best == 0 or best_score < 0.6:
        return None
    # The scrolling area is where each row of the new snapshot is the old one
    # moved by `best`. Toolbars, page counters and status bars stay put or
    # change in place, so they don't count, even when they changed.
    rows = np.arange(n)
    src = rows + best
    valid = (src >= 0) & (src < n)
    moved_err = np.full(n, np.inf)
    moved_err[valid] = np.abs(_rows(after)[valid] - _rows(before)[src[valid]]).mean(axis=1)
    static_err = np.abs(_rows(after) - _rows(before)).mean(axis=1)
    scrolled = (moved_err < 1.5) & (static_err > moved_err + 1.0)
    hits = np.flatnonzero(scrolled)
    if len(hits) < 5:
        return None
    # Rows near the moved edge can't be checked (no source row), so extend by the shift.
    top, bottom = hits[0], hits[-1] + 1
    if best > 0:
        bottom = min(n, bottom + best)
    else:
        top = max(0, top + best)
    return int(best) * SCALE, int(top) * SCALE, int(bottom) * SCALE
