"""L2CS-Net gaze direction from a face crop, run in the background.

The landmark features only see where the iris sits between the eye corners,
a few pixels on a webcam. L2CS-Net looks at the whole face image and was
trained on a large gaze dataset, so it adds a second, independent estimate of
gaze direction. It's slower than the face landmarker, so it runs in its own
thread on the most recent frame and the tracker uses its latest answer.
"""
import threading
import time

import cv2
import numpy as np

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
BIN_DEG = 4.0  # 90 bins of 4 degrees from -180 to 180


def face_box(pts, frame_w, frame_h):
    """Bounding box of the face landmarks, clamped to the frame."""
    x0, y0 = np.floor(pts.min(axis=0)).astype(int)
    x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, frame_w), min(y1, frame_h)
    return (x0, y0, x1, y1) if x1 - x0 >= 20 and y1 - y0 >= 20 else None


def preprocess(rgb, box, size, nchw):
    x0, y0, x1, y1 = box
    face = cv2.resize(rgb[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA)
    x = (face.astype(np.float32) / 255.0 - MEAN) / STD
    if nchw:
        x = x.transpose(2, 0, 1)
    return x[None]


def bins_to_degrees(probs):
    probs = np.asarray(probs, dtype=np.float64).ravel()
    if probs.min() < 0 or abs(probs.sum() - 1.0) > 1e-2:  # logits, not probabilities
        probs = np.exp(probs - probs.max())
        probs /= probs.sum()
    return float(np.sum(probs * np.arange(len(probs))) * BIN_DEG - 180.0)


class GazeNet(threading.Thread):
    def __init__(self, model_path, num_threads=2):
        super().__init__(daemon=True)
        from ai_edge_litert.interpreter import Interpreter

        self.interp = Interpreter(model_path=str(model_path), num_threads=num_threads)
        self.interp.allocate_tensors()
        inp = self.interp.get_input_details()[0]
        self.input_index, self.input_dtype = inp["index"], inp["dtype"]
        shape = inp["shape"]
        self.nchw = shape[1] == 3
        self.size = int(shape[2])
        outs = self.interp.get_output_details()
        # The model card says output 0 is yaw and output 1 is pitch; prefer names when they say so.
        by_name = {o["name"].lower(): o["index"] for o in outs}
        yaw = next((i for n, i in by_name.items() if "yaw" in n), outs[0]["index"])
        pitch = next((i for n, i in by_name.items() if "pitch" in n), outs[1]["index"])
        self.yaw_index, self.pitch_index = yaw, pitch

        self.error = None
        self.fps = 0.0
        self._cond = threading.Condition()
        self._job = None  # (t, rgb, box) waiting to run; newer frames replace it
        self._latest = None  # (t, yaw_deg, pitch_deg)
        self._quit = False

    def submit(self, t, rgb, box):
        with self._cond:
            self._job = (t, rgb, box)
            self._cond.notify()

    def latest(self):
        with self._cond:
            return self._latest

    def stop(self):
        with self._cond:
            self._quit = True
            self._cond.notify()

    def estimate(self, rgb, box):
        x = preprocess(rgb, box, self.size, self.nchw).astype(self.input_dtype)
        self.interp.set_tensor(self.input_index, x)
        self.interp.invoke()
        yaw = bins_to_degrees(self.interp.get_tensor(self.yaw_index))
        pitch = bins_to_degrees(self.interp.get_tensor(self.pitch_index))
        return yaw, pitch

    def run(self):
        frames, fps_t = 0, time.monotonic()
        try:
            while True:
                with self._cond:
                    while self._job is None and not self._quit:
                        self._cond.wait()
                    if self._quit:
                        return
                    (t, rgb, box), self._job = self._job, None
                yaw, pitch = self.estimate(rgb, box)
                with self._cond:
                    self._latest = (t, yaw, pitch)
                frames += 1
                now = time.monotonic()
                if now - fps_t >= 1.0:
                    self.fps = frames / (now - fps_t)
                    frames, fps_t = 0, now
        except Exception as e:  # surfaced in the status window by the tracker
            self.error = f"{type(e).__name__}: {e}"
