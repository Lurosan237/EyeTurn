"""Webcam capture + MediaPipe face landmarks -> per-frame gaze features.

Runs in a background thread; the UI thread polls `latest()`.
"""
import threading
import time
import traceback
from dataclasses import dataclass

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from .camera import Camera, brightness_hint
from .config import resolve_path
from .log import log
from .gaze_net import face_box

# Landmark indices (478-point mesh with irises). "l"/"r" are the corners on the
# image-left / image-right side, so the horizontal ratio means the same thing
# for both eyes.
EYES = (
    {
        "l": 33, "r": 133, "top": 159, "bottom": 145, "iris": 468, "ring": [469, 470, 471, 472],
        "contour": [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246],
    },
    {
        "l": 362, "r": 263, "top": 386, "bottom": 374, "iris": 473, "ring": [474, 475, 476, 477],
        "contour": [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398],
    },
)

# Feature vector layout: eye x, eye y, eye openness, head direction x/y,
# head translation x/y/z, MediaPipe's own learned gaze estimate (eyeLook*
# blendshapes), the iris centre re-measured in the full-resolution image, and
# L2CS-Net's gaze direction in degrees (NaN when L2CS-Net is off).
FEATURE_NAMES = (
    "eye_x", "eye_y", "openness", "head_x", "head_y", "tx", "ty", "tz", "look_x", "look_y",
    "fine_x", "fine_y", "net_yaw", "net_pitch",
)
OPENNESS = 2
NET_MAX_AGE_S = 1.0  # an older L2CS-Net answer belongs to a different moment; don't use it
RESTART_S = 2.0  # first wait before eye tracking restarts after a crash (doubles up to 30 s)
CAMERA_STUCK_S = 20.0  # a camera call that hasn't returned for this long is stuck for good
STALE_FRAME_S = 0.5  # older frames describe a different moment; skip them
# Outer eye corner distance as a share of the picture width. A face smaller than
# MIN_EYE_SPAN is someone behind you (or too far to track); between that and
# FAR_EYE_SPAN you're far enough away for tracking to suffer.
MIN_EYE_SPAN, FAR_EYE_SPAN, NEAR_EYE_SPAN = 0.035, 0.05, 0.25
TOO_FAR = "Too far from the camera - move closer"


@dataclass
class Sample:
    t: float
    features: np.ndarray | None  # None when no face is visible
    blink: float = 0.0  # 0 = eyes open, 1 = closed
    # Where the irises are in the camera picture (0..1, not mirrored) and the
    # outer eye corner distance as a share of its width, for the positioning guide.
    eyes: tuple | None = None
    span: float | None = None


def _blendshape_features(result):
    if not result.face_blendshapes:
        return [0.0, 0.0], 0.0
    b = {c.category_name: c.score for c in result.face_blendshapes[0]}
    # Subject's left eye looking "out" and right eye looking "in" both mean looking left.
    look_x = (b["eyeLookInLeft"] - b["eyeLookOutLeft"] + b["eyeLookOutRight"] - b["eyeLookInRight"]) / 2
    look_y = (b["eyeLookDownLeft"] + b["eyeLookDownRight"] - b["eyeLookUpLeft"] - b["eyeLookUpRight"]) / 2
    blink = (b["eyeBlinkLeft"] + b["eyeBlinkRight"]) / 2
    return [look_x, look_y], blink


