"""Regression from gaze features to normalized screen coordinates.

Which features and how much regularization work best differs per person,
camera and lighting, so calibration tries a few candidates and keeps the one
that best predicts calibration points it was *not* trained on
(leave-one-point-out cross-validation). That held-out error is also the
honest accuracy figure shown to the user.

The model keeps its training data so it can keep learning while you use it:
every mouse click is a free calibration point (people look where they click).
"""
import itertools
import json
import os

import numpy as np

from .tracker import FEATURE_NAMES, OPENNESS

_IDX = {name: i for i, name in enumerate(FEATURE_NAMES)}
FEATURE_SETS = {
    "landmarks": ["eye_x", "eye_y", "openness", "look_x", "look_y"],
    "fine": ["fine_x", "fine_y", "openness", "look_x", "look_y"],
    "both": ["eye_x", "eye_y", "fine_x", "fine_y", "openness", "look_x", "look_y"],
}
# Only tried when L2CS-Net was on during calibration.
NET = ["net_yaw", "net_pitch"]
NET_FEATURE_SETS = {
    "l2cs": NET + ["openness"],
    "fine+l2cs": FEATURE_SETS["fine"] + NET,
    "both+l2cs": FEATURE_SETS["both"] + NET,
}
# Head rotation only. Head position (tx, ty, tz) barely varies while
# calibrating, so the model learned huge weights for it and the gaze point
# drifted off-screen as soon as you sat differently.
HEAD = ["head_x", "head_y"]
# Head features barely vary while calibrating; without a floor on their spread
# a small posture change later would look like a huge, model-breaking jump.
STD_FLOOR = {"head_x": 0.02, "head_y": 0.02, "tx": 0.5, "ty": 0.5, "tz": 1.0}
ALPHAS = (0.3, 1.0, 3.0, 10.0, 30.0)
HEAD_GROUP = 100  # calibration group ids from here up are head-movement dots
Z_CLIP = 4.0
# A posture outside the calibrated range can move the gaze point by at most this
# many standard deviations of head rotation.
HEAD_Z_CLIP = 2.0


def _clip_limits(features):
    return np.array([HEAD_Z_CLIP if n in HEAD else Z_CLIP for n in features])


def _design(z, quadratic):
    cols = [np.ones(len(z)), z]
    if quadratic:
        cols.append(z * z)
    return np.column_stack(cols)


def _fit(X, Y, w, spec):
    cols = [_IDX[n] for n in spec["features"]]
    floor = np.array([STD_FLOOR.get(n, 1e-6) for n in spec["features"]])
    clip = _clip_limits(spec["features"])
    Xs = X[:, cols]
    mean, std = Xs.mean(axis=0), np.maximum(Xs.std(axis=0), floor)
    Z = _design(np.clip((Xs - mean) / std, -clip, clip), spec["quadratic"])
    sw = np.sqrt(w)[:, None]
    Zw, Yw = Z * sw, Y * sw
    reg = spec["alpha"] * np.eye(Z.shape[1])
    reg[0, 0] = 0.0  # don't shrink the intercept
    weights = np.linalg.solve(Zw.T @ Zw + reg, Zw.T @ Yw)
    return cols, mean, std, clip, weights


def _predict(params, quadratic, X):
    cols, mean, std, clip, weights = params
    z = np.clip((np.atleast_2d(X)[:, cols] - mean) / std, -clip, clip)
    return _design(z, quadratic) @ weights


def _candidates(with_head, with_net):
    sets = {**FEATURE_SETS, **(NET_FEATURE_SETS if with_net else {})}
    for name, quadratic, alpha in itertools.product(sets, (False, True), ALPHAS):
        for head in (False, True) if with_head else (False,):
            yield {
                "name": name + ("+head" if head else "") + (" quadratic" if quadratic else ""),
                "features": sets[name] + (HEAD if head else []),
                "quadratic": quadratic,
                "alpha": alpha,
            }


def cross_validate(X, Y, groups, spec, screen):
    """Mean held-out error in pixels, leaving out one calibration point at a time.

    Predictions are lightly averaged over consecutive frames first, like the
    live smoother does, so the score reflects what the user will actually see.
    """
    X, Y, groups = np.asarray(X), np.asarray(Y), np.asarray(groups)
    scale = np.asarray(screen, dtype=float)
    still, moving = [], []
    for g in np.unique(groups):
        test = groups == g
        params = _fit(X[~test], Y[~test], np.ones((~test).sum()), spec)
        pred = _predict(params, spec["quadratic"], X[test])
        k = min(5, len(pred))
        smooth = np.array([pred[max(0, i - k + 1): i + 1].mean(axis=0) for i in range(len(pred))])
        error = np.linalg.norm((smooth - Y[test]) * scale, axis=1).mean()
        (moving if g >= HEAD_GROUP else still).append(error)
    return float(np.mean(still)), float(np.mean(moving)) if moving else None


