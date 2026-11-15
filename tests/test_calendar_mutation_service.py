from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from calendar_core import Store, RoutineItem, EventCategory, Event
from calendar_mutation_service import CalendarMutationService
from calendar_event_service import EventOperationError
from event_commands import EventCommandHistory


class MutationTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'calendar.json')
        self.history = EventCommandHistory(self.store)
        self.service = CalendarMutationService(self.history)
        self.day = date(2026, 11, 16)
        self.habit = RoutineItem('h', 'Habit', created_on='2026-01-01', workdays_only=False,
                                 habit_done=['2026-11-10'], reminder_enabled=True, reminder_time='09:00')
        self.store.routines = [self.habit]
        self.category = EventCategory('cat', 'Category', '#52B788')
        self.store.categories = [self.category]
        self.store.events = [Event('a', 'A', '2026-11-16T10:00', category_id='cat', color_mode='inherit', color='#52B788'),
                             Event('b', 'B', '2026-11-16T10:00', category_id='cat', color='#8B70D6')]
        self.store.notified = {'routine:h:old', 'unrelated:notice'}
        self.store.save()

    def state(self):
        return deepcopy((self.store.events, self.store.categories, self.store.routines,
                         self.store.date_states, self.store.notified, self.store.settings))

    def assert_failure(self, operation):
        before = self.state()
        disk = self.store.data_file.read_bytes()
        undo = self.history.undo_count
        with patch.object(self.store, 'save', side_effect=OSError('disk full')) as save:
            with self.assertRaises(EventOperationError) as error:
                operation()
        self.assertEqual(error.exception.code, 'storage_unavailable')
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.store.data_file.read_bytes(), disk)
        self.assertEqual(self.history.undo_count, undo)

    def test_habit_create_failure(self):
        self.assert_failure(lambda: self.service.upsert_routine(replace(self.habit, id='new')))

    def test_habit_edit_failure(self):
        self.assert_failure(lambda: self.service.upsert_routine(replace(self.habit, title='Updated')))

    def test_habit_delete_failure(self):
        self.assert_failure(lambda: self.service.delete_routine('h'))

    def test_checkin_failure(self):
        self.assert_failure(lambda: self.service.set_routine_completed('h', self.day, True))

    def test_uncheck_failure(self):
        self.service.set_routine_completed('h', self.day, True)
        self.assert_failure(lambda: self.service.set_routine_completed('h', self.day, False))

    def test_schedule_history_failure(self):
        self.assert_failure(lambda: self.service.upsert_routine(replace(self.habit, frequency='weekly', weekdays=[0, 2])))

    def test_reminder_failure(self):
        self.assert_failure(lambda: self.service.upsert_routine(replace(self.habit, reminder_time='10:00')))

    def test_category_create_failure(self):
        self.assert_failure(lambda: self.service.upsert_category(EventCategory('new', 'New')))

    def test_category_color_failure_preserves_references(self):
        old = self.store.events[0]
        self.assert_failure(lambda: self.service.upsert_category(replace(self.category, color='#8B70D6')))
        self.assertIs(self.store.events[0], old)

    def test_category_empty_delete_failure(self):
        self.store.events = []
        self.store.save()
        self.assert_failure(lambda: self.service.delete_category('cat'))

    def test_category_linked_delete_failure(self):
        self.assert_failure(lambda: self.service.delete_category('cat'))

    def test_category_delete_freezes_all_once(self):
        original = [asdict(e) for e in self.store.events]
        with patch.object(self.store, 'save', wraps=self.store.save) as save:
            result = self.service.delete_category('cat')
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result.event_ids, ('a', 'b'))
        for event, before in zip(self.store.events, original):
            before.update(category_id=None, color_mode='override')
            self.assertEqual(asdict(event), before)
        self.assertEqual(Store(self.store.data_file).events, self.store.events)

    def test_date_single_failure(self):
        self.assert_failure(lambda: self.service.set_date_status(self.day, 'leave'))

    def test_date_range_failure(self):
        self.assert_failure(lambda: self.service.set_date_status(self.day, 'leave', date(2026, 11, 20)))

    def test_invalid_ranges_zero_write(self):
        for start, end, status in [('bad', self.day, 'leave'), (self.day, date(2025,1,1), 'leave'),
                                   (self.day, self.day, 'invalid')]:
            with self.subTest(start=start, status=status), patch.object(self.store, 'save') as save:
                with self.assertRaises(EventOperationError):
                    self.service.set_date_status(start, status, end)
                save.assert_not_called()

    def test_range_once_and_noop(self):
        with patch.object(self.store, 'save', wraps=self.store.save) as save:
            result = self.service.set_date_status(self.day, 'leave', date(2026,11,20))
            self.assertEqual(save.call_count, 1)
            self.assertFalse(self.service.set_date_status(self.day, 'leave', date(2026,11,20)).changed)
            self.assertEqual(save.call_count, 1)
        self.assertTrue(result.reminders_changed and result.calendar_rules_changed)
        self.assertEqual(len(result.affected_dates), 5)
        self.assertEqual(Store(self.store.data_file).date_states, self.store.date_states)

    def test_actual_end_ddl_and_affected_dates(self):
        event = replace(self.store.events[0], duration_days=2, skip_non_working_days=True, end_as_ddl=True)
        self.store.events = [event]
        result = self.service.set_date_status(self.day, 'leave', date(2026,11,20))
        self.assertEqual(self.store.event_end_date(event), date(2026,11,24))
        self.assertIn(date(2026,11,24), result.affected_dates)
        self.assertTrue(result.ddl_changed)
        self.assertEqual(result.event_ids, ('a',))
        self.service.set_date_status(self.day, 'normal', date(2026,11,20))
        self.assertEqual(self.store.event_end_date(event), date(2026,11,17))

    def test_completion_explicit_idempotent_and_history_kept(self):
        with patch.object(self.store, 'save', wraps=self.store.save) as save:
            self.service.set_routine_completed('h', self.day, True)
            self.assertFalse(self.service.set_routine_completed('h', self.day, True).changed)
            self.assertEqual(save.call_count, 1)
        self.assertIn('2026-11-10', self.store.routines[0].habit_done)
        self.assertEqual(self.history.undo_count, 0)

    def test_mixed_history_retains_completions(self):
        self.service.upsert_routine(replace(self.habit, frequency='weekly', weekdays=[0]))
        self.history.upsert(replace(self.store.events[0], title='changed'))
        self.service.set_routine_completed('h', self.day, True)
        self.history.undo()
        self.history.undo()
        self.assertEqual(self.store.routines[0].frequency, 'daily')
        self.assertIn(self.day.isoformat(), self.store.routines[0].habit_done)
        self.history.redo()
        self.assertEqual(self.store.routines[0].frequency, 'weekly')

    def test_category_history_invalidates_only_on_success(self):
        self.service.upsert_routine(replace(self.habit, title='changed'))
        self.assert_failure(lambda: self.service.delete_category('cat'))
        self.assertEqual(self.history.undo_count, 1)
        self.service.delete_category('cat')
        self.assertEqual(self.history.undo_count, 0)

    def test_noop_categories_habits_delete(self):
        with patch.object(self.store, 'save') as save:
            self.assertFalse(self.service.upsert_category(self.category).changed)
            self.assertFalse(self.service.upsert_routine(self.habit).changed)
            self.assertFalse(self.service.delete_category('missing').changed)
            self.assertFalse(self.service.delete_routine('missing').changed)
            save.assert_not_called()

    def test_delete_routine_ledger_and_schema(self):
        self.service.delete_routine('h')
        self.assertEqual(self.store.notified, {'unrelated:notice'})
        loaded = Store(self.store.data_file)
        self.assertEqual(loaded.routines, [])
        import json
        self.assertEqual(json.loads(self.store.data_file.read_text(encoding='utf8'))['version'], 9)

    def test_schedule_variants_preserve_completion_and_history(self):
        for changes in [dict(frequency='weekly', weekdays=[0]), dict(interval=2),
                        dict(frequency='monthly', month_day=15), dict(interval=3),
                        dict(start_on='2026-12-01'), dict(end_on='2027-12-31')]:
            item = replace(self.store.routines[0], **changes)
            with patch.object(self.store, 'save', wraps=self.store.save) as save:
                self.service.upsert_routine(item)
                self.assertEqual(save.call_count, 1)
            self.assertIn('2026-11-10', self.store.routines[0].habit_done)
            self.assertEqual(Store(self.store.data_file).routines, self.store.routines)

    def test_unknown_save_outcome_blocks_replay(self):
        real_save = self.store.save
        def replaced_then_failed():
            real_save()
            raise OSError('injected after replace')
        with patch.object(self.store, 'save', side_effect=replaced_then_failed):
            with self.assertRaises(EventOperationError) as error:
                self.service.delete_category('cat')
        self.assertEqual(error.exception.code, 'execution_uncertain')
        self.assertTrue(self.store.load_error)
        self.assertIsNone(self.store.category_by_id('cat'))
        self.assertIsNone(Store(self.store.data_file).category_by_id('cat'))
        with self.assertRaises(EventOperationError):
            self.service.upsert_category(EventCategory('blocked', 'Blocked'))

    def test_unknown_habit_commit_does_not_add_undo(self):
        real_save = self.store.save
        def replaced_then_failed():
            real_save()
            raise OSError('after replace')
        with patch.object(self.store, 'save', side_effect=replaced_then_failed):
            with self.assertRaises(EventOperationError) as error:
                self.service.upsert_routine(replace(self.habit, title='persisted'))
        self.assertEqual(error.exception.code, 'execution_uncertain')
        self.assertEqual(self.store.routines[0].title, 'persisted')
        self.assertEqual(self.history.undo_count, 0)

    def test_backup_restore_after_mutations(self):
        from data_backup import create_backup, restore_store
        self.service.upsert_routine(replace(self.habit, frequency='weekly', weekdays=[0,2]))
        self.service.set_routine_completed('h', self.day, True)
        self.service.delete_category('cat')
        self.service.set_date_status(self.day, 'leave')
        expected = self.state()
        archive = self.store.data_file.with_suffix('.dcalbackup')
        create_backup(self.store.data_file.read_bytes(), archive)
        self.service.delete_routine('h')
        restore_store(self.store, archive)
        self.assertEqual(self.state(), expected)

    def test_native_reminder_ledger_not_cleared(self):
        self.store.settings['native_reminder_state'] = {'delivered': ['kept']}
        self.store.save()
        self.service.set_date_status(self.day, 'leave')
        self.service.delete_routine('h')
        self.assertEqual(self.store.settings['native_reminder_state'], {'delivered': ['kept']})

    def test_legacy_todo_completion_rules(self):
        self.store.routines = [replace(self.habit, kind='todo', habit_done=[])]
        self.service.set_routine_completed('h', self.day, True)
        self.assertEqual(self.store.routines[0].completed_on, self.day.isoformat())
        self.service.set_routine_completed('h', self.day, False)
        self.assertIsNone(self.store.routines[0].completed_on)

    def test_ui_refresh_failure_does_not_replay(self):
        from app import CalendarApp
        from types import SimpleNamespace
        from unittest.mock import Mock
        operation = Mock(side_effect=lambda: self.service.set_routine_completed('h', self.day, True))
        refresh = Mock(side_effect=RuntimeError('view unavailable'))
        with patch('app.owned_messagebox') as notice:
            self.assertTrue(CalendarApp._run_aux_mutation(SimpleNamespace(), operation, refresh, owner=object()))
        self.assertEqual(operation.call_count, 1)
        self.assertTrue(self.store.routines[0].is_done_on(self.day))
        self.assertIn('数据已保存', notice.call_args.args[-1])

    def test_ui_failed_commit_does_not_refresh(self):
        from app import CalendarApp
        from types import SimpleNamespace
        from unittest.mock import Mock
        refresh = Mock()
        with patch('app.owned_messagebox'), patch.object(self.store, 'save', side_effect=OSError('disk')):
            result = CalendarApp._run_aux_mutation(SimpleNamespace(),
                lambda: self.service.delete_category('cat'), refresh, owner=object())
        self.assertFalse(result)
        refresh.assert_not_called()

    def test_date_preview_failure_zero_write(self):
        original = self.store.date_states
        self.store.events[0].skip_non_working_days = True
        with patch.object(self.store, 'event_dates', side_effect=[(self.day,), ValueError('no valid schedule')]), \
             patch.object(self.store, 'save') as save:
            with self.assertRaises(EventOperationError):
                self.service.set_date_status(self.day, 'leave')
        self.assertIs(self.store.date_states, original)
        save.assert_not_called()

    def test_normal_keeps_official_calendar(self):
        day = date(2026,10,1)
        self.service.set_date_status(day, 'leave')
        self.service.set_date_status(day, 'normal')
        self.assertNotIn(day.isoformat(), self.store.date_states)
        self.assertFalse(self.store.is_workday(day))