def _refine_iris(gray, pts, eye):
    """Darkness-weighted centroid of the iris inside the visible eye opening.

    MediaPipe locates landmarks on a downscaled face crop; this uses every
    pixel of the full camera frame, which pins the iris centre down more
    precisely. Falls back to MediaPipe's estimate when the eye is too small.
    """
    center = pts[eye["iris"]]
    radius = np.linalg.norm(pts[eye["ring"]] - center, axis=1).mean()
    poly = pts[eye["contour"]]
    x0, y0 = np.maximum(np.floor(poly.min(axis=0)).astype(int) - 2, 0)
    x1, y1 = np.ceil(poly.max(axis=0)).astype(int) + 3
    x1, y1 = min(x1, gray.shape[1]), min(y1, gray.shape[0])
    if x1 - x0 < 8 or y1 - y0 < 4:
        return center
    crop = gray[y0:y1, x0:x1].astype(np.float32)
    mask = np.zeros(crop.shape, np.uint8)
    cv2.fillPoly(mask, [np.round(poly - (x0, y0)).astype(np.int32)], 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    near = (xx - center[0]) ** 2 + (yy - center[1]) ** 2 <= (1.4 * radius) ** 2
    inside = (mask > 0) & near
    if inside.sum() < 10:
        return center
    lo, hi = np.percentile(crop[inside], [5, 55])
    w = np.clip((hi - crop) / max(hi - lo, 1.0), 0, 1) ** 2 * inside
    total = w.sum()
    if total < 1e-3:
        return center
    return np.array([(w * xx).sum() / total, (w * yy).sum() / total])


def _eye_features(pts, eye, gray):
    left, right = pts[eye["l"]], pts[eye["r"]]
    axis = right - left
    width = np.linalg.norm(axis)
    u = axis / width
    n = np.array([-u[1], u[0]])  # perpendicular, pointing down in the image
    mid = (left + right) / 2
    iris = pts[eye["iris"]]
    fine = _refine_iris(gray, pts, eye)
    x = (iris - left) @ u / width
    y = (iris - mid) @ n / width
    openness = abs((pts[eye["bottom"]] - pts[eye["top"]]) @ n) / width
    return x, y, openness, (fine - left) @ u / width, (fine - mid) @ n / width


def extract_features(result, gray):
    """Returns (features, blink score); features is None when no face is found."""
    if not result.face_landmarks:
        return None, 0.0
    lm = result.face_landmarks[0]
    h, w = gray.shape
    pts = np.array([(p.x * w, p.y * h) for p in lm])
    eyes = np.array([_eye_features(pts, e, gray) for e in EYES]).mean(axis=0)
    if result.facial_transformation_matrixes:
        m = np.asarray(result.facial_transformation_matrixes[0])
        head = [m[0, 2], m[1, 2], m[0, 3], m[1, 3], m[2, 3]]
    else:
        head = [0.0] * 5
    look, blink = _blendshape_features(result)
    return np.concatenate([eyes[:3], head, look, eyes[3:], [np.nan, np.nan]]), blink


def _eye_positions(result):
    if not result.face_landmarks:
        return None, None
    lm = result.face_landmarks[0]
    return ((lm[468].x, lm[468].y), (lm[473].x, lm[473].y)), abs(lm[33].x - lm[263].x)


class Tracker(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.error = None
        self.fps = 0.0
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._seq = 0
        self._latest = None
        self._restarting = False
        self.restarts = 0
        self.camera = None
        self.hint = None  # what would make tracking better ("Too dark ...") or None
        self.net = self._load_gaze_net()
        self.hands = self._load_hands()
        # Switched from the status panel: either kind of tracking can run alone.
        self.eyes_on = self.cfg["eyes_enabled"]
        self.hands_on = self.hands is not None and self.cfg["hands"]["enabled"]

    def _load_hands(self):
        try:
            from .hands import HandGestures

            return HandGestures(self.cfg["hands"])
        except Exception as e:  # e.g. the hand model is missing
            print(f"Hand gestures are off: {e}", flush=True)
        return None

    def _load_gaze_net(self):
        net_cfg = self.cfg["gaze_net"]
        if not net_cfg["enabled"]:
            return None
        path = resolve_path(net_cfg["model_path"])
        if not path.exists():
            print(f"L2CS-Net is off: {path} not found (see README). Using face landmarks only.")
            return None
        try:
            from .gaze_net import GazeNet

            return GazeNet(path, net_cfg["threads"])
        except ImportError as e:
            print(f"L2CS-Net is off: {e}. Run pip install -r requirements.txt.")
        except Exception as e:  # e.g. a broken or wrong model file
            print(f"L2CS-Net is off: could not load {path}: {e}")
        return None

    @property
    def net_fps(self):
        return self.net.fps if self.net else None

    def latest(self):
        with self._lock:
            return self._seq, self._latest

    def stop(self):
        self._quit.set()
        if self.camera:
            self.camera.stop()
        if self.net:
            self.net.stop()
        if self.hands:
            self.hands.stop()

    @property
    def status(self):
        """None while tracking runs normally, else what's wrong, for the panel."""
        if self.error:
            return self.error
        if self._restarting:
            return "Restarting eye tracking..."
        if self.camera is None:
            return "Starting the camera..."
        return self.camera.status

    def run(self):
        """Keeps tracking whatever happens: after a crash it starts again by itself."""
        if self.net:
            self.net.start()
        if self.hands:
            self.hands.start()
        delay = RESTART_S
        while not self._quit.is_set():
            started = time.monotonic()
            try:
                self._loop()
                return  # asked to stop
            except FileNotFoundError as e:  # a missing model file; trying again won't help
                self.error = str(e)
                log(f"Eye tracking stopped: {e}")
                return
            except Exception as e:
                self.restarts += 1
                if time.monotonic() - started > 60:
                    delay = RESTART_S  # it had been running fine; this is a new problem
                log(f"Eye tracking crashed, restarting in {delay:.0f} s (#{self.restarts}): "
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                self._restarting = True
                self._quit.wait(delay)
                self._restarting = False
                delay = min(delay * 2, 30.0)

    def _ensure_camera(self):
        """Starts the camera, or a fresh one if the old one's driver got stuck inside a call."""
        cam = self.camera
        if cam is not None and cam.is_alive() and time.monotonic() - cam.heartbeat < CAMERA_STUCK_S:
            return cam
        if cam is not None:
            log("Camera: the driver stopped responding; starting a new connection")
            cam.stop()  # it may never return; it's a daemon thread, so it can't keep EyeTurn open
        self.camera = Camera(self.cfg["camera_index"], self.cfg["camera_width"], self.cfg["camera_height"], log)
        self.camera.start()
        return self.camera

    def _loop(self):
        model_path = resolve_path(self.cfg["model_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"Missing face model: {model_path} (see README)")
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_facial_transformation_matrixes=True,
            output_face_blendshapes=True,
        )
        t0 = time.monotonic()
        last_ts = -1
        last_seq = None
        frames, fps_t = 0, t0
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            while not self._quit.is_set():
                cam = self._ensure_camera()
                cam.wait(0.25)
                got = cam.latest()
                t = time.monotonic()
                if t - fps_t >= 1.0:
                    self.fps = frames / (t - fps_t)
                    frames, fps_t = 0, t
                if got is None or got[0] == last_seq or t - got[1] > STALE_FRAME_S:
                    continue
                last_seq, t, frame = got
                if self.hands_on:
                    self.hands.submit(frame, t)
                if not self.eyes_on:
                    frames = 0
                    continue
                ts = max(int((t - t0) * 1000), last_ts + 1)
                last_ts = ts
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts
                )
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                features, blink = extract_features(result, gray)
                self.hint = self._hint(result, gray.shape[1], cam.brightness)
                if features is not None and self.hint == TOO_FAR:
                    features = None  # a face that small is someone further back, not the reader
                if self.net and features is not None:
                    features = self._add_net_features(features, t, rgb, result)
                eyes, span = _eye_positions(result)
                sample = Sample(t, features, blink, eyes, span)
                with self._lock:
                    self._seq += 1
                    self._latest = sample
                frames += 1

    @staticmethod
    def _hint(result, frame_w, brightness):
        """Why the eyes may be hard to track (dark, too far, too close), or None."""
        if not result.face_landmarks:
            return brightness_hint(brightness)
        lm = result.face_landmarks[0]
        # Distance between the outer eye corners, as a share of the picture width.
        eyes = abs(lm[33].x - lm[263].x)
        if eyes < MIN_EYE_SPAN:
            return TOO_FAR
        if eyes < FAR_EYE_SPAN:
            return "Move a little closer to the screen"
        if eyes > NEAR_EYE_SPAN:
            return "Move back a little from the screen"
        return brightness_hint(brightness)

    def _add_net_features(self, features, t, rgb, result):
        """Hands this frame to L2CS-Net and fills in its latest answer.

        When L2CS-Net is too slow to have a recent answer, its features stay
        NaN: the frame is still usable, and calibration then won't pick a
        model that depends on L2CS-Net.
        """
        if self.net.error:
            raise RuntimeError(f"L2CS-Net: {self.net.error}")
        h, w = rgb.shape[:2]
        pts = np.array([(p.x * w, p.y * h) for p in result.face_landmarks[0]])
        box = face_box(pts, w, h)
        if box is not None:
            self.net.submit(t, rgb, box)
        latest = self.net.latest()
        if latest is not None and t - latest[0] <= NET_MAX_AGE_S:
            features[-2:] = latest[1:]
        return features
