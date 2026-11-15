"""Adapter-neutral contract cases; factory can be replaced for a future adapter."""
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from calendar_core import Store, Event, RoutineItem, EventCategory
from calendar_repository import RepositoryConflictError
from calendar_event_service import CalendarEventService
from calendar_mutation_service import CalendarMutationService
from event_commands import EventCommandHistory


class RepositoryContractTests(TestCase):
    def create_store(self, path):
        return Store(path)

    def setUp(self):
        folder=TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'calendar.json'
        self.store=self.create_store(self.path)
        self.repo=self.store.repository
        self.history=EventCommandHistory(self.store)
        self.events=CalendarEventService(self.history)
        self.mutations=CalendarMutationService(self.history)

    def seed(self):
        self.events.upsert(Event('legacy-id','First','2026-11-30T23:59',has_time=False,
                                 duration_days=3,end_as_ddl=True))
        return 'legacy-id'

    def test_empty_reads(self):
        self.assertEqual(self.repo.list_events(),[])
        self.assertIsNone(self.repo.get_event('missing'))
        self.assertEqual(self.repo.get_date_states(),{})

    def test_get_event_detached(self):
        identifier=self.seed()
        event=self.repo.get_event(identifier)
        event.title='uncommitted'
        self.assertEqual(self.repo.get_event(identifier).title,'First')

    def test_list_and_snapshot_detached(self):
        self.seed()
        self.repo.list_events().clear()
        snapshot=self.repo.read_calendar_snapshot()
        snapshot['events'][0].title='uncommitted'
        snapshot['date_states']['2026-12-01']='leave'
        self.assertEqual(self.repo.get_event('legacy-id').title,'First')
        self.assertEqual(self.repo.get_date_states(),{})

    def test_expected_revision_commit(self):
        self.seed()
        token=self.repo.revision()
        events=self.repo.list_events()
        events[0].title='New'
        result=self.repo.apply_changes({'events':events},expected_revision=token)
        self.assertTrue(result.changed)
        self.assertNotEqual(result.revision,token)
        events[0].title='after commit mutation'
        self.assertEqual(self.repo.get_event('legacy-id').title,'New')

    def test_conflict_no_write(self):
        token=self.repo.revision()
        self.seed()
        with patch.object(self.store,'save') as save:
            with self.assertRaises(RepositoryConflictError):
                self.repo.apply_changes({'events':[]},expected_revision=token)
            save.assert_not_called()
        self.assertEqual(len(self.repo.list_events()),1)

    def test_noop_zero_save(self):
        self.seed()
        token=self.repo.revision()
        with patch.object(self.store,'save') as save:
            result=self.repo.apply_changes({'events':self.repo.list_events()},expected_revision=token)
            save.assert_not_called()
        self.assertFalse(result.changed)
        self.assertEqual(result.revision,token)

    def test_multi_object_once_and_order(self):
        events=[Event(str(i),str(i),'2026-11-20T23:59',has_time=False) for i in range(5)]
        with patch.object(self.store,'save',wraps=self.store.save) as save:
            self.repo.apply_changes({'events':events,'date_states':{'2026-11-20':'leave'}})
            self.assertEqual(save.call_count,1)
        self.assertEqual([e.id for e in self.repo.list_events()],list(map(str,range(5))))

    def test_delete_and_undo(self):
        identifier=self.seed()
        self.events.delete(identifier)
        self.assertIsNone(self.repo.get_event(identifier))
        self.history.undo()
        self.assertEqual(self.repo.get_event(identifier).id,identifier)

    def test_habit_history_and_completion(self):
        item=RoutineItem('old-habit','Habit',created_on='2026-01-01',workdays_only=False)
        self.mutations.upsert_routine(item)
        self.mutations.upsert_routine(replace(item,frequency='weekly',weekdays=[0]))
        self.mutations.set_routine_completed(item.id,date(2026,11,16),True)
        read=self.repo.get_routine(item.id)
        self.assertTrue(read.schedule_history)
        self.assertIn('2026-11-16',read.habit_done)
        read.habit_done.clear()
        read.schedule_history.clear()
        self.assertTrue(self.repo.get_routine(item.id).schedule_history)
        self.assertTrue(self.repo.get_routine(item.id).habit_done)

    def test_category_color_transaction(self):
        category=EventCategory('cat','Category','#52B788')
        self.mutations.upsert_category(category)
        self.events.upsert(Event('e','Event','2026-11-20T10:00',category_id='cat',color_mode='inherit'))
        read=self.repo.get_category('cat')
        read.color='#8B70D6'
        self.assertEqual(self.repo.get_category('cat').color,'#52B788')
        self.mutations.delete_category('cat')
        self.assertEqual(self.repo.get_event('e').color,'#52B788')
        self.assertIsNone(self.repo.get_event('e').category_id)

    def test_cross_month_deadline_untimed(self):
        self.seed()
        event=self.repo.get_event('legacy-id')
        self.assertFalse(event.has_time)
        self.assertEqual(event.due,'2026-11-30T23:59')
        self.assertEqual(self.store.event_end_date(event),date(2026,12,2))

    def test_save_failure_all_fields_rollback(self):
        self.seed()
        before=self.repo.read_calendar_snapshot()
        with patch.object(self.store,'save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.repo.apply_changes({'events':[],'date_states':{'2026-12-01':'holiday'}})
        self.assertEqual(self.repo.read_calendar_snapshot(),before)

    def test_invalid_set_zero_write(self):
        for changes in ({'unknown':[]},{'events':[Event('x','x','2026-11-20'),Event('x','y','2026-11-21')]},
                        {'date_states':{'bad':'leave'}},{'date_states':{'2026-12-01':'normal'}}):
            with self.subTest(changes=changes), patch.object(self.store,'save') as save:
                with self.assertRaises(ValueError): self.repo.apply_changes(changes)
                save.assert_not_called()

    def test_preferences_detached(self):
        self.store.settings['future_unknown']={'keep':[1,2]}
        self.store.save()
        snapshot=self.repo.read_preferences()
        snapshot['future_unknown']['keep'].clear()
        self.assertEqual(self.repo.read_preferences()['future_unknown'],{'keep':[1,2]})

    def test_restart_token_not_persistent(self):
        self.seed()
        reopened=self.create_store(self.path).repository
        self.assertEqual(reopened.list_events(),self.repo.list_events())
        self.assertNotEqual(reopened.revision(),self.repo.revision())

    def test_rule_change_invalidates_preview(self):
        self.events.upsert(Event('e','e','2026-11-16T23:59',duration_days=2,skip_non_working_days=True))
        preview=self.events.preview_patch('e',{'title':'changed'})
        self.mutations.set_date_status(date(2026,11,17),'leave')
        with self.assertRaises(ValueError): self.events.commit(preview)
