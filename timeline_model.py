from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from calendar_core import EVENT_TYPE_RANK, Event, Store
from holiday_data import holiday_for


@dataclass(frozen=True)
class TimelineDayMeta:
    date: date
    weekday: int
    is_weekend: bool
    is_legal_holiday: bool
    holiday_name: Optional[str]
    is_adjusted_workday: bool
    is_user_leave: bool
    is_user_holiday: bool
    is_workday: bool
    is_today: bool
    is_outside_month: bool = False


@dataclass(frozen=True)
class TimelineSegment:
    start_date: date
    end_date: date
    dates: tuple[date, ...]

    @property
    def days(self) -> int:
        return len(self.dates)


@dataclass(frozen=True)
class TimelineItem:
    id: str
    title: str
    color: str
    task_type: str
    completed: bool
    start_date: date
    end_date: date
    visible_start: date
    visible_end: date
    effective_dates: tuple[date, ...]
    visible_effective_dates: tuple[date, ...]
    skipped_dates: tuple[date, ...]
    segments: tuple[TimelineSegment, ...]
    ddl_date: Optional[date]
    is_urgent: bool
    native_ddl: bool
    end_as_ddl: bool
    continues_from_previous_period: bool
    continues_to_next_period: bool
    source_task_ids: tuple[str, ...]
    calendar_span_days: int
    effective_days_count: int
    created_at: str
    notes: str


@dataclass(frozen=True)
class TimelineMonth:
    year: int
    month: int
    period_start: date
    period_end: date
    days: tuple[TimelineDayMeta, ...]
    items: tuple[TimelineItem, ...]

    def item_by_id(self, item_id: str) -> Optional[TimelineItem]:
        return next((item for item in self.items if item.id == item_id), None)


class TimelineSelection:
    """Stable model selection; Canvas item IDs deliberately never enter this API."""

    def __init__(self) -> None:
        self.selected_item_id: Optional[str] = None

    def select(self, item_id: str, model: TimelineMonth) -> Optional[TimelineItem]:
        item = model.item_by_id(item_id)
        self.selected_item_id = item.id if item else None
        return item

    def get(self, model: TimelineMonth) -> Optional[TimelineItem]:
        if not self.selected_item_id:
            return None
        item = model.item_by_id(self.selected_item_id)
        if item is None:
            self.selected_item_id = None
        return item

    def clear(self) -> None:
        self.selected_item_id = None


def _segments_for(dates: tuple[date, ...]) -> tuple[TimelineSegment, ...]:
    if not dates:
        return ()
    result: list[TimelineSegment] = []
    current: list[date] = [dates[0]]
    for day in dates[1:]:
        if day == current[-1] + timedelta(days=1):
            current.append(day)
            continue
        result.append(TimelineSegment(current[0], current[-1], tuple(current)))
        current = [day]
    result.append(TimelineSegment(current[0], current[-1], tuple(current)))
    return tuple(result)


def _day_meta(store: Store, day: date, today: date) -> TimelineDayMeta:
    holiday = holiday_for(day)
    custom_status = store.date_status(day)
    return TimelineDayMeta(
        date=day,
        weekday=day.weekday(),
        is_weekend=day.weekday() >= calendar.SATURDAY,
        is_legal_holiday=bool(holiday and holiday.kind == "day_off"),
        holiday_name=holiday.name if holiday else None,
        is_adjusted_workday=bool(holiday and holiday.kind == "workday"),
        is_user_leave=custom_status == "leave",
        is_user_holiday=custom_status == "holiday",
        is_workday=store.is_workday(day),
        is_today=day == today,
    )


def _timeline_item(store: Store, event: Event, period_start: date, period_end: date) -> Optional[TimelineItem]:
    effective_dates = store.event_dates(event)
    if not effective_dates:
        return None
    start_date, end_date = effective_dates[0], effective_dates[-1]
    visible_dates = tuple(day for day in effective_dates if period_start <= day <= period_end)
    if not visible_dates:
        return None
    effective_set = set(effective_dates)
    skipped_dates = tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
        if start_date + timedelta(days=offset) not in effective_set
    )
    has_deadline = store.event_has_deadline(event)
    return TimelineItem(
        id=event.id,
        title=event.title,
        color=event.color,
        task_type=event.event_type,
        completed=event.done,
        start_date=start_date,
        end_date=end_date,
        visible_start=visible_dates[0],
        visible_end=visible_dates[-1],
        effective_dates=effective_dates,
        visible_effective_dates=visible_dates,
        skipped_dates=skipped_dates,
        segments=_segments_for(visible_dates),
        ddl_date=store.event_end_date(event) if has_deadline else None,
        is_urgent=event.event_type == "urgent",
        native_ddl=event.event_type == "ddl",
        end_as_ddl=event.end_as_ddl,
        continues_from_previous_period=start_date < period_start,
        continues_to_next_period=end_date > period_end,
        source_task_ids=(event.id,),
        calendar_span_days=(end_date - start_date).days + 1,
        effective_days_count=len(effective_dates),
        created_at=event.created_at,
        notes=event.notes,
    )


def build_month_timeline(
    store: Store,
    year: int,
    month: int,
    *,
    today: Optional[date] = None,
) -> TimelineMonth:
    """Build one immutable month model from the same facts used by Compact View."""
    last_day = calendar.monthrange(year, month)[1]
    period_start = date(year, month, 1)
    period_end = date(year, month, last_day)
    today = today or date.today()
    days = tuple(
        _day_meta(store, period_start + timedelta(days=offset), today)
        for offset in range(last_day)
    )
    items = tuple(
        item
        for item in (
            _timeline_item(store, event, period_start, period_end)
            for event in store.events
        )
        if item is not None
    )
    items = tuple(
        sorted(
            items,
            key=lambda item: (
                item.completed,
                item.start_date,
                EVENT_TYPE_RANK[item.task_type],
                item.created_at,
                item.id,
            ),
        )
    )
    return TimelineMonth(year, month, period_start, period_end, days, items)
