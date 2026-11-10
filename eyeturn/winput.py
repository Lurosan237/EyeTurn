"""Windows mouse-wheel injection that reaches the window you're reading.

Windows delivers wheel events to the window under the mouse pointer, not the
one with keyboard focus. So when the pointer is parked somewhere else, the
wheel event is briefly sent from a point inside the focused window instead:
one SendInput batch moves the pointer there, scrolls, and moves it back, so
the pointer doesn't visibly move.
"""
import ctypes
from ctypes import wintypes

# A private handle: setting argtypes on the shared windll.user32 would break
# pynput, which calls SendInput with its own INPUT type.
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.windll.kernel32

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
GA_ROOT = 2
SM_CXSCREEN, SM_CYSCREEN = 0, 1


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    # The real INPUT is a union; MOUSEINPUT is its largest member, so this has the same size.
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _root_at(x, y):
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None


def _is_own(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == kernel32.GetCurrentProcessId()


def _client_rect(hwnd):
    """The window's client area in screen pixels, as (left, top, right, bottom)."""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    corner = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(corner))
    return corner.x, corner.y, corner.x + rect.right, corner.y + rect.bottom


def _target_point(hwnd, gaze_px):
    """Where to scroll from: where you're looking if that's in the window, else its middle."""
    left, top, right, bottom = _client_rect(hwnd)
    if right <= left or bottom <= top:
        return None
    candidates = []
    if gaze_px is not None and left <= gaze_px[0] < right and top <= gaze_px[1] < bottom:
        candidates.append(gaze_px)
    candidates.append(((left + right) / 2, (top + bottom) / 2))
    for x, y in candidates:
        if _root_at(x, y) == hwnd:  # skip points covered by another window
            return x, y
    return None


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(INPUT_MOUSE, MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))


def _absolute(x, y):
    # Absolute moves use 0..65535 across the primary monitor.
    w, h = user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)
    return _mouse(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        round(x * 65535 / max(w - 1, 1)),
        round(y * 65535 / max(h - 1, 1)),
    )


VK_CONTROL = 0x11
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_exe():
    """File name of the program you're looking at, lowercased (e.g. "wps.exe"), or ""."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


_last_other = None  # the last window in front that wasn't EyeTurn's own


def track_foreground():
    """Remembers the window you're reading, so a click on EyeTurn's panel can hand it back."""
    global _last_other
    fg = user32.GetForegroundWindow()
    if fg and not _is_own(fg):
        _last_other = fg


def give_back_focus():
    """Clicking a button on EyeTurn's panel brings the panel to the front, and turns and
    scrolls then went to the panel (or were skipped). Put the window you were reading
    back in front, as if you had never left it."""
    fg = user32.GetForegroundWindow()
    if _last_other and fg and _is_own(fg) and user32.IsWindow(_last_other):
        user32.SetForegroundWindow(_last_other)


def send_wheel(delta, gaze_px=None, follow_focus=True, anchor=False):
    """Scrolls by `delta` wheel units (positive = up, like the Windows API).

    With `anchor`, the wheel event always comes from inside the document
    (where you look, else the window's middle), even when the pointer is over
    the same window's toolbar or status bar, where it would do nothing.
    """
    if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
        return  # Ctrl + wheel zooms instead of scrolling
    wheel = _mouse(MOUSEEVENTF_WHEEL, data=delta)
    events = [wheel]
    if follow_focus:
        fg = user32.GetForegroundWindow()
        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        if fg and not _is_own(fg) and (anchor or _root_at(cursor.x, cursor.y) != fg):
            target = _target_point(fg, gaze_px)
            if target is not None:
                events = [_absolute(*target), wheel, _absolute(cursor.x, cursor.y)]
    batch = (INPUT * len(events))(*events)
    user32.SendInput(len(events), batch, ctypes.sizeof(INPUT))
