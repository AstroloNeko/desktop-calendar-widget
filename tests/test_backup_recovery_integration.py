import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from calendar_storage_lifecycle import StorageLifecycle, LifecycleError, digest
from calendar_disaster_recovery import disaster_restore
from desktop_storage_startup import StartupOptions, open_desktop_storage
from desktop_backup import export_sqlite, automatic_sqlite_backup
from calendar_sqlite_backup import read_container
from update_storage_guard import check_archive, capabilities

FIXTURE = json.loads((Path(__file__).parent/'fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'calendar_data.json'
        self.source.write_text(json.dumps(FIXTURE), encoding='utf8')
        self.manager = StorageLifecycle(self.root, isolated=True)
        self.state = self.manager.migrate()
        self.db = self.root/self.state['database']
        self.backup = self.root/'safe.dcalbackup'
        self.manager.backup(self.backup)

    def session(self):
        session = open_desktop_storage(StartupOptions(self.root))
        self.addCleanup(session.close)
        return session

    def restore(self, backup=None):
        return self.manager.restore(backup or self.backup, authorize_replacement=True, reminder_replay_reviewed=True)

    def disaster(self, backup=None):
        return disaster_restore(self.manager, backup or self.backup,
            authorize_replacement=True, reminder_replay_reviewed=True)

    def test_live_backup_uses_backup_api_and_excludes_ai(self):
        session = self.session()
        from sqlite_calendar_repository import SqliteCalendarRepository
        original = SqliteCalendarRepository.backup_to
        called = []
        def tracked(repo, destination):
            called.append(True)
            return original(repo, destination)
        target = self.root/'live.dcalbackup'
        with patch.object(SqliteCalendarRepository, 'backup_to', tracked):
            export_sqlite(session.store, target)
        self.assertTrue(called)
        manifest, _, backend = read_container(target)
        self.assertEqual(backend, 'sqlite')
        self.assertIn('ChatJournal', manifest['excluded'])
        self.assertEqual(manifest['identity']['calendar_id'], self.state['identity']['calendar_id'])

    def test_export_failure_preserves_current_and_existing_backup(self):
        session = self.session()
        before = digest(self.backup)
        with patch('desktop_backup.create_sqlite_backup', side_effect=OSError('full')):
            with self.assertRaises(OSError): export_sqlite(session.store, self.backup, overwrite=True)
        self.assertEqual(digest(self.backup), before)
        self.assertEqual(self.manager._select()[0]['epoch'], self.state['epoch'])

    def test_daily_sqlite_never_reads_archived_json(self):
        session = self.session()
        self.source.write_bytes(b'old writer')
        target = automatic_sqlite_backup(session.store)
        self.assertEqual(read_container(target)[2], 'sqlite')
        self.assertEqual(automatic_sqlite_backup(session.store), target)

    def test_normal_restore_requires_offline_and_consent(self):
        session = self.session()
        with self.assertRaisesRegex(LifecycleError, 'directory_busy'): self.restore()
        session.close()
        with self.assertRaisesRegex(LifecycleError, 'explicit_authorization'): self.manager.restore(self.backup)

    def test_restore_new_epoch_and_settings_ledger(self):
        before = self.session()
        settings, notified = dict(before.store.settings), set(before.store.notified)
        old_store = before.store
        before.close()
        state = self.restore()
        after = self.session()
        self.assertNotEqual(state['epoch'], self.state['epoch'])
        self.assertEqual(after.store.settings, settings)
        self.assertEqual(after.store.notified, notified)
        with self.assertRaises(RuntimeError): old_store.repository.revision()

    def test_pending_journal_epoch_isolation_configuration_retained(self):
        from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
        from desktop_calendar.assistant.calendar_chat_tools import ChatError
        from test_calendar_runtime import EphemeralProtection
        session = self.session()
        protector = EphemeralProtection()
        journal = ChatJournal(session.journal_path, protector)
        journal.put('operation', 'old-pending', 'owner', {'status':'pending'})
        config = self.root/'ai-assistant/configuration.dpapi'
        config.write_bytes(b'isolated sentinel')
        session.close()
        self.restore()
        restored = self.session()
        fresh = ChatJournal(restored.journal_path, protector)
        with self.assertRaises(ChatError): fresh.get('operation', 'old-pending', 'owner')
        self.assertEqual(journal.get('operation', 'old-pending', 'owner')['status'], 'pending')
        self.assertEqual(config.read_bytes(), b'isolated sentinel')

    def test_legacy_schema8_and_schema9_backups(self):
        from data_backup import create_backup
        from calendar_core import Store
        for version in (8,9):
            legacy = self.root/f'legacy{version}.dcalbackup'
            data = self.source.read_bytes()
            if version == 9:
                isolated = Store(self.root/'conversion.json')
                isolated.save()
                data = isolated.data_file.read_bytes()
            create_backup(data, legacy)
            state = self.restore(legacy)
            self.assertEqual(state['state'], 'ACTIVE')

    def test_changed_backup_after_review_refused(self):
        with self.assertRaisesRegex(LifecycleError, 'backup_changed'):
            self.manager.restore(self.backup, authorize_replacement=True, expected_backup_hash='0'*64)
        self.assertEqual(self.manager._read()['epoch'], self.state['epoch'])

    def test_corrupt_epoch_refuses_before_opening_runtime_or_journal(self):
        state = self.restore()
        state['epoch'] = '../../outside'
        self.manager._write(state)
        with self.assertRaisesRegex(LifecycleError, 'invalid_storage_epoch'):
            self.session()

    def test_corrupt_db_wal_shm_archived_byte_for_byte(self):
        self.db.write_bytes(b'damaged database')
        wal = Path(str(self.db)+'-wal');wal.write_bytes(b'damaged WAL')
        shm = Path(str(self.db)+'-shm');shm.write_bytes(b'damaged SHM')
        state = self.disaster()
        for path in (self.db, wal, shm):
            self.assertEqual((self.root/state['archive']/path.name).read_bytes(), path.read_bytes())
        self.assertTrue(self.session().store.events)

    def test_missing_database_can_restore_without_empty_initialization(self):
        self.db.unlink()
        self.disaster()
        self.assertFalse(self.db.exists())
        self.assertTrue(self.session().sqlite)

    def test_corrupt_metadata_can_recover_with_witness(self):
        self.manager.state_path.write_bytes(b'corrupt metadata')
        state = self.disaster()
        self.assertEqual((self.root/state['archive']/'storage-state.json').read_bytes(), b'corrupt metadata')
        self.assertTrue(self.session().sqlite)

    def test_invalid_backup_preserves_corrupt_source_and_state(self):
        self.db.write_bytes(b'broken')
        before = self.manager.state_path.read_bytes()
        bad = self.root/'bad.dcalbackup';bad.write_bytes(b'bad')
        with self.assertRaises(LifecycleError): self.disaster(bad)
        self.assertEqual(self.db.read_bytes(), b'broken')
        self.assertEqual(self.manager.state_path.read_bytes(), before)
        with self.assertRaises(LifecycleError): self.session()

    def test_archive_failure_never_changes_authority(self):
        self.db.write_bytes(b'broken')
        with patch('calendar_disaster_recovery.durable_new', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.disaster()
        self.assertEqual(self.manager._read()['epoch'], self.state['epoch'])
        self.assertEqual(self.db.read_bytes(), b'broken')

    def test_offline_restore_process_crash_and_resume(self):
        for point in ('ARCHIVED','STAGED','ACTIVATING','PLACED'):
            with self.subTest(point=point):
                active = self.manager._read()
                (self.root/active['database']).write_bytes(b'broken before crash test')
                script = "from calendar_storage_lifecycle import StorageLifecycle; from calendar_disaster_recovery import disaster_restore; import sys,os; m=StorageLifecycle(sys.argv[1],isolated=True,checkpoint=lambda p:os._exit(74) if p==sys.argv[3] else None); disaster_restore(m,sys.argv[2],authorize_replacement=True,reminder_replay_reviewed=True)"
                result = subprocess.run([sys.executable,'-c',script,str(self.root),str(self.backup),point],capture_output=True)
                self.assertEqual(result.returncode,74,result.stderr)
                if point == 'ARCHIVED':
                    with self.assertRaises(LifecycleError): self.manager._select()
                    self.disaster()
                else:
                    with self.manager.lease():
                        self.assertEqual(self.manager._select()[0]['state'],'ACTIVE')

    def test_old_json_not_adopted_after_restore(self):
        self.restore()
        self.source.write_bytes(b'old EXE changes')
        self.assertTrue(self.session().store.events)

    def test_updater_rejects_missing_or_wrong_capabilities(self):
        archive = self.root/'old.zip'
        with zipfile.ZipFile(archive,'w') as package: package.writestr('DesktopCalendar/app-version.txt','0.10.2')
        with self.assertRaises(LifecycleError): check_archive(archive,self.root)
        with zipfile.ZipFile(archive,'w') as package:
            package.writestr('DesktopCalendar/app-version.txt','0.11.0')
            package.writestr('DesktopCalendar/storage-capabilities.json',json.dumps(capabilities('0.10.2')))
        with self.assertRaises(LifecycleError): check_archive(archive,self.root)

    def test_updater_accepts_matching_capabilities(self):
        archive = self.root/'new.zip'
        with zipfile.ZipFile(archive,'w') as package:
            package.writestr('DesktopCalendar/app-version.txt','0.11.0')
            package.writestr('DesktopCalendar/storage-capabilities.json',json.dumps(capabilities('0.11.0')))
        check_archive(archive,self.root)

    def test_old_payload_never_stages_updater(self):
        from update_payload import stage_verified_updater, PayloadError
        archive = self.root/'old.zip'
        with zipfile.ZipFile(archive,'w') as package:
            package.writestr('DesktopCalendar/DesktopCalendarUpdater.exe', b'MZold')
        with patch('update_storage_guard.profile_root', return_value=self.root), \
                patch('update_payload.tempfile.mkdtemp') as stage:
            with self.assertRaises(PayloadError): stage_verified_updater(archive)
            stage.assert_not_called()

    def test_updater_gate_copy_and_restart_are_ordered(self):
        import updater
        from types import SimpleNamespace
        from calendar_storage_lifecycle import DirectoryLease
        source = self.root/'payload';source.mkdir()
        before = self.manager.state_path.read_bytes()
        with tempfile.TemporaryDirectory() as install:
            args = SimpleNamespace(pid=123, archive=self.root/'download.zip', install_dir=Path(install), executable='DesktopCalendar.exe')
            for compatible, copy_fails in ((False,False),(True,True),(True,False)):
                if compatible:
                    (source/'app-version.txt').write_text('0.11.0')
                    (source/'storage-capabilities.json').write_text(json.dumps(capabilities('0.11.0')))
                calls=[]
                def copy(*args):
                    with self.assertRaisesRegex(LifecycleError,'directory_busy'):
                        with DirectoryLease(self.root): pass
                    calls.append('copy')
                    if copy_fails: raise OSError('synthetic failed copy')
                with patch('updater.parse_args',return_value=args), \
                        patch('updater.wait_for_process',side_effect=lambda pid:calls.append('wait')), \
                        patch('updater.ensure_no_calendar_instance',side_effect=lambda:calls.append('instances')), \
                        patch('updater.safe_extract'), patch('updater.payload_root',return_value=source), \
                        patch('update_storage_guard.profile_root',return_value=self.root), \
                        patch('updater.copy_payload',side_effect=copy), patch('updater.show_error'), \
                        patch('updater.subprocess.Popen',side_effect=lambda *a,**k:calls.append('restart')):
                    result=updater.main()
                self.assertEqual(result,0 if compatible and not copy_fails else 1)
                if not compatible:self.assertNotIn('copy',calls)
                if copy_fails or not compatible:self.assertNotIn('restart',calls)
                else:self.assertEqual(calls,['wait','instances','copy','restart'])
                self.assertEqual(self.manager.state_path.read_bytes(),before)
        for backup in (self.root/'backups').glob('before-update-*.dcalbackup'):
            self.assertEqual(read_container(backup)[2],'sqlite')
