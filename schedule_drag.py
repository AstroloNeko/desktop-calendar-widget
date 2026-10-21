"""Shared, write-free scheduling gesture for Flow and Timeline renderers."""

from dataclasses import dataclass
from datetime import date, timedelta
from math import hypot
from typing import Optional

from event_commands import CommandConflictError, EventCommandHistory, EventPreview


@dataclass(frozen=True)
class ScheduleHit:
    event_id: str
    bounds: tuple[float, float, float, float]
    true_start: bool
    true_end: bool

    def operation_at(self, x: float, edge: float) -> str:
        left, _, right, _ = self.bounds
        if self.true_start and abs(x - left) <= edge:
            return "start"
        if self.true_end and abs(x - right) <= edge:
            return "end"
        return "move"


class ScheduleDrag:
    """One immutable source snapshot, many previews, at most one final command.

    Coordinates are in the renderer's *physical* canvas units. The caller supplies
    a DPI-scaled threshold; date interpretation belongs to the shared Store.
    """

    def __init__(self, history: EventCommandHistory, event_id: str, operation: str,
                 anchor_day: date, pointer: tuple[float, float], *, threshold: float = 5):
        if operation not in ("move", "start", "end"):
            raise ValueError("unknown schedule operation")
        self.history = history
        self.source = history.preview_update(event_id)
        self.operation = operation
        self.anchor_day = anchor_day
        self.pointer = pointer
        self.threshold = threshold
        self.active = False
        self.cancelled = False
        self.error = ""
        self.preview: Optional[EventPreview] = None
        self.last_day: Optional[date] = None

    def update(self, day: Optional[date], pointer: tuple[float, float]) -> Optional[EventPreview]:
        if self.cancelled:
            return None
        self.active = self.active or hypot(pointer[0] - self.pointer[0], pointer[1] - self.pointer[1]) >= self.threshold
        if not self.active:
            return None
        if day == self.last_day and self.preview is not None:
            return self.preview
        self.last_day = day
        self.preview = None
        self.error = ""
        if day is None:
            self.error = "请将鼠标移回排期区域，或按 Esc 取消"
            return None
        try:
            event = self.source.event()
            live = self.history.preview_update(event.id)
            if live.before != self.source.before or live.effective_dates != self.source.effective_dates:
                raise CommandConflictError("事项或工作日日历已变化，请重新拖动")
            if self.operation == "move":
                delta = (day - self.anchor_day).days
                # A stored Saturday start can be displayed from Monday. Move
                # the visible schedule, not an invisible pre-expansion anchor.
                start = self.source.effective_dates[0] + timedelta(days=delta) if delta else event.due_date
                candidate = self.history.preview_schedule(event.id, start_date=start)
            elif self.operation == "start":
                candidate = self.history.preview_schedule(event.id, start_date=day, end_date=self.source.effective_dates[-1])
            else:
                candidate = self.history.preview_schedule(event.id, end_date=day)
            self.preview = candidate
        except (ValueError, OverflowError) as exc:
            self.error = str(exc)
        return self.preview

    def finish(self) -> Optional[EventPreview]:
        candidate = self.preview if self.active and not self.cancelled and not self.error else None
        self.cancelled = True
        if candidate is None:
            return None
        old, new = self.source.event(), candidate.event()
        # A gesture back to its original schedule must not clear a snooze,
        # consume history, or discard a redo branch.
        if (old.due, old.duration_days) == (new.due, new.duration_days):
            return None
        return candidate

    def cancel(self) -> None:
        self.cancelled = True
        self.preview = None


def hit_at(hits: list[ScheduleHit], x: float, y: float) -> Optional[ScheduleHit]:
    return next((hit for hit in reversed(hits)
                 if hit.bounds[0] <= x <= hit.bounds[2] and hit.bounds[1] <= y <= hit.bounds[3]), None)
