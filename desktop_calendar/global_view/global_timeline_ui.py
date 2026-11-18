from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from timeline_model import TimelineItem


@dataclass(frozen=True)
class GlobalTimelineLayout:
    """Logical sizes shared by the Global workspace and its tests."""

    label_width: int = 216
    detail_width: int = 256
    date_header_height: int = 54
    row_height: int = 38
    day_min_width: int = 34
    detail_hide_below: int = 980
    detail_min_width: int = 220

    def show_detail_panel(self, logical_width: int) -> bool:
        return logical_width >= self.detail_hide_below

    def detail_panel_width(self, logical_width: int) -> int:
        """Let the optional inspector grow gently instead of jumping to full width."""
        available_growth = max(0, logical_width - self.detail_hide_below)
        return min(self.detail_width, self.detail_min_width + available_growth // 5)

    def day_width(self, viewport_width: int, day_count: int) -> int:
        if day_count <= 0:
            return self.day_min_width
        return max(self.day_min_width, viewport_width // day_count)


GLOBAL_TIMELINE_LAYOUT = GlobalTimelineLayout()


def primary_action_for_view(view_mode: str) -> str:
    return "create" if view_mode == "global" else "day_detail"


def canvas_day_at(
    canvas_x: float,
    *,
    period_start: date,
    day_width: int,
    day_count: int,
) -> Optional[date]:
    """Resolve a scrolled Timeline x-coordinate to a date safely."""
    if day_width <= 0 or day_count <= 0 or canvas_x < 0:
        return None
    index = int(canvas_x // day_width)
    if not 0 <= index < day_count:
        return None
    return period_start.fromordinal(period_start.toordinal() + index)


def wheel_units(delta: int) -> int:
    if delta == 0:
        return 0
    return -1 if delta > 0 else 1


def timeline_bar_label(title: str, logical_width: int) -> str:
    """Fit a concise label inside a Timeline segment without crossing its bounds."""
    usable_width = logical_width - 18
    if usable_width < 28:
        return ""
    character_limit = usable_width // 7
    if character_limit < 4:
        return ""
    if len(title) <= character_limit:
        return title
    return title[: character_limit - 1] + "…"


def timeline_type_label(item: TimelineItem) -> str:
    return {"general": "一般", "urgent": "紧急", "ddl": "DDL"}.get(item.task_type, "一般")


def timeline_tooltip_text(item: TimelineItem) -> str:
    date_text = (
        item.start_date.strftime("%Y-%m-%d")
        if item.start_date == item.end_date
        else f"{item.start_date:%Y-%m-%d} → {item.end_date:%Y-%m-%d}"
    )
    lines = [item.title, date_text, f"{item.effective_days_count} 个有效工作日", f"类型：{timeline_type_label(item)}"]
    if item.ddl_date:
        lines.append(f"DDL：{item.ddl_date:%Y-%m-%d}")
    return "\n".join(lines)


def detail_state_text(item: TimelineItem) -> str:
    if item.completed:
        return "已完成"
    today = date.today()
    if item.ddl_date and item.ddl_date < today:
        return "已逾期"
    if item.start_date > today:
        return "未开始"
    return "进行中"
