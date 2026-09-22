from __future__ import annotations

import ctypes
from ctypes import wintypes
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
MAX_WORK_AREA_COORDINATE = 1_000_000
MAX_WORK_AREA_DIMENSION = 65_536

_active_dpi = BASE_DPI


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _win32_function(function, argtypes: list[type], restype: type):
    """Declare the full Win32 signature before passing Python integers as handles."""
    function.argtypes = argtypes
    function.restype = restype
    return function


def _context_matches(value: int) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        get_context = _win32_function(user32.GetThreadDpiAwarenessContext, [], wintypes.HANDLE)
        contexts_equal = _win32_function(
            user32.AreDpiAwarenessContextsEqual,
            [wintypes.HANDLE, wintypes.HANDLE],
            wintypes.BOOL,
        )
        return bool(contexts_equal(get_context(), wintypes.HANDLE(value)))
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
        set_context = _win32_function(
            ctypes.windll.user32.SetProcessDpiAwarenessContext,
            [wintypes.HANDLE],
            wintypes.BOOL,
        )
        if set_context(wintypes.HANDLE(PER_MONITOR_AWARE_V2)):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        set_awareness = _win32_function(
            ctypes.windll.shcore.SetProcessDpiAwareness,
            [ctypes.c_int],
            ctypes.c_long,
        )
        result = set_awareness(PROCESS_PER_MONITOR_DPI_AWARE)
        if result in (0, -2147024891):  # S_OK or E_ACCESSDENIED (manifest/already configured)
            current = dpi_awareness_mode()
            if current != "unknown":
                return current
    except (AttributeError, OSError):
        pass
    try:
        set_aware = _win32_function(ctypes.windll.user32.SetProcessDPIAware, [], wintypes.BOOL)
        if set_aware():
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
        get_ancestor = _win32_function(user32.GetAncestor, [wintypes.HWND, wintypes.UINT], wintypes.HWND)
        root = get_ancestor(wintypes.HWND(child), 2)  # GA_ROOT
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
            get_dpi = _win32_function(user32.GetDpiForWindow, [wintypes.HWND], wintypes.UINT)
            dpi = int(get_dpi(wintypes.HWND(hwnd)))
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
        get_dpi = _win32_function(user32.GetDpiForSystem, [], wintypes.UINT)
        return int(get_dpi()) or fallback
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
    fallback = WorkArea(0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    hwnd = window_handle(widget)
    if not hwnd:
        return fallback
    user32 = ctypes.windll.user32
    monitor_from_window = _win32_function(
        user32.MonitorFromWindow, [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
    )
    monitor = monitor_from_window(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    return _monitor_work_area(user32, monitor, fallback)


def _monitor_work_area(user32, monitor: int | None, fallback: WorkArea) -> WorkArea:
    if not monitor:
        return fallback
    get_monitor_info = _win32_function(
        user32.GetMonitorInfoW,
        [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)],
        wintypes.BOOL,
    )
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(MonitorInfo)
    if not get_monitor_info(wintypes.HANDLE(monitor), ctypes.byref(info)):
        return fallback
    work = info.rcWork
    if (
        not all(
            -MAX_WORK_AREA_COORDINATE <= edge <= MAX_WORK_AREA_COORDINATE
            for edge in (work.left, work.top, work.right, work.bottom)
        )
        or not 0 < work.right - work.left <= MAX_WORK_AREA_DIMENSION
        or not 0 < work.bottom - work.top <= MAX_WORK_AREA_DIMENSION
    ):
        return fallback
    return WorkArea(work.left, work.top, work.right, work.bottom)


def work_area_for_rect(x: int, y: int, width: int, height: int) -> WorkArea:
    """Return the work area nearest a saved device-pixel rectangle."""
    if not IS_WINDOWS:
        return WorkArea(x, y, x + max(1, width), y + max(1, height))

    user32 = ctypes.windll.user32
    rectangle = wintypes.RECT(x, y, x + max(1, width), y + max(1, height))
    fallback = WorkArea(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
    monitor_from_rect = _win32_function(
        user32.MonitorFromRect, [ctypes.POINTER(wintypes.RECT), wintypes.DWORD], wintypes.HANDLE
    )
    monitor = monitor_from_rect(ctypes.byref(rectangle), 2)  # MONITOR_DEFAULTTONEAREST
    return _monitor_work_area(user32, monitor, fallback)


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
