"""Webcam capture that keeps working when the camera doesn't.

Webcams stop delivering usable pictures more often than you'd think: after
the PC wakes from sleep (frozen or flickering frames at 1 fps), when another
app takes the camera, or when a USB camera is unplugged. OpenCV doesn't
notice any of that, so this reads the camera in its own thread, watches what
comes out, and closes and reopens the camera whenever the pictures stop,
freeze, flicker or slow to a crawl.

`latest()` always returns the newest good frame, so a slow consumer never
falls behind the camera either.
"""
import os
import threading
import time

import cv2

# How long each kind of trouble may last before the camera is reopened.
NO_FRAME_S = 2.0  # read() keeps failing or blocks
FROZEN_S = 3.0  # every picture exactly the same (a stuck driver)
FLICKER_S = 3.0  # pictures jumping between unrelated images (a broken stream)
SLOW_S = 6.0  # far fewer frames per second than a webcam ever gives
SLOW_FPS = 3.0
FLICKER_DIFF = 80.0  # average change per pixel (0-255) that a real scene never keeps up
WARMUP_FRAMES = 5  # dropped after opening while exposure settles
SLEEP_GAP_S = 10.0  # the wall clock jumped this much between frames: the PC was asleep
RETRY_S = (1.0, 2.0, 3.0, 5.0, 10.0)  # waits between reopen attempts, then the last one repeats


def _thumb(frame):
    return cv2.cvtColor(cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)


class Camera(threading.Thread):
    """Reads frames in the background and reopens the camera when it misbehaves.

    `status` is None while frames flow, else a short message for the panel.
    """

    def __init__(self, index, width, height, log=print):
        super().__init__(daemon=True, name="camera")
        self.index = index
        self.size = (width, height)
        self.log = log
        self.status = "Starting the camera..."
        self.fps = 0.0
        self.reopens = 0
        self.brightness = None  # average image brightness 0-255, for "too dark" hints
        self._lock = threading.Lock()
        self._frame = None  # (seq, time, frame)
        self._seq = 0
        self._new = threading.Event()
        self._quit = threading.Event()
        self._reopen = threading.Event()
        self._cap = None
        self.heartbeat = time.monotonic()  # last time read() returned; a stuck driver stops it

    # --- for the consumer -----------------------------------------------------
    def latest(self):
        """(seq, time, frame) of the newest good frame, or None."""
        with self._lock:
            return self._frame

    def wait(self, timeout):
        """Waits for a frame newer than the last one returned by latest()."""
        got = self._new.wait(timeout)
        self._new.clear()
        return got

    def reopen(self, reason):
        """Asks for the camera to be closed and opened again (e.g. after sleep)."""
        self.log(f"Camera: reopening ({reason})")
        self._reopen.set()

    def stop(self):
        self._quit.set()
        self._new.set()

    # --- the reading thread ---------------------------------------------------
    def _backends(self):
        if os.name == "nt":
            return [("DirectShow", cv2.CAP_DSHOW), ("Media Foundation", cv2.CAP_MSMF)]
        return [("default", cv2.CAP_ANY)]

    def _open(self):
        """Tries each backend; returns an open capture that delivered a frame, or None."""
        for name, backend in self._backends():
            cap = None
            try:
                cap = cv2.VideoCapture(self.index, backend)
                if not cap.isOpened():
                    cap.release()
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # newest frame, not a queue of old ones
                ok = False
                for _ in range(WARMUP_FRAMES):
                    ok, frame = cap.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    self.log(f"Camera {self.index} open via {name} at {w}x{h}")
                    return cap
                cap.release()
            except Exception as e:  # a driver error on one backend; try the next
                self.log(f"Camera: {name} failed: {type(e).__name__}: {e}")
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        return None

    def _close(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception as e:
                self.log(f"Camera: release failed: {e}")
            self._cap = None

    def run(self):
        attempt = 0
        while not self._quit.is_set():
            self._reopen.clear()
            self.heartbeat = time.monotonic()
            self._cap = self._open()
            self.heartbeat = time.monotonic()
            if self._cap is None:
                wait = RETRY_S[min(attempt, len(RETRY_S) - 1)]
                attempt += 1
                self.status = (
                    "Camera not found or used by another app - retrying..."
                    if attempt >= 3 else "Connecting to the camera..."
                )
                if attempt in (1, 3) or attempt % 20 == 0:
                    self.log(f"Camera {self.index}: can't open it (attempt {attempt})")
                self.heartbeat = time.monotonic()
                self._quit.wait(wait)
                self.heartbeat = time.monotonic()
                continue
            attempt = 0
            reason = self._read_loop()
            self._close()
            if reason and not self._quit.is_set():
                self.reopens += 1
                self.status = "Reconnecting the camera..."
                self.log(f"Camera: {reason}; reopening (#{self.reopens})")
                self._quit.wait(0.5)
        self._close()

    def _read_loop(self):
        """Reads until the camera misbehaves; returns why it stopped (None = asked to stop)."""
        now = time.monotonic()
        last_good = now
        wall = time.time()
        prev_thumb = None
        frozen_since = flicker_since = slow_since = None
        frames, fps_t = 0, now
        while not self._quit.is_set():
            if self._reopen.is_set():
                return "asked to"
            ok, frame = self._cap.read()
            now = self.heartbeat = time.monotonic()
            # A big jump of the wall clock between two reads means the PC slept.
            # Webcams rarely survive that, so start fresh.
            new_wall = time.time()
            if new_wall - wall > SLEEP_GAP_S:
                return f"the PC was asleep for {new_wall - wall:.0f} s"
            wall = new_wall
            if not ok or frame is None:
                if now - last_good > NO_FRAME_S:
                    return f"no picture for {now - last_good:.0f} s"
                time.sleep(0.01)
                continue

            thumb = _thumb(frame)
            diff = None if prev_thumb is None else float(cv2.absdiff(thumb, prev_thumb).mean())
            prev_thumb = thumb
            self.brightness = float(thumb.mean())
            # An exactly identical picture never happens with a live sensor (there's
            # always noise), unless the whole picture is black (lens covered).
            if diff == 0.0 and self.brightness > 3:
                frozen_since = frozen_since or now
                if now - frozen_since > FROZEN_S:
                    return "the picture is frozen"
                continue  # not a new picture; don't hand it out
            frozen_since = None
            if diff is not None and diff > FLICKER_DIFF:
                flicker_since = flicker_since or now
                if now - flicker_since > FLICKER_S:
                    return "the picture is flickering (broken stream)"
                continue
            flicker_since = None

            last_good = now
            with self._lock:
                self._seq += 1
                self._frame = (self._seq, now, frame)
            self._new.set()
            self.status = None
            frames += 1
            if now - fps_t >= 1.0:
                self.fps = frames / (now - fps_t)
                frames, fps_t = 0, now
                if self.fps < SLOW_FPS:
                    slow_since = slow_since or now
                    if now - slow_since > SLOW_S:
                        return f"only {self.fps:.1f} frames per second"
                else:
                    slow_since = None
        return None


def brightness_hint(brightness):
    """A hint for the panel when the picture is too dark to find a face, else None."""
    if brightness is None:
        return None
    if brightness < 8:
        return "The camera picture is black - is the lens covered?"
    if brightness < 35:
        return "Too dark - light your face from the front"
    return None
