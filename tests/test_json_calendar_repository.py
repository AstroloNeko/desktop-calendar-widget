from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from calendar_core import Store, Event, MutationUncertainError
from desktop_calendar.storage.json_calendar_repository import JsonCalendarRepository
from calendar_runtime import HeadlessRuntime
from calendar_event_service import CalendarEventService, EventOperationError
from event_commands import EventCommandHistory
from data_compat import DataCompatibilityError


class JsonAdapterTests(TestCase):
    def setUp(self):
        folder=TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'data.json'
        self.store=Store(self.path)
        self.store.save()
        self.repo=self.store.repository

    def changes(self):
        return {'events':[Event('old','Synthetic','2026-11-20T23:59',has_time=False)]}

    def test_temporary_write_failure(self):
        before=self.path.read_bytes()
        with patch.object(Path,'write_text',side_effect=OSError('disk')):
            with self.assertRaises(OSError): self.repo.apply_changes(self.changes())
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.repo.list_events(),[])

    def test_replace_failure(self):
        before=self.path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('replace')):
            with self.assertRaises(OSError): self.repo.apply_changes(self.changes())
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.repo.list_events(),[])

    def test_post_replace_unknown_blocks(self):
        original=Path.replace
        def replace(path,target):
            original(path,target)
            raise OSError('after replace')
        with patch.object(Path,'replace',replace):
            with self.assertRaises(MutationUncertainError): self.repo.apply_changes(self.changes())
        self.assertTrue(self.store.load_error)
        self.assertEqual(len(Store(self.path).events),1)
        with self.assertRaises(DataCompatibilityError): self.repo.apply_changes({'events':[]})

    def test_unreadable_outcome_unknown(self):
        with patch.object(self.repo,'_disk_digest',side_effect=[b'before',OSError('unreadable')]), \
             patch.object(self.store,'save',side_effect=OSError('disk')):
            with self.assertRaises(MutationUncertainError): self.repo.apply_changes(self.changes())

    def test_event_command_unknown_no_undo(self):
        service=CalendarEventService(EventCommandHistory(self.store))
        original=self.store.save
        def save():
            original()
            raise OSError('after save')
        with patch.object(self.store,'save',side_effect=save):
            with self.assertRaises(EventOperationError) as error:
                service.create_quick('test',date(2026,11,20))
        self.assertEqual(error.exception.code,'execution_uncertain')
        self.assertEqual(service.history.undo_count,0)
        self.assertEqual(len(Store(self.path).events),1)

    def test_synthetic_versions_one_to_nine(self):
        # Synthetic compatibility gate coverage, NOT archived authentic releases.
        for version in range(1,10):
            self.path.write_text(json.dumps({'version':version,'events':[{'id':'old-id','title':'test',
                'due':'2026-11-20T23:59','has_time':False}], 'settings':{'future_field':{'x':1}}}),encoding='utf8')
            loaded=Store(self.path)
            self.assertIsNone(loaded.load_error)
            self.assertEqual(loaded.repository.get_event('old-id').id,'old-id')
            self.assertFalse(loaded.repository.get_event('old-id').has_time)
            self.assertEqual(loaded.settings['future_field'],{'x':1})

    def test_incompatible_and_corrupt_refused(self):
        for raw in (b'not json',b'{"version":10}',b'[]'):
            self.path.write_bytes(raw)
            store=Store(self.path)
            self.assertTrue(store.load_error)
            with self.assertRaises(DataCompatibilityError): store.save()
            self.assertEqual(self.path.read_bytes(),raw)

    def test_duplicate_legacy_read_not_silent_merge(self):
        row={'id':'duplicate','title':'same','due':'2026-11-20T10:00'}
        self.path.write_text(json.dumps({'version':9,'events':[row,row,{'bad':'record'}]}),encoding='utf8')
        store=Store(self.path)
        self.assertEqual(len(store.repository.list_events()),2)
        with self.assertRaises(ValueError): store.repository.get_event('duplicate')

    def test_restore_keeps_adapter_owner(self):
        from data_backup import create_backup, restore_store
        self.repo.apply_changes(self.changes())
        archive=self.path.with_suffix('.dcalbackup')
        create_backup(self.path.read_bytes(),archive)
        self.repo.apply_changes({'events':[]})
        token=self.repo.revision()
        restore_store(self.store,archive)
        self.assertIs(self.store.repository,self.repo)
        self.assertIs(self.repo.legacy_store,self.store)
        self.assertNotEqual(token,self.repo.revision())
        self.repo.apply_changes({'events':[]})
        self.assertEqual(Store(self.path).events,[])

    def test_search_snapshot_has_no_writer(self):
        from event_search import capture_search_store
        snapshot=capture_search_store(self.store)
        self.assertIsNone(snapshot.repository)
        with self.assertRaises(AttributeError): snapshot.save()

    def test_headless_injected_adapter(self):
        instances=[]
        def factory(store):
            adapter=JsonCalendarRepository(store)
            instances.append(adapter)
            return adapter
        with HeadlessRuntime(self.path,repository_factory=factory) as runtime:
            runtime.events.create_quick('test',date(2026,11,20))
            self.assertIs(runtime._core.repository,instances[0])
            self.assertIs(runtime._core.events.repository,instances[0])
            self.assertIs(runtime._core.mutations.repository,instances[0])
            self.assertEqual(len(runtime.snapshot()['events']),1)

    def test_json_fields_and_schema_unchanged(self):
        self.repo.apply_changes(self.changes())
        data=json.loads(self.path.read_bytes())
        self.assertEqual(data['version'],9)
        self.assertEqual(set(data),{'version','events','categories','routines','date_states','settings','notified'})
        self.assertNotIn('revision',data['events'][0])
