from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch

import test_calendar_repository_contract as contract
import test_calendar_runtime as runtime_cases
from calendar_core import Store, Event, RoutineItem, EventCategory, MutationUncertainError
from desktop_calendar.storage.calendar_repository import RepositoryConflictError
from calendar_event_service import CalendarEventService, EventOperationError
from calendar_mutation_service import CalendarMutationService
from calendar_runtime import HeadlessRuntime
from data_compat import DataCompatibilityError
from event_commands import EventCommandHistory
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository


class SqliteContractTests(contract.RepositoryContractTests):
    persistent_revision = True

    def create_store(self, path):
        return Store(path.with_suffix('.sqlite3'), repository_factory=SqliteCalendarRepository)

    def persist_hook(self):
        return patch.object(self.repo, '_commit', wraps=self.repo._commit)

    def fail_persist_hook(self):
        return patch.object(self.repo, '_commit', side_effect=OSError('disk'))


class SqliteRuntimeTests(TestCase):
    # Reuse actual A4 business/chat/HTTP tests, with only backend construction changed.
    setUp = runtime_cases.RuntimeTests.setUp
    create = runtime_cases.RuntimeTests.create

    def make(self, **options):
        return HeadlessRuntime(self.path.with_suffix('.sqlite3'), protector=self.protection,
            provider=self.provider, repository_factory=SqliteCalendarRepository, **options)

    test_crud_restart = runtime_cases.RuntimeTests.test_event_crud_and_restart
    test_habit_history = runtime_cases.RuntimeTests.test_habit_rules_history_completion_restart
    test_mixed_undo = runtime_cases.RuntimeTests.test_mixed_undo
    test_category_history = runtime_cases.RuntimeTests.test_category_invalidates_history
    test_snapshot = runtime_cases.RuntimeTests.test_snapshot_detached
    test_arguments = runtime_cases.RuntimeTests.test_arguments_detached
    test_stop = runtime_cases.RuntimeTests.test_double_stop_and_reject_new_work
    test_ai_tools = runtime_cases.RuntimeTests.test_ai_real_tools_multiturn_delete
    test_http = runtime_cases.RuntimeTests.test_http_auth_and_lifecycle
    test_bounded_stop = runtime_cases.RuntimeTests.test_llm_does_not_occupy_business_executor_and_stop_is_bounded

    def test_no_json_and_closed(self):
        self.create()
        self.runtime.stop()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.runtime._store.repository._closed)
        with self.assertRaises(RuntimeError): self.runtime._store.repository.read_document()


class SqliteSpecificTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'calendar.sqlite3'
        self.store = self.open()
        self.repo = self.store.repository
        self.history = EventCommandHistory(self.store)
        self.events = CalendarEventService(self.history)
        self.mutations = CalendarMutationService(self.history)

    def open(self, path=None):
        return Store(path or self.path, repository_factory=SqliteCalendarRepository)

    def event(self, identifier='legacy-not-uuid'):
        return Event(identifier,'Synthetic','2026-11-20T23:59',has_time=False,
                     duration_days=3,skip_non_working_days=True,end_as_ddl=True)

    def test_archived_theme_fallback_preserves_preferences_and_event(self):
        from desktop_preferences import normalize_theme_name

        event = self.event()
        self.events.upsert(event)
        self.store.settings.update(theme='paper', unrelated={'preserve': True})
        self.store.save()
        loaded = self.open()
        self.addCleanup(loaded.repository.close)
        self.addCleanup(self.repo.close)
        self.assertEqual(loaded.settings['theme'], 'paper')
        self.assertEqual(normalize_theme_name(loaded.settings['theme']), 'modern')
        self.assertEqual(loaded.settings['unrelated'], {'preserve': True})
        self.assertEqual(loaded.events[0].id, event.id)

    def test_two_connections_stale_service_rejected(self):
        other = self.open()
        service = CalendarEventService(EventCommandHistory(other))
        token = other.repository.revision()
        self.events.upsert(self.event())
        with self.assertRaises(EventOperationError) as error: service.upsert(self.event('other'))
        self.assertEqual(error.exception.code,'operation_conflict')
        self.assertEqual(other.events,[])
        with self.assertRaises(RepositoryConflictError):
            other.repository.apply_changes({'events':[]},expected_revision=token)
        other.load()
        service.upsert(self.event('other'))
        self.assertEqual(len(self.open().events),2)

    def test_explicit_new_revision_cannot_bypass_stale_store(self):
        other = self.open()
        self.events.upsert(self.event())
        with self.assertRaises(RepositoryConflictError):
            other.repository.apply_changes({'events':[]},expected_revision=self.repo.revision())

    def test_preview_no_write_and_batch_one_commit(self):
        self.repo.apply_changes({'events':[self.event(str(i)) for i in range(5)]})
        token=self.repo.revision()
        preview=self.events.preview_bulk(['0','1','2'],'shift',days=2)
        self.assertEqual(token,self.repo.revision())
        with patch.object(self.repo,'_commit',wraps=self.repo._commit) as commit:
            self.events.commit(preview)
            self.assertEqual(commit.call_count,1)
        self.assertEqual(self.history.undo_count,1)
        self.history.undo()
        self.assertEqual(self.store.events[0].due,self.event().due)

    def test_partial_sql_failure_rolls_back(self):
        self.events.upsert(self.event())
        before=self.repo.read_calendar_snapshot()
        original=self.repo._sync_records
        def fail(db,table,*args):
            result=original(db,table,*args)
            if table=='events': raise OSError('after row write')
            return result
        with patch.object(self.repo,'_sync_records',side_effect=fail):
            with self.assertRaises(OSError):
                self.repo.apply_changes({'categories':[EventCategory('c','C')],'events':[]})
        self.assertEqual(self.repo.read_calendar_snapshot(),before)
        self.assertEqual(self.open().events,self.store.events)
        self.assertEqual(self.open().categories,[])

    def test_commit_raises_after_success_verified_receipt(self):
        original=self.repo._commit
        def fail(db):
            original(db)
            raise OSError('commit response lost')
        with patch.object(self.repo,'_commit',side_effect=fail):
            result=self.events.upsert(self.event())
        self.assertTrue(result.changed)
        self.assertEqual(self.history.undo_count,1)
        self.assertEqual(len(self.open().events),1)
        self.assertIsNone(self.store.load_error)

    def test_uncertain_commit_no_replay(self):
        original=self.repo._commit
        def fail(db):
            original(db)
            raise OSError('response lost')
        with patch.object(self.repo,'_commit',side_effect=fail), patch.object(self.repo,'receipt',side_effect=OSError('cannot verify')):
            with self.assertRaises(EventOperationError) as error: self.events.upsert(self.event())
        self.assertEqual(error.exception.code,'execution_uncertain')
        self.assertEqual(self.history.undo_count,0)
        self.assertTrue(self.store.load_error)
        with self.assertRaises(DataCompatibilityError): self.repo.apply_changes({'events':[]})
        self.assertEqual(len(self.open().events),1)

    def test_receipt_retry_persistent_and_new_id_is_new_intent(self):
        changes={'events':[self.event()]}
        result=self.repo.apply_changes(changes,operation_id='request-1')
        reopened=self.open().repository
        retry=reopened.apply_changes(changes,operation_id='request-1')
        self.assertTrue(retry.replayed)
        self.assertEqual(result.revision,retry.revision)
        with self.assertRaises(RepositoryConflictError): reopened.apply_changes({'events':[]},operation_id='request-1')
        reopened.apply_changes({'events':changes['events']+[self.event('second')]},operation_id='request-2')
        self.assertEqual(len(self.open().events),2)

    def test_receipt_retry_after_later_commit_does_not_overwrite(self):
        changes={'events':[self.event()]}
        self.repo.apply_changes(changes,operation_id='one')
        self.events.patch('legacy-not-uuid',{'title':'Later'})
        self.repo.apply_changes(changes,operation_id='one')
        self.assertEqual(self.store.events[0].title,'Later')

    def test_busy_lock_safe_failure(self):
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(EventOperationError) as error: self.events.upsert(self.event())
            self.assertEqual(error.exception.code,'storage_unavailable')
        self.assertEqual(self.store.events,[])
        self.assertEqual(self.history.undo_count,0)
        self.events.upsert(self.event())

    def test_foreign_key_batch_rollback(self):
        item=self.event(); item.category_id='missing'
        with self.assertRaises(ValueError): self.repo.apply_changes({'events':[item]})
        self.assertEqual(self.open().events,[])

    def test_legacy_save_cannot_write_domain(self):
        self.store.events=[self.event()]
        with self.assertRaises(RepositoryConflictError): self.store.save()
        self.assertEqual(self.open().events,[])

    def test_preferences_ledger_saved_without_domain_overwrite(self):
        self.events.upsert(self.event())
        self.store.settings['unknown_legacy']={'a':[1,2]}
        self.store.settings['native_reminder_state']={'example':1}
        self.store.notified={'e:key'}
        self.store.save()
        reopened=self.open()
        self.assertEqual(reopened.settings['unknown_legacy'],{'a':[1,2]})
        self.assertEqual(reopened.notified,{'e:key'})
        self.assertEqual(len(reopened.events),1)

    def test_backup_wal_and_reopen_full_state(self):
        self.mutations.upsert_category(EventCategory('c','Category'))
        self.events.upsert(self.event())
        self.mutations.upsert_routine(RoutineItem('h','Habit',workdays_only=False))
        self.mutations.set_routine_completed('h',date(2026,11,15),True)
        self.mutations.set_date_status(date(2026,11,16),'leave')
        # Hold a connection so the WAL isn't deleted at last connection close.
        with closing(self.repo._connect()) as reader:
            reader.execute('BEGIN'); reader.execute('SELECT count(*) FROM events').fetchone()
            target=self.repo.backup_to(self.path.with_name('backup.db'))
            self.assertEqual(reader.execute('SELECT count(*) FROM events').fetchone()[0],1)
        reopened=self.open(target)
        self.assertEqual(reopened.repository.read_calendar_snapshot(),self.repo.read_calendar_snapshot())

    def test_backup_failure_preserves_source(self):
        self.events.upsert(self.event())
        target=self.path.with_name('backup.db')
        target.write_bytes(b'existing backup')
        with self.assertRaises(FileExistsError): self.repo.backup_to(target)
        self.assertEqual(target.read_bytes(),b'existing backup')
        self.assertEqual(len(self.open().events),1)

    def test_json_restore_rejected_before_save(self):
        from data_backup import restore_store, BackupError
        with self.assertRaises(BackupError): restore_store(self.store,self.path.with_name('missing.backup'))

    def test_future_schema_refused_unchanged(self):
        with closing(sqlite3.connect(self.path)) as db: db.execute('PRAGMA user_version=999')
        before=self.path.read_bytes()
        with self.assertRaises(DataCompatibilityError): self.open()
        self.assertEqual(self.path.read_bytes(),before)

    def test_corrupt_file_refused(self):
        path=self.path.with_name('bad.db'); path.write_bytes(b'not a database')
        with self.assertRaises(DataCompatibilityError): self.open(path)
        self.assertEqual(path.read_bytes(),b'not a database')

    def test_json_path_rejected(self):
        path=self.path.with_name('calendar_data.json'); path.write_bytes(b'{"version":9}')
        with self.assertRaises(ValueError): self.open(path)
        self.assertEqual(path.read_bytes(),b'{"version":9}')

    def test_schema_init_failure_atomic(self):
        path=self.path.with_name('init.db')
        with patch('desktop_calendar.storage.sqlite_calendar_repository.statements',return_value=iter(['CREATE TABLE partial(x)','INVALID SQL'])):
            with self.assertRaises(DataCompatibilityError): self.open(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        self.assertIsNone(self.open(path).load_error)

    def test_reload_removes_old_date_overrides(self):
        self.mutations.set_date_status(date(2026,11,16),'leave')
        other=self.open()
        self.mutations.set_date_status(date(2026,11,16),'normal')
        other.load()
        self.assertEqual(other.date_states,{})

    def test_open_instance_cannot_write_future_schema(self):
        with closing(sqlite3.connect(self.path)) as db: db.execute('PRAGMA user_version=999')
        with self.assertRaises(DataCompatibilityError): self.repo.apply_changes({'events':[self.event()]})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)

    def test_second_adapter_on_same_store_rejected(self):
        with self.assertRaises(ValueError): SqliteCalendarRepository(self.store)

    def test_single_patch_only_updates_affected_event(self):
        self.repo.apply_changes({'events':[self.event(str(i)) for i in range(30)]})
        trace=[]
        original=self.repo._connect
        def connect():
            db=original(); db.set_trace_callback(trace.append); return db
        with patch.object(self.repo,'_connect',side_effect=connect):
            self.events.patch('10',{'title':'Changed'})
        inserts=[sql for sql in trace if sql.startswith('INSERT INTO events')]
        deletes=[sql for sql in trace if sql.startswith('DELETE FROM events')]
        self.assertEqual(len(inserts),1)
        self.assertEqual(deletes,[])

    def test_batch_delete_one_undo_restores_ids(self):
        original=[self.event(str(i)) for i in range(4)]
        self.repo.apply_changes({'events':original})
        with patch.object(self.repo,'_commit',wraps=self.repo._commit) as commit:
            self.events.bulk(['0','1','2','3'],'delete')
            self.assertEqual(commit.call_count,1)
        self.assertEqual(self.open().events,[])
        self.history.undo()
        self.assertEqual(self.open().events,original)

    def test_skip_day_endpoint_after_date_rule_change(self):
        item=self.event()
        self.events.upsert(item)
        self.assertEqual(self.store.event_end_date(item),date(2026,11,24))
        self.mutations.set_date_status(date(2026,11,23),'leave')
        reopened=self.open()
        self.assertEqual(reopened.event_end_date(reopened.events[0]),date(2026,11,25))

    def test_category_inheritance_and_override_persist(self):
        self.mutations.upsert_category(EventCategory('c','Category','#52B788'))
        self.events.upsert(replace(self.event('inherit'),category_id='c',color_mode='inherit'))
        self.events.upsert(replace(self.event('override'),category_id='c',color_mode='override',color='#6687F2'))
        self.mutations.upsert_category(replace(self.store.categories[0],color='#8B70D6'))
        reopened=self.open()
        self.assertEqual(reopened.effective_event_color(reopened.events[0]),'#8B70D6')
        self.assertEqual(reopened.effective_event_color(reopened.events[1]),'#6687F2')
        self.mutations.delete_category('c')
        reopened=self.open()
        self.assertIsNone(reopened.events[0].category_id)
        self.assertEqual(reopened.events[0].color,'#8B70D6')

    def test_backup_validation_failure_removes_only_new_target(self):
        self.events.upsert(self.event())
        target=self.path.with_name('failed.db')
        with patch.object(self.repo,'_validate',side_effect=DataCompatibilityError('validation')):
            with self.assertRaises(DataCompatibilityError): self.repo.backup_to(target)
        self.assertFalse(target.exists())
        self.assertEqual(len(self.open().events),1)
