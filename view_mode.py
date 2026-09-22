from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from dpi_utils import WorkArea, scale_px, unscale_px


VIEW_MODES = ("compact", "global")
MAX_SAVED_DIMENSION = 16384
MAX_SAVED_COORDINATE = 1_000_000


def saved_window_coordinate(value: object) -> Optional[int]:
    """Accept a signed device-pixel position without permitting corrupt Win32 coordinates."""
    if isinstance(value, bool):
        return None
    try:
        coordinate = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and coordinate != value:
        return None
    return coordinate if -MAX_SAVED_COORDINATE <= coordinate <= MAX_SAVED_COORDINATE else None


def normalize_view_mode(value: object) -> str:
    return value if isinstance(value, str) and value in VIEW_MODES else "compact"


@dataclass(frozen=True)
class WindowGeometry:
    """Logical size plus device-pixel position for Per-Monitor DPI restoration."""

    width: int
    height: int
    x: int
    y: int

    def as_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height, "x": self.x, "y": self.y}

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        minimum_width: int = 1,
        minimum_height: int = 1,
    ) -> Optional["WindowGeometry"]:
        if not isinstance(value, Mapping):
            return None
        try:
            x = saved_window_coordinate(value["x"])
            y = saved_window_coordinate(value["y"])
            width = saved_window_coordinate(value["width"])
            height = saved_window_coordinate(value["height"])
            if x is None or y is None or width is None or height is None:
                return None
            geometry = cls(width=width, height=height, x=x, y=y)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (
            geometry.width < minimum_width
            or geometry.height < minimum_height
            or geometry.width > MAX_SAVED_DIMENSION
            or geometry.height > MAX_SAVED_DIMENSION
        ):
            return None
        return geometry


def fit_geometry_to_work_area(
    geometry: WindowGeometry,
    work_area: WorkArea,
    dpi: int,
    *,
    margin: int = 8,
) -> WindowGeometry:
    """Fit a logical window size and physical position inside one work area."""
    margin_px = scale_px(margin, dpi)
    max_device_width = max(1, work_area.width - margin_px * 2)
    max_device_height = max(1, work_area.height - margin_px * 2)
    width = min(geometry.width, max(1, unscale_px(max_device_width, dpi)))
    height = min(geometry.height, max(1, unscale_px(max_device_height, dpi)))
    device_width = scale_px(width, dpi)
    device_height = scale_px(height, dpi)
    min_x = work_area.left + margin_px
    min_y = work_area.top + margin_px
    max_x = max(min_x, work_area.right - margin_px - device_width)
    max_y = max(min_y, work_area.bottom - margin_px - device_height)
    return WindowGeometry(
        width,
        height,
        max(min_x, min(geometry.x, max_x)),
        max(min_y, min(geometry.y, max_y)),
    )


def initial_global_geometry(work_area: WorkArea, dpi: int) -> WindowGeometry:
    """Use most, but not all, of the current monitor's usable work area."""
    width = max(720, unscale_px(round(work_area.width * 0.86), dpi))
    height = max(520, unscale_px(round(work_area.height * 0.80), dpi))
    device_width = scale_px(width, dpi)
    device_height = scale_px(height, dpi)
    geometry = WindowGeometry(
        width,
        height,
        work_area.left + (work_area.width - device_width) // 2,
        work_area.top + (work_area.height - device_height) // 2,
    )
    return fit_geometry_to_work_area(geometry, work_area, dpi)
