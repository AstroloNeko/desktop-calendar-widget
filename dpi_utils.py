from __future__ import annotations

import ctypes
import math
import os
import tkinter as tk
import weakref
from dataclasses import dataclass
from typing import Iterable, Optional


BASE_DPI = 96
IS_WINDOWS = os.name == "nt"
PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2

_active_dpi = BASE_DPI


def _context_matches(value: int) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        context = user32.GetThreadDpiAwarenessContext()
        return bool(user32.AreDpiAwarenessContextsEqual(ctypes.c_void_p(context), ctypes.c_void_p(value)))
    except (AttributeError, OSError):
        return False


def dpi_awareness_mode() -> str:
    if not IS_WINDOWS:
        return "unsupported"
    contexts = (
        (-4, "per_monitor_v2"),
        (-3, "per_monitor_v1"),
        (-2, "system_aware"),
        (-5, "unaware_gdi_scaled"),
        (-1, "unaware"),
    )
    for value, name in contexts:
        if _context_matches(value):
            return name
    return "unknown"


def enable_dpi_awareness() -> str:
    """Enable the best Windows DPI mode before the first Tk window exists."""
    if not IS_WINDOWS:
        return "unsupported"
    current = dpi_awareness_mode()
    if current in ("per_monitor_v2", "per_monitor_v1"):
        return current
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        result = ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        if result in (0, -2147024891):  # S_OK or E_ACCESSDENIED (manifest/already configured)
            current = dpi_awareness_mode()
            if current != "unknown":
                return current
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system_aware"
    except (AttributeError, OSError):
        pass
    return dpi_awareness_mode()


def set_active_dpi(dpi: int) -> int:
    global _active_dpi
    _active_dpi = max(BASE_DPI, int(dpi or BASE_DPI))
    return _active_dpi


def active_dpi() -> int:
    return _active_dpi


def scale_factor(dpi: Optional[int] = None) -> float:
    return max(BASE_DPI, int(dpi or _active_dpi)) / BASE_DPI


def scale_px(value: float, dpi: Optional[int] = None) -> int:
    """Round a logical offset symmetrically onto the device-pixel grid."""
    scaled = abs(float(value)) * scale_factor(dpi)
    rounded = math.floor(scaled + 0.5)
    return -rounded if value < 0 else rounded


def unscale_px(value: float, dpi: Optional[int] = None) -> int:
    unscaled = abs(float(value)) / scale_factor(dpi)
    rounded = math.floor(unscaled + 0.5)
    return -rounded if value < 0 else rounded


def scale_line_width(value: float = 1, dpi: Optional[int] = None) -> int:
    return max(1, scale_px(value, dpi))


def scaled_geometry(width: int, height: int, x: int, y: int, dpi: Optional[int] = None) -> str:
    return f"{scale_px(width, dpi)}x{scale_px(height, dpi)}{int(x):+d}{int(y):+d}"


def window_handle(widget: tk.Misc) -> Optional[int]:
    if not IS_WINDOWS:
        return None
    try:
        widget.update_idletasks()
        child = int(widget.winfo_id())
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = ctypes.c_void_p
        root = user32.GetAncestor(child, 2)  # GA_ROOT
        return int(root or child)
    except (AttributeError, OSError, tk.TclError, ValueError):
        return None


def dpi_for_window(widget: tk.Misc, fallback: int = BASE_DPI) -> int:
    if not IS_WINDOWS:
        return fallback
    hwnd = window_handle(widget)
    if hwnd:
        try:
            user32 = ctypes.windll.user32
            user32.GetDpiForWindow.restype = ctypes.c_uint
            dpi = int(user32.GetDpiForWindow(hwnd))
            if dpi:
                return dpi
        except (AttributeError, OSError):
            pass
    return system_dpi(fallback)


def system_dpi(fallback: int = BASE_DPI) -> int:
    if not IS_WINDOWS:
        return fallback
    try:
        user32 = ctypes.windll.user32
        user32.GetDpiForSystem.restype = ctypes.c_uint
        return int(user32.GetDpiForSystem()) or fallback
    except (AttributeError, OSError):
        return fallback


