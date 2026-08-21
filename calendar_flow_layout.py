from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Optional

from timeline_model import TimelineItem, TimelineMonth


GLOBAL_DISPLAY_MODES = ("flow", "timeline")
DEFAULT_GLOBAL_DISPLAY_MODE = "timeline"
FLOW_CARD_COMPACT_MAX_WIDTH = 116
FLOW_CARD_MEDIUM_MAX_WIDTH = 228


def normalize_global_display_mode(value: object) -> str:
    return value if isinstance(value, str) and value in GLOBAL_DISPLAY_MODES else DEFAULT_GLOBAL_DISPLAY_MODE


def flow_card_detail_level(logical_width: int, *, span_columns: int = 1) -> str:
    """Select text density without coupling the renderer to task semantics."""
    if span_columns <= 1 or logical_width <= FLOW_CARD_COMPACT_MAX_WIDTH:
        return "compact"
    if logical_width <= FLOW_CARD_MEDIUM_MAX_WIDTH:
        return "medium"
    return "large"


def normalize_flow_drag_range(first: date, last: date) -> tuple[date, date, int]:
    start_date, end_date = sorted((first, last))
    return start_date, end_date, (end_date - start_date).days + 1


@dataclass(frozen=True)
class CalendarFlowBlock:
    """One visible weekly piece that still points at the canonical TimelineItem."""

    item: TimelineItem
    week_index: int
    start_column: int
    end_column: int
    lane: int
    start_date: date
    end_date: date
    ddl_date: Optional[date]
    continues_before: bool
    continues_after: bool
    visible: bool


@dataclass(frozen=True)
class CalendarFlowWeek:
    index: int
    dates: tuple[date, ...]
    blocks: tuple[CalendarFlowBlock, ...]
    hidden_counts: tuple[int, ...]

    def hidden_on(self, column: int) -> int:
        return self.hidden_counts[column] if 0 <= column < len(self.hidden_counts) else 0


@dataclass(frozen=True)
class CalendarFlowLayout:
    year: int
    month: int
    weeks: tuple[CalendarFlowWeek, ...]
    max_visible_lanes: int

    def block_by_item_id(self, item_id: str) -> tuple[CalendarFlowBlock, ...]:
        return tuple(block for week in self.weeks for block in week.blocks if block.item.id == item_id)


@dataclass(frozen=True)
class _BlockCandidate:
    item: TimelineItem
    week_index: int
    start_column: int
    end_column: int
    start_date: date
    end_date: date
    continues_before: bool
    continues_after: bool


def _candidate_blocks(model: TimelineMonth, weeks: list[list[date]]) -> list[list[_BlockCandidate]]:
    week_for_day = {day: (week_index, column) for week_index, week in enumerate(weeks) for column, day in enumerate(week)}
    candidates: list[list[_BlockCandidate]] = [[] for _week in weeks]
    for item in model.items:
        for segment in item.segments:
            dates_by_week: dict[int, list[date]] = {}
            for day in segment.dates:
                location = week_for_day.get(day)
                if location is not None:
                    dates_by_week.setdefault(location[0], []).append(day)
            for week_index, dates in sorted(dates_by_week.items()):
                start_date, end_date = dates[0], dates[-1]
                start_column = week_for_day[start_date][1]
                end_column = week_for_day[end_date][1]
                candidates[week_index].append(
                    _BlockCandidate(
                        item=item,
                        week_index=week_index,
                        start_column=start_column,
                        end_column=end_column,
                        start_date=start_date,
                        end_date=end_date,
                        continues_before=(
                            segment.start_date < start_date
                            or item.continues_from_previous_period and start_date == model.period_start
                        ),
                        continues_after=(
                            segment.end_date > end_date
                            or item.continues_to_next_period and end_date == model.period_end
                        ),
                    )
                )
    return candidates


def build_calendar_flow_layout(model: TimelineMonth, *, max_visible_lanes: int = 3) -> CalendarFlowLayout:
    """Project the existing Timeline model into weekly lanes without recalculating task dates."""
    max_visible_lanes = max(1, max_visible_lanes)
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(model.year, model.month)
    candidates = _candidate_blocks(model, weeks)
    flow_weeks: list[CalendarFlowWeek] = []
    for week_index, week in enumerate(weeks):
        lane_ends: list[int] = []
        blocks: list[CalendarFlowBlock] = []
        hidden_counts = [0] * 7
        # Interval packing must proceed from left to right.  Timeline items
        # remain stably ordered when they start on the same day, while cards
        # that do not overlap can reliably reuse a lane.
        for candidate in sorted(candidates[week_index], key=lambda value: value.start_column):
            lane = next(
                (index for index, last_column in enumerate(lane_ends) if last_column < candidate.start_column),
                len(lane_ends),
            )
            if lane == len(lane_ends):
                lane_ends.append(candidate.end_column)
            else:
                lane_ends[lane] = candidate.end_column
            visible = lane < max_visible_lanes
            if not visible:
                for column in range(candidate.start_column, candidate.end_column + 1):
                    hidden_counts[column] += 1
            ddl_date = (
                candidate.item.ddl_date
                if candidate.item.ddl_date and candidate.start_date <= candidate.item.ddl_date <= candidate.end_date
                else None
            )
            blocks.append(
                CalendarFlowBlock(
                    item=candidate.item,
                    week_index=week_index,
                    start_column=candidate.start_column,
                    end_column=candidate.end_column,
                    lane=lane,
                    start_date=candidate.start_date,
                    end_date=candidate.end_date,
                    ddl_date=ddl_date,
                    continues_before=candidate.continues_before,
                    continues_after=candidate.continues_after,
                    visible=visible,
                )
            )
        flow_weeks.append(
            CalendarFlowWeek(
                index=week_index,
                dates=tuple(week),
                blocks=tuple(blocks),
                hidden_counts=tuple(hidden_counts),
            )
        )
    return CalendarFlowLayout(model.year, model.month, tuple(flow_weeks), max_visible_lanes)


def flow_date_range_text(item: TimelineItem) -> str:
    if item.start_date == item.end_date:
        return f"{item.start_date.month}.{item.start_date.day}"
    return f"{item.start_date.month}.{item.start_date.day}-{item.end_date.month}.{item.end_date.day}"


def flow_day_at(
    canvas_x: float,
    canvas_y: float,
    *,
    layout: CalendarFlowLayout,
    column_width: int,
    week_height: int,
) -> Optional[date]:
    """Map scrolled Canvas coordinates to a displayed calendar date."""
    if canvas_x < 0 or canvas_y < 0 or column_width <= 0 or week_height <= 0:
        return None
    column = int(canvas_x // column_width)
    week_index = int(canvas_y // week_height)
    if not (0 <= week_index < len(layout.weeks) and 0 <= column < 7):
        return None
    return layout.weeks[week_index].dates[column]