def select_spec(X, Y, groups, screen, with_head, with_net=False):
    """Tries every candidate; returns (best spec, held-out error holding still,
    held-out error while moving the head or None).

    Head-movement dots count half: robustness to shifting around matters, but
    not at the cost of accuracy in normal reading posture.
    """
    best = None
    for spec in _candidates(with_head, with_net):
        still, moving = cross_validate(X, Y, groups, spec, screen)
        score = still + (0.5 * moving if moving is not None else 0.0)
        if best is None or score < best[0]:
            best = (score, spec, still, moving)
    return best[1:]


def write_json(path, data, indent=None):
    """Writes to a temporary file first and then swaps it in, so a crash or power
    cut halfway through never leaves a half-written, unreadable file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class GazeModel:
    def __init__(self, screen, base_x, base_y, spec, groups=None, extra_x=(), extra_y=(),
                 extra_weight=8.0, max_extra=300, cv_error=None, cv_head_error=None):
        self.screen = tuple(screen)
        self.spec = spec
        self.cv_error = cv_error
        self.cv_head_error = cv_head_error
        self.base_x = np.asarray(base_x, dtype=float)
        self.base_y = np.asarray(base_y, dtype=float)
        self.groups = list(groups) if groups is not None else None
        self.extra_x = [np.asarray(x, dtype=float) for x in extra_x]
        self.extra_y = [np.asarray(y, dtype=float) for y in extra_y]
        self.extra_weight = extra_weight
        self.max_extra = max_extra
        self.openness_ref = float(np.median(self.base_x[:, OPENNESS]))
        self._refit()

    @classmethod
    def calibrate(cls, screen, X, Y, groups, with_head):
        with_net = bool(np.isfinite(np.asarray(X, dtype=float)[:, [_IDX[n] for n in NET]]).all())
        spec, still, moving = select_spec(X, Y, groups, screen, with_head, with_net)
        return cls(screen, X, Y, spec, groups=groups, cv_error=still, cv_head_error=moving)

    def _refit(self):
        X, Y = self.base_x, self.base_y
        w = np.ones(len(X))
        if self.extra_x:
            X = np.vstack([X, self.extra_x])
            Y = np.vstack([Y, self.extra_y])
            # Recent clicks reflect how you sit right now, so weigh them more than old ones.
            n = len(self.extra_x)
            w = np.concatenate([w, self.extra_weight * np.linspace(0.5, 1.0, n)])
        self.params = _fit(X, Y, w, self.spec)

    def add_sample(self, features, target):
        """Learn from one known (features, screen point) pair, e.g. a mouse click."""
        self.extra_x.append(np.asarray(features, dtype=float))
        self.extra_y.append(np.asarray(target, dtype=float))
        del self.extra_x[: -self.max_extra], self.extra_y[: -self.max_extra]
        self._refit()

    def predict(self, features):
        out = _predict(self.params, self.spec["quadratic"], features)
        return out[0] if np.ndim(features) == 1 else out

    def save(self, path):
        data = {
            "features": list(FEATURE_NAMES),
            "screen": list(self.screen),
            "spec": self.spec,
            "cv_error": self.cv_error,
            "cv_head_error": self.cv_head_error,
            "groups": self.groups,
            "base_x": self.base_x.tolist(),
            "base_y": self.base_y.tolist(),
            "extra_x": [x.tolist() for x in self.extra_x],
            "extra_y": [y.tolist() for y in self.extra_y],
        }
        write_json(path, data)

    @classmethod
    def load(cls, path, net_available=False):
        """Returns None for files from an older version with different features,
        or for a model that uses L2CS-Net when it isn't available now."""
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:  # damaged file: calibrate again rather than crash
            print(f"Can't read {path}: {e}; please calibrate again", flush=True)
            return None
        if d.get("features") != list(FEATURE_NAMES) or "spec" not in d:
            return None
        if not net_available and set(NET) & set(d["spec"]["features"]):
            return None
        return cls(
            d["screen"], d["base_x"], d["base_y"], d["spec"], groups=d.get("groups"),
            extra_x=d["extra_x"], extra_y=d["extra_y"], cv_error=d.get("cv_error"),
            cv_head_error=d.get("cv_head_error"),
        )
