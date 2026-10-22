"""Session-local, reversible event commands; independent of Tk and rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any, Optional

from calendar_core import Event, Store


class CommandConflictError(ValueError):
    """A preview/history item no longer describes the canonical event."""


@dataclass(frozen=True)
class EventState:
    # Event currently contains scalar fields only. Tuples prevent callers from
    # mutating a history entry through a cached editor or Timeline reference.
    fields: Optional[tuple[tuple[str, Any], ...]]
    index: Optional[int]
    notification_keys: frozenset[str]

    def event(self) -> Optional[Event]:
        return Event(**dict(self.fields)) if self.fields is not None else None


@dataclass(frozen=True)
class EventPreview:
    event_id: str
    label: str
    before: EventState
    after: EventState
    before_dates: tuple[date, ...]
    effective_dates: tuple[date, ...]
    deadline: Optional[date]
    effective_color: Optional[str]

    def event(self) -> Optional[Event]:
        return self.after.event()


@dataclass(frozen=True)
class EventChange:
    event_id: str
    label: str
    before: EventState
    after: EventState
    before_dates: tuple[date, ...]
    after_dates: tuple[date, ...]

    @property
    def affected_dates(self) -> frozenset[date]:
        return frozenset(self.before_dates + self.after_dates)

    @property
    def ddl_changed(self) -> bool:
        return any(
            event is not None and Store.event_has_deadline(event)
            for event in (self.before.event(), self.after.event())
        )


class EventCommandHistory:
    """Only successful commits enter history; previews never mutate Store."""

    def __init__(self, store: Store, limit: int = 100) -> None:
        self.store = store
        self.limit = max(50, limit)
        self._undo: list[EventPreview] = []
        self._redo: list[EventPreview] = []

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_count(self) -> int:
        return len(self._undo)

    @property
    def redo_count(self) -> int:
        return len(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def _capture(self, event_id: str) -> EventState:
        matches = [(index, event) for index, event in enumerate(self.store.events) if event.id == event_id]
        if len(matches) > 1:
            raise CommandConflictError("存在重复事项 ID，已取消修改。")
        index, event = matches[0] if matches else (None, None)
        keys = frozenset(key for key in self.store.notified if key.startswith(event_id + ":"))
        return EventState(tuple(asdict(event).items()) if event else None, index, keys)

    def _dates(self, state: EventState) -> tuple[date, ...]:
        event = state.event()
        return self.store.event_dates(event) if event is not None else ()

    def preview_upsert(self, event: Event, *, label: Optional[str] = None) -> EventPreview:
        candidate = self.store.prepare_event(event)
        before = self._capture(candidate.id)
        previous = before.event()
        schedule_fields = ("due", "reminder", "has_time", "duration_days", "skip_non_working_days", "end_as_ddl")
        reset = previous is None or any(getattr(previous, key) != getattr(candidate, key) for key in schedule_fields)
        if previous is not None and reset:
            candidate.snooze_until = None
        after = EventState(
            tuple(asdict(candidate).items()),
            before.index if before.index is not None else len(self.store.events),
            frozenset() if reset else before.notification_keys,
        )
        dates = self._dates(after)
        return EventPreview(
            candidate.id, label or ("编辑事项" if previous else "新建事项"), before, after,
            self._dates(before), dates,
            dates[-1] if self.store.event_has_deadline(candidate) else None,
            self.store.effective_event_color(candidate),
        )

    def preview_update(self, event_id: str, *, label: str = "编辑事项", **changes: Any) -> EventPreview:
        current = self.store.event_by_id(event_id)
        if current is None:
            raise CommandConflictError("事项已不存在。")
        if "id" in changes and changes["id"] != event_id:
            raise ValueError("event id cannot change")
        return self.preview_upsert(replace(current, **changes), label=label)

    def preview_schedule(
        self,
        event_id: str,
        *,
        start_date: Optional[date] = None,
        duration_days: Optional[int] = None,
        end_date: Optional[date] = None,
    ) -> EventPreview:
        current = self.store.event_by_id(event_id)
        if current is None:
            raise CommandConflictError("事项已不存在。")
        if end_date is not None and duration_days is not None:
            raise ValueError("specify an end date or a duration, not both")
        start = start_date or current.due_date
        if end_date is not None:
            duration_days = self.store.duration_for_range(start, end_date, skip_non_working_days=current.skip_non_working_days)
        if duration_days is not None and (type(duration_days) is not int or not 1 <= duration_days <= 365):
            raise ValueError("duration must be between 1 and 365 effective days")
        due = datetime.combine(start, current.due_at.time()).isoformat(timespec="minutes")
        return self.preview_update(
            event_id, label="调整排期", due=due,
            duration_days=current.duration_days if duration_days is None else duration_days,
            snooze_until=None,
        )

    def commit(self, preview: EventPreview) -> Optional[EventChange]:
        if self._capture(preview.event_id) != preview.before or self._dates(preview.after) != preview.effective_dates:
            raise CommandConflictError("事项或工作日日历已变化，请重新预览后再提交。")
        candidate = preview.event()
        if candidate is not None and asdict(self.store.prepare_event(candidate)) != asdict(candidate):
            raise CommandConflictError("事项分类已变化，请重新预览后再提交。")
        if preview.before.fields == preview.after.fields:
            return None
        change = self._apply(preview, forward=True)
        # Capture the successfully persisted state (including notification pruning).
        committed = replace(preview, after=self._capture(preview.event_id))
        self._undo.append(committed)
        del self._undo[:-self.limit]
        self._redo.clear()
        return change

    def upsert(self, event: Event, *, label: Optional[str] = None) -> Optional[EventChange]:
        return self.commit(self.preview_upsert(event, label=label))

    def update(self, event_id: str, *, label: str = "编辑事项", **changes: Any) -> Optional[EventChange]:
        return self.commit(self.preview_update(event_id, label=label, **changes))

    def create_quick(self, title: str, day: date, **options: Any) -> Optional[EventChange]:
        return self.upsert(self.store.make_quick_event(title, day, **options))

    def delete(self, event_id: str) -> Optional[EventChange]:
        before = self._capture(event_id)
        if before.fields is None:
            return None
        return self.commit(EventPreview(
            event_id, "删除事项", before, EventState(None, None, frozenset()),
            self._dates(before), (), None, None,
        ))

    def _apply(self, command: EventPreview, *, forward: bool) -> EventChange:
        source, target = (command.before, command.after) if forward else (command.after, command.before)
        live = self._capture(command.event_id)
        if live.fields != source.fields:
            raise CommandConflictError("事项已在历史记录之外发生变化，已取消撤销或重做。")
        event = target.event()
        if event is not None and self.store.prepare_event(event).category_id != event.category_id:
            raise CommandConflictError("事项分类已变化，无法恢复原状态。")
        before_dates, after_dates = self._dates(source), self._dates(target)
        # Preserve reminders delivered since this command when their keys were
        # not changed by the operation itself (e.g. undoing only a title edit).
        keys = (
            live.notification_keys - (source.notification_keys - target.notification_keys)
        ) | (target.notification_keys - source.notification_keys)
        if event is None:
            keys = frozenset()
        self.store.persist_event_state(
            command.event_id, event, index=target.index, notification_keys=keys,
        )
        return EventChange(command.event_id, command.label, source, self._capture(command.event_id), before_dates, after_dates)

    def undo(self) -> Optional[EventChange]:
        if not self._undo:
            return None
        command = self._undo[-1]
        change = self._apply(command, forward=False)
        self._undo.pop()
        self._redo.append(command)
        return change

    def redo(self) -> Optional[EventChange]:
        if not self._redo:
            return None
        command = self._redo[-1]
        change = self._apply(command, forward=True)
        self._redo.pop()
        self._undo.append(command)
        return change
