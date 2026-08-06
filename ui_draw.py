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
