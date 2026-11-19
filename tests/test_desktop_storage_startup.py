import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop_storage_startup import StartupOptions, open_desktop_storage
from desktop_calendar.storage.calendar_storage_lifecycle import StorageLifecycle, LifecycleError, DirectoryLease
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']


class DesktopStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'calendar_data.json'
        self.source.write_text(json.dumps(FIXTURE), encoding='utf8')
        self.original = self.source.read_bytes()
        self.options = StartupOptions(self.root)

    def open(self, **kwargs):
        session = open_desktop_storage(self.options, **kwargs)
        self.addCleanup(session.close)
        return session

    def activate(self):
        self.options = StartupOptions(self.root, True)
        return self.open(confirm=lambda: True)

    def test_schema8_json(self):
        session = self.open()
        self.assertFalse(session.sqlite)
        self.assertTrue(session.store.events)

    def test_cancel_keeps_json(self):
        self.options = StartupOptions(self.root, True)
        self.assertFalse(self.open(confirm=lambda: False).sqlite)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_activation_and_restart(self):
        session = self.activate()
        self.assertIsInstance(session.store.repository, SqliteCalendarRepository)
        from event_commands import EventCommandHistory
        from calendar_event_service import CalendarEventService
        from datetime import date
        history = EventCommandHistory(session.store)
        result = CalendarEventService(history).create_quick('Isolated C1', date(2026, 11, 16))
        identifier = result.event_ids[0]
        self.assertEqual(self.source.read_bytes(), self.original)
        session.close()
        reopened = self.open()
        self.assertIn(identifier, [event.id for event in reopened.store.events])

    def test_lifetime_lock_and_close(self):
        session = self.open()
        with self.assertRaisesRegex(LifecycleError, 'directory_busy'):
            open_desktop_storage(self.options)
        session.close()
        self.open()

    def test_missing_active_database_refused(self):
        session = self.activate()
        path = session.store.data_file
        session.close()
        path.unlink()
        with self.assertRaisesRegex(LifecycleError, 'database_missing'):
            self.open()
        self.assertFalse(path.exists())

    def test_corrupt_database_refused(self):
        session = self.activate()
        path = session.store.data_file
        session.close()
        path.write_bytes(b'broken')
        with self.assertRaises(LifecycleError): self.open()
        self.assertEqual(path.read_bytes(), b'broken')

    def test_missing_state_refused(self):
        session = self.activate()
        session.close()
        (self.root / 'storage-state.json').unlink()
        with self.assertRaisesRegex(LifecycleError, 'state_missing'): self.open()

    def test_corrupt_state_releases_lock(self):
        (self.root / 'storage-state.json').write_text('{}')
        with self.assertRaises(LifecycleError): self.open()
        with DirectoryLease(self.root): pass

    def test_invalid_source_not_changed(self):
        self.source.write_bytes(b'{invalid')
        self.options = StartupOptions(self.root, True)
        with self.assertRaisesRegex(LifecycleError, 'invalid_source'):
            self.open(confirm=lambda: True)
        self.assertEqual(self.source.read_bytes(), b'{invalid')

    def test_confirmation_source_change_refused(self):
        self.options = StartupOptions(self.root, True)
        def confirm():
            self.source.write_bytes(b'{}')
            return True
        with self.assertRaisesRegex(LifecycleError, 'source_changed'): self.open(confirm=confirm)

    def test_preparing_refused(self):
        def interrupt(point):
            if point == 'PREPARING': raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            StorageLifecycle(self.root, isolated=True, checkpoint=interrupt).migrate()
        with self.assertRaisesRegex(LifecycleError, 'recovery_required'): self.open()

    def test_staged_recovery(self):
        def interrupt(point):
            if point == 'STAGED': raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            StorageLifecycle(self.root, isolated=True, checkpoint=interrupt).migrate()
        self.assertTrue(self.open().sqlite)

    def test_production_cannot_migrate(self):
        manager = StorageLifecycle(self.root, existing_only=True)
        with self.assertRaisesRegex(LifecycleError, 'production_migration_disabled'): manager.migrate()
        with self.assertRaisesRegex(LifecycleError, 'sqlite_not_active'): manager.restore('unused')

    def test_real_directory_rejected_for_isolated_mode(self):
        with patch('desktop_calendar.storage.calendar_storage_lifecycle.DATA_DIR', self.root):
            with self.assertRaisesRegex(LifecycleError, 'explicit_isolated'): self.options.manager()

    def test_no_implicit_production_migration(self):
        with self.assertRaisesRegex(LifecycleError, 'explicit_isolated'):
            StartupOptions(offer_migration=True).manager()

    def test_explicit_upgrade_gate_production_path_in_isolated_profile(self):
        with patch('desktop_storage_startup.DATA_DIR', self.root), patch('desktop_calendar.storage.calendar_migration.DATA_DIR', self.root):
            options = StartupOptions(upgrade_storage=True)
            session = open_desktop_storage(options, confirm=lambda: True)
        self.addCleanup(session.close)
        self.assertTrue(session.sqlite)

    def test_new_profile_initializes_without_json_or_migration_dialog(self):
        self.source.unlink()
        from unittest.mock import Mock
        confirm = Mock(side_effect=AssertionError('not a migration'))
        notify = Mock(side_effect=AssertionError('not a migration'))
        with patch.object(StorageLifecycle, 'migrate', side_effect=AssertionError('no JSON migration')):
            session = self.open(confirm=confirm, notify=notify)
        self.assertTrue(session.sqlite)
        self.assertEqual(session.state['operation'], 'initialize')
        self.assertFalse(session.store.events)
        path = session.store.data_file
        epoch = session.state['epoch']
        from event_commands import EventCommandHistory
        from calendar_event_service import CalendarEventService
        from datetime import date
        created = CalendarEventService(EventCommandHistory(session.store)).create_quick('First SQLite task', date.today())
        session.close()
        self.assertFalse(self.source.exists())
        reopened = self.open(confirm=confirm, notify=notify)
        self.assertEqual((reopened.store.data_file, reopened.state['epoch']), (path, epoch))
        self.assertEqual([e.id for e in reopened.store.events], list(created.event_ids))
        self.assertEqual(list(self.root.glob('*.sqlite3')), [path])

    def test_legacy_empty_schema9_is_still_json(self):
        from calendar_core import Store
        self.source.unlink()
        Store(self.source).save()
        original = self.source.read_bytes()
        self.assertEqual(json.loads(original)['version'], 9)
        self.assertFalse(self.open().sqlite)
        self.assertFalse((self.root / 'storage-state.json').exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_unknown_profile_and_missing_metadata_do_not_initialize_or_fall_back(self):
        for name, legacy_source in (('calendar-old.sqlite3', True), ('historical.dcalbackup', False),
                                    ('storage-state.json', False), ('calendar_data.json.tmp', True)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                data = b'{"format":1,"state":"NOT_STARTED"}' if name == 'storage-state.json' else b'original'
                (root / name).write_bytes(data)
                if legacy_source:
                    (root / self.source.name).write_bytes(self.original)
                with self.assertRaisesRegex(LifecycleError, 'unrecognized'):
                    open_desktop_storage(StartupOptions(root))
                self.assertEqual((root / name).read_bytes(), data)
                self.assertFalse((root / '.storage-managed').exists())

    def test_interrupted_fresh_initialization_is_recoverable_or_refused(self):
        for stage in ('PREPARING', 'STAGED', 'ACTIVATING', 'PLACED'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                def interrupt(point):
                    if point == stage:
                        raise RuntimeError('synthetic process interruption')
                manager = StorageLifecycle(root, isolated=True, checkpoint=interrupt)
                with patch.object(StartupOptions, 'manager', return_value=manager):
                    with self.assertRaisesRegex(RuntimeError, 'interruption'):
                        open_desktop_storage(StartupOptions(root))
                if stage == 'PREPARING':
                    with self.assertRaisesRegex(LifecycleError, 'recovery_required'):
                        open_desktop_storage(StartupOptions(root))
                    self.assertFalse(list(root.glob('*.sqlite3')))
                else:
                    session = open_desktop_storage(StartupOptions(root))
                    try:
                        self.assertTrue(session.sqlite)
                        self.assertEqual(session.state['operation'], 'initialize')
                    finally:
                        session.close()
                self.assertFalse((root / self.source.name).exists())

    def test_legacy_writer_appearing_during_initialization_is_not_ignored(self):
        self.source.unlink()
        def old_program(point):
            if point == 'PLACED':
                self.source.write_bytes(self.original)
        manager = StorageLifecycle(self.root, isolated=True, checkpoint=old_program)
        with patch.object(StartupOptions, 'manager', return_value=manager):
            with self.assertRaisesRegex(LifecycleError, 'json_appeared'):
                self.open()
        self.assertEqual(manager._read()['state'], 'ACTIVATING')
        self.assertEqual(self.source.read_bytes(), self.original)
        with self.assertRaisesRegex(LifecycleError, 'json_appeared'):
            self.open()

    def test_legacy_json_import_is_consented_verified_and_limited_to_empty_sqlite(self):
        from desktop_calendar.storage.calendar_storage_lifecycle import digest, inspect_database
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            opts = StartupOptions(root)
            empty = open_desktop_storage(opts)
            original_epoch = empty.state['epoch']
            empty_identity = inspect_database(empty.store.data_file)
            empty.close()
            manager = opts.manager()
            with self.assertRaisesRegex(LifecycleError, 'explicit_review'):
                manager.import_legacy_json(self.source)
            with self.assertRaisesRegex(LifecycleError, 'changed_since_review'):
                manager.import_legacy_json(self.source, authorize_import=True, expected_source_hash='0' * 64)
            self.assertEqual(manager._read()['epoch'], original_epoch)
            state = manager.import_legacy_json(self.source, authorize_import=True, expected_source_hash=digest(self.source))
            self.assertEqual(state['operation'], 'import_json')
            self.assertNotEqual(state['epoch'], original_epoch)
            self.assertEqual(inspect_database(root / state['protection']), empty_identity)
            active = open_desktop_storage(opts)
            try:
                self.assertEqual({e.id for e in active.store.events}, {e['id'] for e in FIXTURE['events']})
                self.assertIn(state['epoch'], str(active.journal_path))
            finally:
                active.close()
            with self.assertRaisesRegex(LifecycleError, 'requires_empty_sqlite'):
                manager.import_legacy_json(self.source, authorize_import=True, expected_source_hash=digest(self.source))
            self.assertEqual(manager._read(), state)
            self.assertEqual(self.source.read_bytes(), self.original)
            self.assertFalse((root / self.source.name).exists())

    def test_fresh_production_launch_does_not_require_migration_permission(self):
        self.source.unlink()
        with patch('desktop_storage_startup.DATA_DIR', self.root):
            session = open_desktop_storage(StartupOptions())
        self.addCleanup(session.close)
        self.assertTrue(session.manager.existing_only)
        self.assertTrue(session.sqlite)

    def test_schema9_import_into_fresh_sqlite(self):
        from calendar_core import Store, Event
        from desktop_calendar.storage.calendar_storage_lifecycle import digest
        legacy = self.root / 'source-schema9.json'
        source = Store(legacy)
        source.events = [Event('schema9-id', 'Schema 9', '2026-11-16T10:00')]
        source.save()
        with tempfile.TemporaryDirectory() as folder:
            opts = StartupOptions(Path(folder))
            open_desktop_storage(opts).close()
            opts.manager().import_legacy_json(legacy, authorize_import=True, expected_source_hash=digest(legacy))
            session = open_desktop_storage(opts)
            try:
                self.assertEqual([event.id for event in session.store.events], ['schema9-id'])
            finally:
                session.close()

    def test_chat_folder_retained(self):
        folder = self.root / 'ai-assistant'
        folder.mkdir()
        marker = folder / 'existing-history'
        marker.write_bytes(b'isolated marker')
        self.activate()
        self.assertEqual(marker.read_bytes(), b'isolated marker')
        self.assertFalse(list(self.root.glob('runtime-*')))

    def test_activating_recovery(self):
        def interrupt(point):
            if point == 'ACTIVATING': raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            StorageLifecycle(self.root, isolated=True, checkpoint=interrupt).migrate()
        self.assertTrue(self.open().sqlite)

    def test_old_json_writer_not_authority(self):
        session = self.activate()
        expected = [event.id for event in session.store.events]
        session.close()
        self.source.write_bytes(b'{old program wrote this}')
        self.assertEqual([event.id for event in self.open().store.events], expected)

    def test_factory_failure_closes_session(self):
        from desktop_storage_startup import create_desktop_app
        class BrokenApp:
            def __init__(self, **kwargs): raise ValueError('UI construction failed')
            def stop_native_reminders(self): pass
        with self.assertRaisesRegex(ValueError, 'UI construction'):
            create_desktop_app(self.options, BrokenApp)
        with DirectoryLease(self.root): pass

    def test_factory_never_constructs_ui_on_bad_authority(self):
        from desktop_storage_startup import create_desktop_app
        (self.root / '.storage-managed').write_bytes(b'witness')
        class ForbiddenApp:
            def __init__(self, **kwargs): raise AssertionError('UI must not start')
        with self.assertRaisesRegex(LifecycleError, 'state_missing'):
            create_desktop_app(self.options, ForbiddenApp)

    def test_shared_mutations_undo_restart(self):
        from calendar_mutation_service import CalendarMutationService
        from event_commands import EventCommandHistory
        from calendar_core import RoutineItem
        from datetime import date
        session = self.activate()
        history = EventCommandHistory(session.store)
        service = CalendarMutationService(history)
        habit = deepcopy(session.store.routines[0])
        old = habit.title
        habit.title = 'C1 renamed'
        service.upsert_routine(habit)
        history.undo()
        self.assertEqual(session.store.routines[0].title, old)
        history.redo()
        service.set_date_status(date(2026, 11, 16), 'leave')
        session.close()
        restarted = self.open()
        self.assertEqual(restarted.store.routines[0].title, 'C1 renamed')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_reminder_device_preferences_preserved(self):
        before = self.open()
        settings = dict(before.store.settings)
        notified = deepcopy(before.store.notified)
        before.close()
        after = self.activate()
        self.assertEqual(after.store.settings, settings)
        self.assertEqual(after.store.notified, notified)

    def test_restore_requires_review_before_any_ui_services(self):
        session = self.activate()
        session.close()
        manager = StorageLifecycle(self.root, isolated=True)
        backup = self.root / 'isolated.dcalbackup'
        manager.backup(backup)
        manager.restore(backup, authorize_replacement=True)
        with self.assertRaisesRegex(LifecycleError, 'restored_desktop_requires_review'):
            self.open()
        with DirectoryLease(self.root): pass

    def test_source_change_revalidated_inside_migration_lease(self):
        manager = StorageLifecycle(self.root, isolated=True)
        with self.assertRaisesRegex(LifecycleError, 'source_changed'):
            manager.migrate(expected_source_hash='0' * 64)
        self.assertFalse(manager.witness.exists())

    def test_shutdown_stops_producers_before_unlock(self):
        from types import SimpleNamespace
        from desktop_storage_startup import stop_desktop_storage
        session = self.activate()
        calls = []
        def stopping():
            self.assertFalse(session.closed)
            with self.assertRaisesRegex(LifecycleError, 'directory_busy'):
                with DirectoryLease(self.root): pass
            calls.append('stopped')
        app = SimpleNamespace(storage_session=session,
            lifecycle=SimpleNamespace(exiting=False, stopped=False),
            chat_runtime=SimpleNamespace(stop=stopping),
            stop_native_reminders=stopping, tray_icon=SimpleNamespace(stop=stopping))
        stop_desktop_storage(app)
        self.assertEqual(len(calls), 3)
        self.assertTrue(app.lifecycle.stopped and session.closed)
        stop_desktop_storage(app)
        self.assertEqual(len(calls), 3)

    def test_backup_ui_never_passes_sqlite_to_json_tools(self):
        from backup_ui import BackupActionsMixin
        from types import SimpleNamespace
        session = self.activate()
        class App(BackupActionsMixin):
            store = session.store
            update_busy = False
            def _active_modal_window(self): return None
            def _data_message(self, *args): pass
            def _owned_data_dialog(self, function): return function()
        with patch('backup_ui.create_backup') as export, patch('backup_ui.restore_store') as restore, \
                patch('backup_ui.filedialog.asksaveasfilename', return_value=''), \
                patch('backup_ui.filedialog.askopenfilename', return_value=''):
            App().export_backup()
            App().restore_data_backup()
            export.assert_not_called()
            restore.assert_not_called()

    def test_production_options_select_without_migration(self):
        with patch('desktop_storage_startup.DATA_DIR', self.root):
            session = open_desktop_storage(StartupOptions())
        self.addCleanup(session.close)
        self.assertFalse(session.sqlite)
        self.assertTrue(session.manager.existing_only)

    def test_competing_staged_source_must_match_consent(self):
        def interrupt(point):
            if point == 'STAGED': raise RuntimeError('interrupted')
        manager = StorageLifecycle(self.root, isolated=True, checkpoint=interrupt)
        with self.assertRaises(RuntimeError): manager.migrate()
        manager.checkpoint = lambda point: None
        with self.assertRaisesRegex(LifecycleError, 'source_changed'):
            manager.migrate(expected_source_hash='0' * 64)
        self.assertEqual(manager._read()['state'], 'STAGED')