@dataclass(frozen=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def work_area_for_window(widget: tk.Misc) -> WorkArea:
    if not IS_WINDOWS:
        return WorkArea(0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", Rect), ("rcWork", Rect), ("dwFlags", ctypes.c_ulong)]

    hwnd = window_handle(widget)
    if not hwnd:
        return WorkArea(0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(MonitorInfo)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return WorkArea(0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    return WorkArea(info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)


def work_area_for_rect(x: int, y: int, width: int, height: int) -> WorkArea:
    """Return the work area nearest a saved device-pixel rectangle."""
    if not IS_WINDOWS:
        return WorkArea(x, y, x + max(1, width), y + max(1, height))

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", Rect), ("rcWork", Rect), ("dwFlags", ctypes.c_ulong)]

    user32 = ctypes.windll.user32
    user32.MonitorFromRect.restype = ctypes.c_void_p
    rectangle = Rect(x, y, x + max(1, width), y + max(1, height))
    monitor = user32.MonitorFromRect(ctypes.byref(rectangle), 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(MonitorInfo)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return WorkArea(x, y, x + max(1, width), y + max(1, height))
    return WorkArea(info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)


class DpiManager:
    def __init__(self, root: tk.Misc, dpi: Optional[int] = None) -> None:
        self.root = root
        self.dpi = max(BASE_DPI, int(dpi or dpi_for_window(root)))
        self._canvases: weakref.WeakSet[LogicalCanvas] = weakref.WeakSet()
        self.apply(self.dpi, refresh_canvases=False)

    @property
    def scale(self) -> float:
        return scale_factor(self.dpi)

    def px(self, value: float) -> int:
        return scale_px(value, self.dpi)

    def logical(self, value: float) -> int:
        return unscale_px(value, self.dpi)

    def line(self, value: float = 1) -> int:
        return scale_line_width(value, self.dpi)

    def apply(self, dpi: int, *, refresh_canvases: bool = True) -> bool:
        dpi = max(BASE_DPI, int(dpi or BASE_DPI))
        changed = dpi != self.dpi
        self.dpi = dpi
        set_active_dpi(dpi)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except (AttributeError, tk.TclError):
            pass
        if refresh_canvases:
            for canvas in tuple(self._canvases):
                canvas.refresh_dpi()
        return changed

    def current_window_dpi(self) -> int:
        return dpi_for_window(self.root, self.dpi)

    def work_area(self) -> WorkArea:
        return work_area_for_window(self.root)

    def register_canvas(self, canvas: "LogicalCanvas") -> None:
        self._canvases.add(canvas)


class LogicalCanvas(tk.Canvas):
    """Canvas whose public drawing coordinates stay in 96-DPI design units."""

    _PIXEL_OPTIONS = ("width", "height", "bd", "borderwidth", "highlightthickness")

    def __init__(self, master: tk.Misc, *, dpi: DpiManager, **kwargs) -> None:
        self.dpi = dpi
        self._logical_options: dict[str, float] = {}
        scaled = dict(kwargs)
        for name in self._PIXEL_OPTIONS:
            if name in scaled and isinstance(scaled[name], (int, float)):
                self._logical_options[name] = float(scaled[name])
                scaled[name] = self.dpi.px(scaled[name])
        super().__init__(master, **scaled)
        self.dpi.register_canvas(self)

    def logical_width(self) -> int:
        actual = super().winfo_width()
        if actual <= 1 and "width" in self._logical_options:
            return max(1, round(self._logical_options["width"]))
        return max(1, self.dpi.logical(actual))

    def logical_height(self) -> int:
        actual = super().winfo_height()
        if actual <= 1 and "height" in self._logical_options:
            return max(1, round(self._logical_options["height"]))
        return max(1, self.dpi.logical(actual))

    def refresh_dpi(self) -> None:
        if not self.winfo_exists():
            return
        if self._logical_options:
            super().configure(**{name: self.dpi.px(value) for name, value in self._logical_options.items()})
        try:
            self.event_generate("<Configure>")
        except tk.TclError:
            pass

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        scaled = dict(kwargs)
        for name in self._PIXEL_OPTIONS:
            if name in scaled and isinstance(scaled[name], (int, float)):
                self._logical_options[name] = float(scaled[name])
                scaled[name] = self.dpi.px(scaled[name])
        return super().configure(**scaled)

    config = configure

    def _coords(self, values: Iterable) -> list[int]:
        flattened: list = []
        for value in values:
            if isinstance(value, (list, tuple)):
                flattened.extend(value)
            else:
                flattened.append(value)
        return [self.dpi.px(value) for value in flattened]

    def _item_options(self, options: dict, *, default_line: bool = False) -> dict:
        result = dict(options)
        if "width" in result and isinstance(result["width"], (int, float)):
            result["width"] = self.dpi.line(result["width"])
        elif default_line:
            result["width"] = self.dpi.line()
        return result

    def create_line(self, *args, **kwargs):
        return super().create_line(*self._coords(args), **self._item_options(kwargs, default_line=True))

    def create_rectangle(self, *args, **kwargs):
        outlined = bool(kwargs.get("outline"))
        return super().create_rectangle(*self._coords(args), **self._item_options(kwargs, default_line=outlined))

    def create_oval(self, *args, **kwargs):
        outlined = bool(kwargs.get("outline"))
        return super().create_oval(*self._coords(args), **self._item_options(kwargs, default_line=outlined))

    def create_polygon(self, *args, **kwargs):
        outlined = bool(kwargs.get("outline"))
        return super().create_polygon(*self._coords(args), **self._item_options(kwargs, default_line=outlined))

    def create_text(self, *args, **kwargs):
        return super().create_text(*self._coords(args), **kwargs)

    def create_arc(self, *args, **kwargs):
        outlined = bool(kwargs.get("outline"))
        return super().create_arc(*self._coords(args), **self._item_options(kwargs, default_line=outlined))
