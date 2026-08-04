from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional


IS_WINDOWS = os.name == "nt"
ERROR_ALREADY_EXISTS = 183


def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class SingleInstance:
    def __init__(self, name: str = "Local\\DesktopCalendarWidgetV2") -> None:
        self.handle: Optional[int] = None
        self.already_running = False
        if not IS_WINDOWS:
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.SetLastError(0)
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = kernel32.GetLastError() == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and IS_WINDOWS:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def window_handle(widget) -> Optional[int]:
    if not IS_WINDOWS:
        return None
    widget.update_idletasks()
    child = int(widget.winfo_id())
    ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
    root = ctypes.windll.user32.GetAncestor(child, 2)  # GA_ROOT
    return int(root or child)


def make_tool_window(widget) -> None:
    if not IS_WINDOWS:
        return
    hwnd = window_handle(widget)
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.restype = ctypes.c_ssize_t
    set_long.restype = ctypes.c_ssize_t
    index = -20  # GWL_EXSTYLE
    style = get_long(hwnd, index)
    style = (style | 0x00000080) & ~0x00040000  # WS_EX_TOOLWINDOW, no WS_EX_APPWINDOW
    set_long(hwnd, index, style)
    flags = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
    try:
        # Windows 11 rounded corners; harmlessly ignored on older Windows.
        preference = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
    except (AttributeError, OSError):
        pass


def _desktop_host() -> Optional[int]:
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    user32.FindWindowExW.restype = ctypes.c_void_p
    user32.GetShellWindow.restype = ctypes.c_void_p
    user32.GetWindow.restype = ctypes.c_void_p
    result = ctypes.c_void_p()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam) -> bool:
        view = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if view:
            result.value = hwnd
            return False
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return int(result.value) if result.value else int(user32.GetShellWindow() or 0) or None


def send_to_desktop(widget) -> None:
    """Place a tool window below normal applications but above the desktop surface."""
    if not IS_WINDOWS:
        try:
            widget.lower()
        except Exception:
            pass
        return
    hwnd = window_handle(widget)
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # Leave the topmost band first.
    host = _desktop_host()
    if host:
        above_desktop = int(user32.GetWindow(host, 3) or 0)  # GW_HWNDPREV
        if above_desktop and above_desktop != hwnd:
            # Insert immediately above Explorer's desktop host and below normal apps.
            user32.SetWindowPos(hwnd, above_desktop, 0, 0, 0, 0, flags)
            return
        if above_desktop == hwnd:
            return
    user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, flags)  # Conservative fallback: HWND_BOTTOM.


def clamp_to_work_area(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Keep a saved gadget position visible on the nearest monitor, including negative coordinates."""
    if not IS_WINDOWS:
        return max(4, x), max(4, y)

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", Rect),
            ("rcWork", Rect),
            ("dwFlags", ctypes.c_ulong),
        ]

    user32 = ctypes.windll.user32
    user32.MonitorFromRect.restype = ctypes.c_void_p
    rectangle = Rect(x, y, x + width, y + height)
    monitor = user32.MonitorFromRect(ctypes.byref(rectangle), 2)  # MONITOR_DEFAULTTONEAREST
    if not monitor:
        return x, y
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return x, y
    work = info.rcWork
    max_x = max(work.left, work.right - width)
    max_y = max(work.top, work.bottom - height)
    return max(work.left + 4, min(x, max_x - 4)), max(work.top + 4, min(y, max_y - 4))


def startup_file() -> Path:
    base = Path(os.getenv("APPDATA", str(Path.home())))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "DesktopCalendar.vbs"


def is_autostart_enabled() -> bool:
    return startup_file().exists()


def set_autostart(enabled: bool, app_script: Path) -> None:
    target = startup_file()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        escaped = str(app_script.resolve()).replace('"', '""')
        pyw = str(Path(os.getenv("SystemRoot", "C:\\Windows")) / "pyw.exe").replace('"', '""')
        content = (
            'Set shell = CreateObject("WScript.Shell")\n'
            f'shell.Run """{pyw}"" -3 ""{escaped}""", 0, False\n'
        )
        target.write_text(content, encoding="utf-16")
    elif target.exists():
        target.unlink()
