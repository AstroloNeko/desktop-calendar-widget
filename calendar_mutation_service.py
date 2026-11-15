"""Habit, category and calendar-rule application transactions (JSON adapter).

No UI callbacks: results describe committed changes, never refresh success.
Habit edits retain the existing mixed Event/Habit history. Deletes/completions
remain non-undoable; category changes invalidate that history after saving.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from calendar_event_service import CalendarEventService, OperationContext
from calendar_event_service import EventOperationError
from calendar_core import MutationUncertainError


@dataclass(frozen=True)
class MutationResult:
    operation: str
    changed: bool
    routine_ids: tuple = ()
    category_ids: tuple = ()
    event_ids: tuple = ()
    affected_dates: frozenset = frozenset()
    ddl_changed: bool = False
    reminders_changed: bool = False
    calendar_rules_changed: bool = False
    change: object = None
    context: OperationContext = OperationContext()

    @property
    def succeeded(self):
        return True


class CalendarMutationService(CalendarEventService):
    """Reuse A2 error mapping and the caller's single command history."""
    @property
    def store(self):
        return self.history.store

    def _call(self, function):
        def guarded():
            try:
                return function()
            except MutationUncertainError as exc:
                raise EventOperationError('execution_uncertain', str(exc)) from exc
        return super()._call(guarded)

    def upsert_routine(self, item):
        change = self._call(lambda: self.history.upsert_routine(item))
        return MutationResult('routine_upsert', change is not None, routine_ids=(item.id,),
                              reminders_changed=change is not None, change=change, context=self.context)

    def delete_routine(self, identifier):
        exists = any(r.id == identifier for r in self.store.routines)
        self._call(lambda: self.store.delete_routine(identifier))
        return MutationResult('routine_delete', exists, routine_ids=(identifier,),
                              reminders_changed=exists, context=self.context)

    def set_routine_completed(self, identifier, day, completed):
        changed = self._call(lambda: self.store.set_routine_completed(identifier, day, completed))
        return MutationResult('routine_completion', changed, routine_ids=(identifier,),
                              affected_dates=frozenset((day,)), reminders_changed=changed, context=self.context)

    def upsert_category(self, category):
        before = self.repository.get_category(category.id)
        changed = before != category
        ids = tuple(e.id for e in self.store.events if e.category_id == category.id)
        self._call(lambda: self.store.upsert_category(category))
        if changed:
            self.history.clear()
        return MutationResult('category_upsert', changed, category_ids=(category.id,),
                              event_ids=ids, context=self.context)

    def delete_category(self, identifier):
        changed = self.store.category_by_id(identifier) is not None
        ids = tuple(e.id for e in self.store.events if e.category_id == identifier)
        self._call(lambda: self.store.delete_category(identifier))
        if changed:
            self.history.clear()
        return MutationResult('category_delete', changed, category_ids=(identifier,),
                              event_ids=ids, context=self.context)

    def set_date_status(self, start, status, end=None):
        end = start if end is None else end
        events = [e for e in self.store.events if e.skip_non_working_days]
        before = {e.id: self.store.event_dates(e) for e in events}
        states = self._call(lambda: self.store.prepare_date_status_range(start, end, status))
        original = self.store.date_states
        try:
            # Serial application thread only; evaluate with existing calendar rules,
            # never persist or publish a notification during this preview.
            self.store.date_states = states
            after_dates = self._call(lambda: {e.id: self.store.event_dates(e) for e in events})
        finally:
            self.store.date_states = original
        changed = self._call(lambda: self.store.set_date_status_range(start, end, status))
        dates = set()
        ids = []
        if changed:
            dates.update(start + timedelta(days=n) for n in range((end - start).days + 1))
            for event in events:
                after = after_dates[event.id]
                if before[event.id] != after:
                    ids.append(event.id)
                    dates.update(before[event.id])
                    dates.update(after)
        return MutationResult('date_status', changed, event_ids=tuple(ids),
            routine_ids=tuple(r.id for r in self.store.routines) if changed else (),
            affected_dates=frozenset(dates), ddl_changed=bool(ids), reminders_changed=changed,
            calendar_rules_changed=changed, context=self.context)


def mutation_service(history):
    return CalendarMutationService(history)
