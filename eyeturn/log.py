"""Everything EyeTurn prints also goes to logs/eyeturn.log, so a problem can be
looked up afterwards. The file starts over at 2 MB and the last 3 are kept,
so it never fills the disk. Crashes in any thread are logged with their
traceback instead of disappearing.
"""
import sys
import threading
import time
import traceback

from .config import resolve_path

MAX_BYTES = 2_000_000
KEEP = 3  # eyeturn.log plus eyeturn.1.log ... eyeturn.2.log


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class _RotatingStream:
    def __init__(self, path, echo):
        self.path = path
        self.echo = echo  # the console, if there is one (pythonw has none)
        self.lock = threading.Lock()
        self.file = open(path, "a", encoding="utf-8", errors="replace")

    def _rotate(self):
        self.file.close()
        for i in range(KEEP - 1, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"eyeturn.{i - 1}.log")
            dst = self.path.with_name(f"eyeturn.{i}.log")
            try:
                if src.exists():
                    src.replace(dst)
            except OSError:
                pass
        self.file = open(self.path, "a", encoding="utf-8", errors="replace")

    def write(self, text):
        with self.lock:
            try:
                self.file.write(text)
                if self.file.tell() > MAX_BYTES:
                    self._rotate()
            except (OSError, ValueError):
                pass  # a full disk must not stop eye tracking
            if self.echo is not None:
                try:
                    self.echo.write(text)
                except (OSError, ValueError):
                    self.echo = None
        return len(text)

    def flush(self):
        with self.lock:
            for f in (self.file, self.echo):
                try:
                    if f is not None:
                        f.flush()
                except (OSError, ValueError):
                    pass

    def isatty(self):
        return False


def setup():
    """Sends print() output and uncaught errors from every thread to the log file."""
    folder = resolve_path("logs")
    try:
        folder.mkdir(exist_ok=True)
        stream = _RotatingStream(folder / "eyeturn.log", sys.__stdout__)
    except OSError as e:
        print(f"Logging to the console only: {e}", flush=True)
        return
    sys.stdout = sys.stderr = stream

    def thread_crash(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log(f"Crash in thread {name}:\n" + "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    def crash(exc_type, exc_value, exc_tb):
        log("Crash:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    threading.excepthook = thread_crash
    sys.excepthook = crash
    log("EyeTurn starting")
