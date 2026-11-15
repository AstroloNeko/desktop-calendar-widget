"""Thin application boundary. The caller owns threading, receipts and UI effects."""
from dataclasses import dataclass
from typing import Optional

from data_compat import DataCompatibilityError
from event_commands import EventPreview, BatchPreview, EventChange, BatchChange, CommandConflictError
from event_input import build_event_candidate


@dataclass(frozen=True)
class OperationContext:
    source: str = 'desktop'
    request_id: Optional[str] = None


class EventOperationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EventOperationResult:
    operation: str
    change: Optional[EventChange | BatchChange]
    event_ids: tuple[str, ...]
    snapshots: tuple
    context: OperationContext

    @property
    def succeeded(self):
        return True  # Failures raise; never manufacture a success result.

    @property
    def changed(self):
        return self.change is not None

    @property
    def affected_dates(self):
        return self.change.affected_dates if self.change else frozenset()

    @property
    def ddl_changed(self):
        return bool(self.change and self.change.ddl_changed)

    @property
    def reminders_changed(self):
        return self.changed  # Existing UI conservatively invalidates on every change.

    @property
    def count(self):
        return len(self.event_ids)


class CalendarEventService:
    def __init__(self, history, context=None):
        self.history = history
        self.context = context or OperationContext()

    def _call(self, function):
        try:
            return function()
        except EventOperationError:
            raise
        except DataCompatibilityError as exc:
            raise EventOperationError('incompatible_data', str(exc)) from exc
        except CommandConflictError as exc:
            raise EventOperationError('operation_conflict', str(exc)) from exc
        except OSError as exc:
            raise EventOperationError('storage_unavailable', '日历保存未成功，请检查存储是否可用。') from exc
        except (ValueError, OverflowError) as exc:
            raise EventOperationError('validation_failed', str(exc)) from exc

    def _result(self, operation, change, ids):
        # Detached immutable EventState snapshots; no mutable canonical objects escape.
        return EventOperationResult(operation, change, tuple(ids),
            tuple(self.history._capture(identifier) for identifier in ids), self.context)

    def preview_upsert(self, event, **options):
        return self._call(lambda: self.history.preview_upsert(event, **options))

    def preview_update(self, identifier, **options):
        return self._call(lambda: self.history.preview_update(identifier, **options))

    def preview_patch(self, identifier, changes):
        current = self.history.store.event_by_id(identifier)
        if current is None:
            raise EventOperationError('event_not_found', '事项已不存在。')
        return self._call(lambda: self.history.preview_upsert(build_event_candidate(
            changes, current=current, context=self.history.store)))

    def preview_schedule(self, identifier, **options):
        return self._call(lambda: self.history.preview_schedule(identifier, **options))

    def preview_bulk(self, ids, operation, **options):
        return self._call(lambda: self.history.preview_bulk(ids, operation, **options))

    def commit(self, preview):
        if not isinstance(preview, (EventPreview, BatchPreview)):
            raise EventOperationError('invalid_preview', '事项预览无效。')
        batch = isinstance(preview, BatchPreview)
        ids = tuple(e.event_id for e in preview.entries) if batch else (preview.event_id,)
        change = self._call(lambda: self.history.commit_bulk(preview) if batch else self.history.commit(preview))
        return self._result('batch' if batch else 'commit', change, ids)

    def upsert(self, event, **options):
        return self.commit(self.preview_upsert(event, **options))

    def update(self, identifier, **options):
        return self.commit(self.preview_update(identifier, **options))

    def patch(self, identifier, changes):
        return self.commit(self.preview_patch(identifier, changes))

    def set_completed(self, identifier, done):
        if type(done) is not bool:
            raise EventOperationError('validation_failed', '完成状态必须为布尔值。')
        return self.update(identifier, done=done, snooze_until=None)

    def create_quick(self, title, day, **options):
        candidate = self._call(lambda: self.history.store.make_quick_event(title, day, **options))
        return self.upsert(candidate)

    def delete(self, identifier):
        change = self._call(lambda: self.history.delete(identifier))
        return self._result('delete', change, (identifier,))

    def bulk(self, ids, operation, **options):
        return self.commit(self.preview_bulk(ids, operation, **options))


def event_service(history):
    """A lightweight facade over the SAME history, never a second undo stack."""
    return CalendarEventService(history)
