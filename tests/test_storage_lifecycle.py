from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from calendar_storage_lifecycle import StorageLifecycle,LifecycleError,DirectoryLease

FIXTURE=json.loads((Path(__file__).parent/'fixtures'/'schema8_v0102.json').read_text(encoding='utf8'))['document']

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'calendar_data.json'
        self.source.write_text(json.dumps(FIXTURE),encoding='utf8')
        self.manager=StorageLifecycle(self.root,isolated=True)
    def test_explicit_isolation(self):
        with self.assertRaises(LifecycleError):StorageLifecycle(self.root)
    def test_not_started_ignores_stray_database(self):
        (self.root/'old.sqlite3').write_bytes(b'not authority')
        state,path,factory=self.manager._select()
        self.assertEqual(state['state'],'NOT_STARTED');self.assertIsNone(factory)
    def test_activation_and_original(self):
        original=self.source.read_bytes();state=self.manager.migrate()
        self.assertEqual(state['state'],'ACTIVE')
        self.assertEqual((self.root/state['protection']).read_bytes(),original)
        self.assertEqual(self.source.read_bytes(),original)
        self.assertEqual(self.manager._select()[0]['state'],'ACTIVE')
    def test_duplicate_migration_refused(self):
        self.manager.migrate()
        with self.assertRaisesRegex(LifecycleError,'already_active'):self.manager.migrate()
    def test_active_corruption_never_json(self):
        state=self.manager.migrate();(self.root/state['database']).write_bytes(b'broken')
        with self.assertRaises(LifecycleError):self.manager._select()
        self.assertTrue(self.source.exists())
    def test_missing_state_never_json(self):
        self.manager.migrate();self.manager.state_path.unlink()
        with self.assertRaisesRegex(LifecycleError,'state_missing'):self.manager._select()
    def test_corrupt_state(self):
        self.manager.state_path.write_text('{}')
        with self.assertRaisesRegex(LifecycleError,'state_corrupt'):self.manager._select()
    def test_old_writer_ignored_after_active(self):
        state=self.manager.migrate();self.source.write_text('{"version":8}')
        self.assertEqual(self.manager._select()[1],self.root/state['database'])
    def test_lease_conflict(self):
        with self.manager.lease():
            with self.assertRaisesRegex(LifecycleError,'directory_busy'):
                with DirectoryLease(self.root):pass
    def test_real_process_crashes_and_recovery(self):
        for point in ('STAGED','ACTIVATING','PLACED','ACTIVE'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'calendar_data.json').write_text(json.dumps(FIXTURE))
                script="from calendar_storage_lifecycle import StorageLifecycle; import os,sys; StorageLifecycle(sys.argv[1],isolated=True,checkpoint=lambda p: os._exit(73) if p==sys.argv[2] else None).migrate()"
                result=subprocess.run([sys.executable,'-c',script,str(root),point],capture_output=True)
                self.assertEqual(result.returncode,73,result.stderr)
                manager=StorageLifecycle(root,isolated=True)
                with manager.lease():self.assertEqual(manager._select()[0]['state'],'ACTIVE')
    def test_source_changed_before_activation(self):
        def change(point):
            if point=='STAGED':self.source.write_text('{}')
        self.manager.checkpoint=change
        with self.assertRaisesRegex(LifecycleError,'source_changed'):self.manager.migrate()
    def test_protection_failure_blocks_start(self):
        from calendar_storage_lifecycle import durable_new
        def fail(path,data):
            if path.name.startswith('original-'):raise OSError('disk full')
            return durable_new(path,data)
        with patch('calendar_storage_lifecycle.durable_new',side_effect=fail):
            with self.assertRaises(OSError):self.manager.migrate()
        with self.assertRaisesRegex(LifecycleError,'recovery_required'):self.manager._select()
    def test_runtime_backup_restore_and_epoch(self):
        from datetime import date
        from test_calendar_runtime import EphemeralProtection
        from test_calendar_chat import ScriptedProvider
        options=dict(protector=EphemeralProtection(),provider=ScriptedProvider([]))
        initial=self.manager.migrate()
        with self.manager.runtime(**options) as runtime:
            identifier=runtime.events.create_quick('Synthetic added',date(2026,11,20)).event_ids[0]
            from calendar_core import RoutineItem
            habit=RoutineItem.from_dict(runtime.snapshot()['routines'][0]);habit.title='Synthetic changed habit'
            runtime.mutations.upsert_routine(habit)
            session=runtime.service.new_session()['session_id']
            runtime._core.journal.put('pending','old-pending','desktop',{'id':'old-pending','session_id':session})
            with self.assertRaisesRegex(LifecycleError,'directory_busy'):self.manager.backup(self.root/'busy.dcalbackup')
        backup=self.root/'snapshot.dcalbackup';self.manager.backup(backup)
        with self.manager.runtime(**options) as runtime:
            runtime.events.delete(identifier)
        with self.assertRaisesRegex(LifecycleError,'explicit_authorization'):self.manager.restore(backup)
        restored=self.manager.restore(backup,authorize_replacement=True)
        self.assertNotEqual(initial['epoch'],restored['epoch'])
        with self.manager.runtime(**options) as runtime:
            self.assertIn(identifier,[e['id'] for e in runtime.snapshot()['events']])
            self.assertIsNone(runtime.undo())
            self.assertIn(restored['epoch'],str(runtime._core.configuration.folder))
            from calendar_chat_tools import ChatError
            with self.assertRaises(ChatError):runtime._core.journal.get('pending','old-pending','desktop')
            with self.assertRaises(ChatError):runtime.service.send_message(session,'继续','stale-request')
            self.assertEqual(runtime.snapshot()['routines'][0]['title'],'Synthetic changed habit')
        self.source.write_text('{}')
        with self.manager.runtime(**options) as runtime:
            self.assertIn(identifier,[e['id'] for e in runtime.snapshot()['events']])
    def test_backup_tamper(self):
        import zipfile
        from calendar_sqlite_backup import read_container
        path=self.root/'bad.dcalbackup'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('../calendar.sqlite3',b'x');z.writestr('manifest.json','{}')
        with self.assertRaises(LifecycleError):read_container(path)
    def test_backup_credentials_refused(self):
        from calendar_core import Store
        from sqlite_calendar_repository import SqliteCalendarRepository
        self.manager.migrate();state,path,factory=self.manager._select()
        store=Store(path,repository_factory=factory)
        store.settings['api_key']='synthetic-secret';store.save();store.repository.close()
        with self.assertRaisesRegex(LifecycleError,'credential'):self.manager.backup(self.root/'secret.dcalbackup')
        self.assertFalse((self.root/'secret.dcalbackup').exists())
    def test_legacy_backup_restore(self):
        import base64
        from tools.generate_schema8_fixture import generate
        self.manager.migrate();backup=self.root/'legacy.dcalbackup'
        backup.write_bytes(base64.b64decode(generate(backup=True)['backup']))
        state=self.manager.restore(backup,authorize_replacement=True)
        self.assertEqual(state['state'],'ACTIVE')
    def test_restore_crashes(self):
        for point in ('STAGED','ACTIVATING','PLACED','ACTIVE'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'calendar_data.json').write_text(json.dumps(FIXTURE))
                manager=StorageLifecycle(root,isolated=True);manager.migrate()
                backup=root/'backup.dcalbackup';manager.backup(backup)
                script="from calendar_storage_lifecycle import StorageLifecycle; import os,sys; StorageLifecycle(sys.argv[1],isolated=True,checkpoint=lambda p: os._exit(74) if p==sys.argv[2] else None).restore(sys.argv[3],authorize_replacement=True)"
                process=subprocess.run([sys.executable,'-c',script,str(root),point,str(backup)],capture_output=True)
                self.assertEqual(process.returncode,74,process.stderr)
                with manager.lease():self.assertEqual(manager._select()[0]['state'],'ACTIVE')
    def test_staged_tamper_rejected(self):
        for target in ('candidate','protection','source','report'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'calendar_data.json').write_text(json.dumps(FIXTURE))
                def stop(point):
                    if point=='STAGED':raise RuntimeError('stop')
                manager=StorageLifecycle(root,isolated=True,checkpoint=stop)
                with self.assertRaises(RuntimeError):manager.migrate()
                state=manager._read();path=root/state[target]
                if target=='report':path=path/'migration-report.json'
                path.write_bytes(b'changed')
                manager.checkpoint=lambda point:None
                with self.assertRaises((LifecycleError,OSError)):manager.migrate()
    def test_preparing_process_crash_blocks(self):
        script="from calendar_storage_lifecycle import StorageLifecycle; import os,sys; StorageLifecycle(sys.argv[1],isolated=True,checkpoint=lambda p: os._exit(75) if p=='PREPARING' else None).migrate()"
        self.assertEqual(subprocess.run([sys.executable,'-c',script,str(self.root)]).returncode,75)
        with self.assertRaisesRegex(LifecycleError,'recovery_required'):self.manager._select()
        self.manager.abandon_preparation()
        self.assertEqual(self.manager.migrate()['state'],'ACTIVE')
    def test_active_cannot_abandon(self):
        self.manager.migrate()
        with self.assertRaisesRegex(LifecycleError,'cannot_abandon'):self.manager.abandon_preparation()
    def test_restore_corrupt_archive_keeps_authority(self):
        state=self.manager.migrate();backup=self.root/'bad';backup.write_bytes(b'bad')
        with self.assertRaises(LifecycleError):self.manager.restore(backup,authorize_replacement=True)
        self.assertEqual(self.manager._select()[0]['epoch'],state['epoch'])
    def test_missing_active_database_is_not_initialized(self):
        state=self.manager.migrate();db=self.root/state['database'];db.unlink()
        with self.assertRaisesRegex(LifecycleError,'database_missing'):self.manager._select()
        self.assertFalse(db.exists())
    def test_process_lease(self):
        script="from calendar_storage_lifecycle import StorageLifecycle,LifecycleError; import sys; m=StorageLifecycle(sys.argv[1],isolated=True); m.migrate()"
        with self.manager.lease():
            result=subprocess.run([sys.executable,'-c',script,str(self.root)],capture_output=True)
            self.assertNotEqual(result.returncode,0);self.assertIn(b'directory_busy',result.stderr)
    def test_injected_migration_failures(self):
        points=('calendar_storage_lifecycle.dry_run_migration','calendar_storage_lifecycle.inspect_database',
                'calendar_storage_lifecycle.os.link','sqlite_calendar_repository.SqliteCalendarRepository.backup_to')
        for point in points:
            with self.subTest(point=point),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);source=root/'calendar_data.json';source.write_text(json.dumps(FIXTURE));before=source.read_bytes()
                manager=StorageLifecycle(root,isolated=True)
                with patch(point,side_effect=OSError('injected storage failure')):
                    with self.assertRaises(OSError):manager.migrate()
                self.assertEqual(source.read_bytes(),before)
                self.assertNotEqual(manager._read()['state'],'ACTIVE')
    def test_new_backup_manifest_and_member_failures(self):
        import zipfile
        from calendar_sqlite_backup import read_container
        self.manager.migrate();good=self.root/'good';self.manager.backup(good)
        with zipfile.ZipFile(good) as z:manifest=z.read('manifest.json');data=z.read('calendar.sqlite3')
        for mode in ('hash','version','duplicate','symlink','oversized'):
            with self.subTest(mode=mode):
                bad=self.root/mode
                with zipfile.ZipFile(bad,'w',zipfile.ZIP_DEFLATED) as z:
                    m=json.loads(manifest)
                    if mode=='version':m['format_version']=999
                    z.writestr('manifest.json',json.dumps(m))
                    info=zipfile.ZipInfo('calendar.sqlite3')
                    if mode=='symlink':info.external_attr=0o120777<<16
                    z.writestr(info,b'bad' if mode=='hash' else data)
                    if mode=='duplicate':z.writestr('calendar.sqlite3',data)
                if mode=='oversized':
                    with patch('calendar_sqlite_backup.LIMIT',1):
                        with self.assertRaises(LifecycleError):read_container(bad)
                else:
                    with self.assertRaises(LifecycleError):read_container(bad)
    def test_restore_reminder_acknowledgment(self):
        self.manager.migrate();backup=self.root/'b';self.manager.backup(backup);self.manager.restore(backup,authorize_replacement=True)
        with self.assertRaisesRegex(LifecycleError,'reminder_ledger'):
            with self.manager.runtime(notification_backend=object()):pass
    def test_loopback_http(self):
        from test_calendar_runtime import EphemeralProtection
        from test_calendar_chat import ScriptedProvider
        from calendar_chat_http import LocalChatHTTP
        import urllib.request
        self.manager.migrate()
        with self.manager.runtime(protector=EphemeralProtection(),provider=ScriptedProvider([])) as runtime:
            token='synthetic-test-token-'+'x'*40
            server=LocalChatHTTP(runtime.service,token,0)
            server.start()
            try:
                request=urllib.request.Request(f'http://127.0.0.1:{server.port}/v1/sessions',data=b'{}',
                    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response:self.assertIn('session_id',json.load(response))
            finally:server.stop(wait_requests=True)

if __name__=='__main__':unittest.main()
