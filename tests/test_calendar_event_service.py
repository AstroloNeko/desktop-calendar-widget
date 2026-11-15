from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
from unittest import TestCase
from unittest.mock import patch
from calendar_core import Store, RoutineItem
from event_commands import EventCommandHistory, RoutineChange
from calendar_event_service import CalendarEventService, EventOperationError


class ServiceTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'data.json')
        self.history = EventCommandHistory(self.store)
        self.service = CalendarEventService(self.history)

    def seed(self):
        return self.service.create_quick('test', date(2026,11,23)).event_ids[0]

    def test_create_patch_delete(self):
        identifier = self.seed()
        result = self.service.patch(identifier, {'title':'changed'})
        self.assertTrue(result.succeeded and result.changed)
        self.assertEqual(result.snapshots[0].event().title, 'changed')
        self.assertEqual(Store(self.store.data_file).events[0].title, 'changed')
        self.assertTrue(self.service.delete(identifier).changed)
        self.assertFalse(self.service.delete(identifier).changed)

    def test_completion_noop(self):
        identifier = self.seed()
        self.service.set_completed(identifier, True)
        count = self.history.undo_count
        with patch.object(self.store,'save', wraps=self.store.save) as save:
            result = self.service.set_completed(identifier, True)
        self.assertFalse(result.changed)
        self.assertEqual(self.history.undo_count,count)
        save.assert_not_called()

    def test_preview_and_commit_counts(self):
        identifier = self.seed()
        count = self.history.undo_count
        with patch.object(self.store,'save', wraps=self.store.save) as save:
            preview = self.service.preview_schedule(identifier, start_date=date(2026,11,24))
            save.assert_not_called()
            self.assertEqual(self.history.undo_count,count)
            result = self.service.commit(preview)
            self.assertEqual(save.call_count,1)
        self.assertEqual(result.affected_dates, {date(2026,11,23),date(2026,11,24)})
        self.assertTrue(result.reminders_changed)

    def test_conflict_and_invalid_preview(self):
        identifier = self.seed()
        preview = self.service.preview_patch(identifier, {'title':'later'})
        self.service.patch(identifier, {'title':'now'})
        with self.assertRaises(EventOperationError) as error:
            self.service.commit(preview)
        self.assertEqual(error.exception.code,'operation_conflict')
        with self.assertRaises(EventOperationError):
            self.service.commit(None)

    def test_invalid_id_patch(self):
        with self.assertRaises(EventOperationError) as error:
            self.service.patch('missing', {'title':'a'})
        self.assertEqual(error.exception.code,'event_not_found')
        identifier = self.seed()
        with self.assertRaises(EventOperationError):
            self.service.patch(identifier, {'title':None})

    def test_failed_save_rolls_back(self):
        identifier = self.seed()
        original = self.store.data_file.read_bytes()
        count = self.history.undo_count
        with patch.object(self.store,'save',side_effect=OSError('disk')):
            with self.assertRaises(EventOperationError) as error:
                self.service.patch(identifier, {'title':'not saved'})
        self.assertEqual(error.exception.code,'storage_unavailable')
        self.assertEqual(self.store.events[0].title,'test')
        self.assertEqual(self.store.data_file.read_bytes(),original)
        self.assertEqual(self.history.undo_count,count)

    def test_bulk_one_save_one_undo(self):
        ids = [self.seed(),self.seed()]
        count = self.history.undo_count
        with patch.object(self.store,'save',wraps=self.store.save) as save:
            result = self.service.bulk(ids,'shift',days=2)
        self.assertEqual(save.call_count,1)
        self.assertEqual(result.count,2)
        self.assertEqual(self.history.undo_count,count+1)
        self.history.undo()
        self.assertTrue(all(e.due_date==date(2026,11,23) for e in self.store.events))

    def test_bulk_failure_atomic(self):
        ids = [self.seed(),self.seed()]
        with patch.object(self.store,'save',side_effect=OSError('disk')):
            with self.assertRaises(EventOperationError):
                self.service.bulk(ids,'delete')
        self.assertEqual([e.id for e in self.store.events],ids)

    def test_mixed_history(self):
        identifier = self.seed()
        self.history.upsert_routine(RoutineItem('habit','习惯'))
        self.service.patch(identifier, {'title':'changed'})
        self.history.undo()
        self.assertIsInstance(self.history.undo(),RoutineChange)
        self.assertEqual(self.store.routines,[])
        self.history.redo()
        self.history.redo()
        self.assertEqual(self.store.events[0].title,'changed')

    def test_a1_ddl_and_no_time(self):
        identifier = self.seed()
        result = self.service.patch(identifier, {'duration_days':3,'skip_non_working_days':True,'end_as_ddl':True})
        self.assertTrue(result.ddl_changed)
        event = result.snapshots[0].event()
        self.assertFalse(event.has_time)
        self.assertTrue(event.due.endswith('23:59'))
