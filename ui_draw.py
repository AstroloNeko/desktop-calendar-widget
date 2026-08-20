from __future__ import annotations

import math
import tkinter as tk
from typing import Iterable


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[index : index + 2], 16) for index in (0, 2, 4))


def blend(first: str, second: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    a = _rgb(first)
    b = _rgb(second)
    values = tuple(round(left + (right - left) * amount) for left, right in zip(a, b))
    return "#" + "".join(f"{value:02X}" for value in values)


def vertical_gradient(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    start: str,
    end: str,
    *,
    steps: int | None = None,
    tags: str | Iterable[str] = (),
) -> None:
    height = max(1, y2 - y1)
    count = max(2, min(steps or height, height))
    line_height = height / count
    for index in range(count):
        color = blend(start, end, index / max(1, count - 1))
        top = round(y1 + index * line_height)
        bottom = round(y1 + (index + 1) * line_height)
        canvas.create_rectangle(x1, top, x2, bottom + 1, fill=color, outline="", tags=tags)


def vertical_multi_gradient(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    stops: Iterable[tuple[float, str]],
    *,
    tags: str | Iterable[str] = (),
) -> None:
    ordered = sorted(stops, key=lambda item: item[0])
    if len(ordered) < 2:
        color = ordered[0][1] if ordered else "#FFFFFF"
        canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="", tags=tags)
        return
    height = max(1, y2 - y1)
    for offset in range(height):
        position = offset / max(1, height - 1)
        left = ordered[0]
        right = ordered[-1]
        for index in range(len(ordered) - 1):
            if ordered[index][0] <= position <= ordered[index + 1][0]:
                left, right = ordered[index], ordered[index + 1]
                break
        span = max(0.0001, right[0] - left[0])
        amount = (position - left[0]) / span
        color = blend(left[1], right[1], amount)
        canvas.create_line(x1, y1 + offset, x2, y1 + offset, fill=color, tags=tags)


def rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs,
) -> int:
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if radius <= 1:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    points = (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


def rounded_vertical_gradient(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    start: str,
    end: str,
    *,
    tags: str | Iterable[str] = (),
) -> None:
    top = math.ceil(y1)
    bottom = math.floor(y2)
    radius = max(1.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    height = max(1, bottom - top)
    for y in range(top, bottom + 1):
        position = (y - top) / height
        edge_distance = min(y - y1, y2 - y)
        inset = 0.0
        if edge_distance < radius:
            circle_y = radius - max(0.0, edge_distance)
            inset = radius - math.sqrt(max(0.0, radius * radius - circle_y * circle_y))
        canvas.create_line(
            round(x1 + inset),
            y,
            round(x2 - inset),
            y,
            fill=blend(start, end, position),
            tags=tags,
        )


def layered_panel(
    canvas: tk.Canvas,
    width: int,
    height: int,
    *,
    outer: str,
    inner: str,
    fill: str,
    highlight: str,
    radius: int,
) -> None:
    rounded_rectangle(canvas, 0, 0, width - 1, height - 1, radius, fill=outer, outline=outer)
    rounded_rectangle(canvas, 1, 1, width - 2, height - 2, max(1, radius - 1), fill=inner, outline=highlight)
    rounded_rectangle(canvas, 2, 2, width - 3, height - 3, max(1, radius - 2), fill=fill, outline=inner)


def bevel_control(
    canvas: tk.Canvas,
    width: int,
    height: int,
    *,
    background: str,
    border: str,
    highlight: str,
    radius: int,
    pressed: bool = False,
) -> None:
    rounded_rectangle(canvas, 0, 0, width - 1, height - 1, radius, fill=border, outline=border)
    rounded_rectangle(canvas, 1, 1, width - 2, height - 2, max(1, radius - 1), fill=background, outline="")
    if pressed:
        canvas.create_line(3, height - 3, width - 4, height - 3, fill=highlight)
        canvas.create_line(3, 3, width - 4, 3, fill=blend(background, border, 0.45))
    else:
        canvas.create_line(3, 2, width - 4, 2, fill=highlight)
        canvas.create_line(3, height - 3, width - 4, height - 3, fill=blend(background, border, 0.28))


def glossy_control(
    canvas: tk.Canvas,
    width: int,
    height: int,
    *,
    background: str,
    border: str,
    highlight: str,
    depth: str,
    radius: int,
    pressed: bool = False,
) -> None:
    """Draw a compact translucent-plastic control without moving its bounds."""
    left, top, right, bottom = 0, 0, max(1, width - 1), max(1, height - 1)
    inner_radius = max(2, radius - 1)
    gradient_start = (
        blend(background, depth, 0.22)
        if pressed
        else blend(background, highlight, 0.58)
    )
    gradient_end = (
        blend(background, highlight, 0.08)
        if pressed
        else blend(background, depth, 0.28)
    )
    rounded_vertical_gradient(
        canvas,
        left + 1,
        top + 1,
        right - 1,
        bottom - 1,
        inner_radius,
        gradient_start,
        gradient_end,
        tags="control_art",
    )
    rounded_rectangle(
        canvas,
        left,
        top,
        right,
        bottom,
        radius,
        fill="",
        outline=border,
        width=1,
        tags="control_art",
    )
    rounded_rectangle(
        canvas,
        left + 1,
        top + 1,
        right - 1,
        bottom - 1,
        inner_radius,
        fill="",
        outline=blend(border, highlight, 0.58 if not pressed else 0.28),
        width=1,
        tags="control_art",
    )
    shine_y = top + 3
    canvas.create_line(
        left + radius,
        shine_y,
        right - radius,
        shine_y,
        fill=blend(background, highlight, 0.76 if not pressed else 0.26),
        width=1,
        capstyle="round",
        tags="control_art",
    )
    canvas.create_line(
        left + radius,
        bottom - 2,
        right - radius,
        bottom - 2,
        fill=blend(background, depth, 0.44),
        width=1,
        capstyle="round",
        tags="control_art",
    )


def draw_bubble_motif(
    canvas: tk.Canvas,
    bubbles: Iterable[tuple[float, float, float]],
    *,
    outline: str,
    highlight: str,
    accent: str,
    tags: str | Iterable[str] = "bubble_art",
) -> None:
    """Draw deterministic glass bubbles suitable for static chrome decoration."""
    for center_x, center_y, radius in bubbles:
        if radius < 2:
            continue
        canvas.create_oval(
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
            fill="",
            outline=outline,
            width=1,
            tags=tags,
        )
        canvas.create_arc(
            center_x - radius + 1,
            center_y - radius + 1,
            center_x + radius - 1,
            center_y + radius - 1,
            start=48,
            extent=94,
            style="arc",
            outline=highlight,
            width=1,
            tags=tags,
        )
        glint = max(1, round(radius * 0.18))
        canvas.create_oval(
            center_x - radius * 0.36 - glint,
            center_y - radius * 0.35 - glint,
            center_x - radius * 0.36 + glint,
            center_y - radius * 0.35 + glint,
            fill=highlight,
            outline="",
            tags=tags,
        )
        if radius >= 6:
            canvas.create_arc(
                center_x - radius + 2,
                center_y - radius + 2,
                center_x + radius - 2,
                center_y + radius - 2,
                start=214,
                extent=52,
                style="arc",
                outline=accent,
                width=1,
                tags=tags,
            )


def draw_ecology_horizon(
    canvas: tk.Canvas,
    width: int,
    height: int,
    *,
    background: str,
    haze: str,
    horizon: str,
    highlight: str,
    accent: str,
    tags: str | Iterable[str] = "environment_art",
) -> None:
    """Blend a cool surface into a soft ecological haze at the lower edge."""
    if width <= 1 or height <= 1:
        return
    lower_haze = blend(background, haze, 0.42)
    vertical_multi_gradient(
        canvas,
        0,
        0,
        width,
        height,
        (
            (0.0, background),
            (0.26, blend(background, haze, 0.03)),
            (0.56, blend(background, haze, 0.10)),
            (0.82, blend(background, haze, 0.24)),
            (1.0, lower_haze),
        ),
        tags=tags,
    )
    horizon_y = max(2, round(height * 0.88))
    canvas.create_line(
        0,
        horizon_y,
        round(width * 0.34),
        horizon_y - 1,
        round(width * 0.67),
        horizon_y,
        width,
        horizon_y - 1,
        fill=blend(lower_haze, horizon, 0.08),
        width=1,
        smooth=True,
        tags=tags,
    )
    canvas.create_line(
        round(width * 0.18),
        horizon_y - 3,
        round(width * 0.49),
        horizon_y - 4,
        round(width * 0.82),
        horizon_y - 3,
        fill=blend(lower_haze, highlight, 0.24),
        width=1,
        smooth=True,
        tags=tags,
    )

    # A very low-contrast side reflection hints at vegetation outside the
    # glass without drawing any countable blades or a landscape silhouette.
    side_glow = blend(lower_haze, accent, 0.08)
    canvas.create_line(
        0,
        height - 2,
        round(width * 0.10),
        height - 3,
        fill=side_glow,
        width=1,
        smooth=True,
        tags=tags,
    )
    canvas.create_line(
        round(width * 0.90),
        height - 3,
        width,
        height - 2,
        fill=side_glow,
        width=1,
        smooth=True,
        tags=tags,
    )


def _calendar_rounded_rectangle(
    canvas: tk.Canvas,
    left: int,
    top: int,
    right: int,
    bottom: int,
    radius: int,
    **kwargs,
) -> int:
    """Draw a bounded, pixel-symmetric rounded polygon for date states."""
    radius = max(1, min(int(radius), (right - left) // 2, (bottom - top) // 2))
    offsets = [round(math.sqrt(max(0, radius * radius - (radius - step) ** 2))) for step in range(radius + 1)]
    corners = (
        ((right - radius + offsets[step], top + step) for step in range(radius + 1)),
        ((right - radius + offsets[radius - step], bottom - radius + step) for step in range(radius + 1)),
        ((left + radius - offsets[step], bottom - step) for step in range(radius + 1)),
        ((left + radius - offsets[radius - step], top + radius - step) for step in range(radius + 1)),
    )
    points: list[int] = []
    for corner in corners:
        for point in corner:
            if points[-2:] != list(point):
                points.extend(point)
    return canvas.create_polygon(points, smooth=False, **kwargs)


def draw_calendar_date_state(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    fill: str,
    border: str,
    radius: int,
    gradient_start: str | None = None,
    gradient_end: str | None = None,
    inner_border: str | None = None,
    top_highlight: str | None = None,
    today_ring: str | None = None,
    tags: str | Iterable[str] = "date_state",
) -> None:
    """Draw a calendar state inside one stable, integer bounding box.

    Every optional layer is inset from the supplied bounds.  In particular,
    the today ring never expands the selected body toward the lower-right,
    which keeps the helper suitable for compact date cells at any DPI scale.
    """
    left, right = sorted((round(x1), round(x2)))
    top, bottom = sorted((round(y1), round(y2)))
    if right <= left or bottom <= top:
        return

    # Canvas outlines are centred on their path. Tk reserves roughly two
    # pixels around polygon outlines in its item bbox,
    # even at width=1.  A two-pixel path inset keeps that reservation inside
    # the public date-state bounds at scaled and unscaled DPI settings.
    edge_inset = 2
    body_inset = edge_inset + (1 if today_ring else 0)
    if today_ring:
        _calendar_rounded_rectangle(
            canvas,
            left + edge_inset,
            top + edge_inset,
            right - edge_inset,
            bottom - edge_inset,
            max(2, radius - edge_inset),
            fill="",
            outline=today_ring,
            width=1,
            tags=tags,
        )

    body_left = left + body_inset
    body_top = top + body_inset
    body_right = right - body_inset
    body_bottom = bottom - body_inset
    body_radius = max(2, radius - body_inset)
    _calendar_rounded_rectangle(
        canvas,
        body_left,
        body_top,
        body_right,
        body_bottom,
        body_radius,
        fill=fill,
        outline="",
        tags=tags,
    )

    if gradient_start or gradient_end:
        rounded_vertical_gradient(
            canvas,
            body_left + 1,
            body_top + 1,
            body_right - 1,
            body_bottom - 1,
            max(1, body_radius - 1),
            gradient_start or fill,
            gradient_end or fill,
            tags=tags,
        )

    if inner_border and body_right - body_left > 4 and body_bottom - body_top > 4:
        _calendar_rounded_rectangle(
            canvas,
            body_left + 1,
            body_top + 1,
            body_right - 1,
            body_bottom - 1,
            max(1, body_radius - 1),
            fill="",
            outline=inner_border,
            width=1,
            tags=tags,
        )

    if top_highlight:
        highlight_y = body_top + 2
        canvas.create_line(
            body_left + body_radius,
            highlight_y,
            body_right - body_radius,
            highlight_y,
            fill=top_highlight,
            width=1,
            tags=tags,
        )

    _calendar_rounded_rectangle(
        canvas,
        body_left,
        body_top,
        body_right,
        body_bottom,
        body_radius,
        fill="",
        outline=border,
        width=1,
        tags=tags,
    )


def draw_calendar_date_ring(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    color: str,
    radius: int,
    inner_highlight: str | None = None,
    tags: str | Iterable[str] = "date_marker",
) -> None:
    """Draw a thin status ring around, never over, the date-state body."""
    left, right = sorted((round(x1), round(x2)))
    top, bottom = sorted((round(y1), round(y2)))
    if right <= left or bottom <= top:
        return

    # Keep Tk's outline reservation within the public box.  The optional
    # highlight is inset as well, so DDL + selected/today never grows a tail.
    edge_inset = 2
    _calendar_rounded_rectangle(
        canvas,
        left + edge_inset,
        top + edge_inset,
        right - edge_inset,
        bottom - edge_inset,
        max(2, radius - edge_inset),
        fill="",
        outline=color,
        width=1,
        tags=tags,
    )
    if inner_highlight and right - left > 8 and bottom - top > 8:
        _calendar_rounded_rectangle(
            canvas,
            left + edge_inset + 1,
            top + edge_inset + 1,
            right - edge_inset - 1,
            bottom - edge_inset - 1,
            max(1, radius - edge_inset - 1),
            fill="",
            outline=inner_highlight,
            width=1,
            tags=tags,
        )


def draw_calendar_today_accent(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    color: str,
    highlight: str | None = None,
    tags: str | Iterable[str] = "date_today_marker",
) -> None:
    """Add a compact warm marker inside a selected date without another ring."""
    left, right = sorted((round(x1), round(x2)))
    top, bottom = sorted((round(y1), round(y2)))
    if right - left < 10 or bottom <= top:
        return
    center = (left + right) // 2
    marker_y = top + 3
    if highlight:
        canvas.create_line(
            center - 4,
            marker_y + 1,
            center + 4,
            marker_y + 1,
            fill=highlight,
            width=1,
            capstyle="round",
            tags=tags,
        )
    canvas.create_line(
        center - 4,
        marker_y,
        center + 4,
        marker_y,
        fill=color,
        width=2,
        capstyle="round",
        tags=tags,
    )
