"""Small always-on-top status panel and an optional click-through gaze dot."""
import os
import tkinter as tk

BG = "#1d2026"
FG = "#e8e8e8"
MUTED = "#9aa0a6"
ACCENT = "#4f9dff"
TRANSPARENT = "#ff00fe"  # color keyed out of the gaze dot window


def _make_click_through(win):
    if os.name != "nt":
        return
    import ctypes

    GWL_EXSTYLE = -20
    WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
    WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
    )


class GazeDot:
    SIZE = 22

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANSPARENT)
        if os.name == "nt":
            self.win.attributes("-transparentcolor", TRANSPARENT)
        c = tk.Canvas(self.win, width=self.SIZE, height=self.SIZE, bg=TRANSPARENT, highlightthickness=0)
        c.pack()
        c.create_oval(2, 2, self.SIZE - 2, self.SIZE - 2, outline=ACCENT, width=3)
        self.win.update_idletasks()
        _make_click_through(self.win)
        self.visible = True

    def move(self, x, y):
        if not self.visible:
            self.win.deiconify()
            self.visible = True
        h = self.SIZE // 2
        self.win.geometry(f"+{int(x) - h}+{int(y) - h}")

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False


class StatusPanel:
    def __init__(self, root, hotkeys, commands, after_click=None):
        """after_click(command) runs shortly after a button is clicked."""
        self.root = root

        def click(cmd):
            commands.put(cmd)
            if after_click:
                root.after(150, lambda: after_click(cmd))

        root.title("EyeTurn")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        self.mode = tk.Label(root, font=("Segoe UI", 13, "bold"), bg=BG, fg=FG, anchor="w")
        self.mode.pack(fill="x", padx=12, pady=(10, 0))
        # Long messages wrap instead of stretching the panel.
        self.state = tk.Label(root, font=("Segoe UI", 10), bg=BG, fg=MUTED, anchor="w",
                              justify="left", wraplength=280)
        self.state.pack(fill="x", padx=12)

        self.bar = tk.Canvas(root, width=260, height=10, bg="#2c3038", highlightthickness=0)
        self.bar.pack(padx=12, pady=8)
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 10, fill=ACCENT, width=0)

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", padx=8)
        for label, cmd in (("Pause", "pause"), ("Mode", "mode"), ("Calibrate", "recalibrate"),
                           ("Touch-up", "touchup")):
            tk.Button(
                buttons, text=label, command=lambda c=cmd: click(c),
                bg="#2c3038", fg=FG, activebackground="#3a3f49", activeforeground=FG,
                relief="flat", padx=8,
            ).pack(side="left", padx=4)

        # What controls reading: eyes, hands or head, one at a time (blue = on).
        toggles = tk.Frame(root, bg=BG)
        toggles.pack(fill="x", padx=8, pady=(6, 0))
        self.toggles = {}
        for name in ("Eyes", "Hands", "Head"):
            self.toggles[name] = tk.Button(
                toggles, command=lambda c=f"input_{name.lower()}": click(c), width=7,
                fg=FG, activebackground="#3a3f49", activeforeground=FG, relief="flat", padx=8,
            )
            self.toggles[name].pack(side="left", padx=4)

        keys = "   ".join(
            f"{k.replace('<', '').replace('>', '').upper()} {name}" for name, k in hotkeys.items()
        )
        tk.Label(
            root, text=keys, font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=270, justify="left"
        ).pack(fill="x", padx=12, pady=(8, 10))

        root.update_idletasks()
        root.geometry(f"+16+{root.winfo_screenheight() - root.winfo_reqheight() - 90}")

    def show_toggles(self, current, hands_available=True):
        """current: "eyes", "hands" or "head". Only that one is on (blue)."""
        for name, button in self.toggles.items():
            if name == "Hands" and not hands_available:
                button.config(text="Hands: n/a", bg="#2c3038", state="disabled")
            else:
                on = name.lower() == current
                button.config(text=name, bg=ACCENT if on else "#2c3038")

    def show(self, title, state, progress, warn=False):
        if self.mode.cget("text") != title:
            self.mode.config(text=title)
        self.state.config(text=state, fg="#ff8a80" if warn else MUTED)
        p = max(0.0, min(progress, 1.0))
        self.bar.coords(self.bar_fill, 0, 0, 260 * p, 10)
