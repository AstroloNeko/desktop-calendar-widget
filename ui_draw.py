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


def glass_date_state(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    fill: str,
    border: str,
    highlight: str,
    radius: int,
    outer_ring: str | None = None,
    gradient_start: str | None = None,
    gradient_end: str | None = None,
    inner_border: str | None = None,
) -> None:
    if outer_ring:
        rounded_rectangle(canvas, x1 - 1, y1 - 1, x2 + 1, y2 + 1, radius + 1, fill="", outline=outer_ring, width=1)
    if gradient_start or gradient_end or inner_border:
        rounded_vertical_gradient(
            canvas,
            x1 + 1,
            y1 + 1,
            x2 - 1,
            y2 - 1,
            max(1, radius - 1),
            gradient_start or fill,
            gradient_end or fill,
        )
        rounded_rectangle(canvas, x1, y1, x2, y2, radius, fill="", outline=border, width=1)
        canvas.create_line(
            x1 + radius + 2,
            y1 + 2,
            x2 - radius - 2,
            y1 + 2,
            fill=blend(inner_border or fill, highlight, 0.46),
            width=1,
        )
        return
    rounded_rectangle(canvas, x1, y1, x2, y2, radius, fill=fill, outline=border, width=1)
    canvas.create_line(x1 + radius, y1 + 2, x2 - radius, y1 + 2, fill=highlight, width=1)


def flat_date_state(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    fill: str,
    border: str,
    radius: int,
    outer_ring: str | None = None,
) -> None:
    if outer_ring:
        rounded_rectangle(
            canvas,
            x1 - 1,
            y1 - 1,
            x2 + 1,
            y2 + 1,
            radius + 1,
            fill="",
            outline=outer_ring,
            width=1,
        )
    rounded_rectangle(canvas, x1, y1, x2, y2, radius, fill=fill, outline=border, width=1)
