"""All data and install mutations use synthetic, isolated fixtures."""
from dataclasses import asdict
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from calendar_core import DATA_SCHEMA_VERSION, Event, EventCategory, RoutineItem, Store
from data_backup import BackupError, create_backup, read_backup, restore_store, validate_data
from data_compat import DataCompatibilityError, DataVersionTooNew, decode_calendar_data
from event_commands import EventCommandHistory
from update_manager import UpdateError, launch_updater
from update_payload import stage_verified_updater
import updater
import update_state


class DataCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "data.json"

    def payload(self, schema=7):
        return json.dumps({"version": schema, "events": [asdict(Event("original", "Synthetic", "2026-10-23T10:00", event_type="ddl"))],
            "categories": [asdict(EventCategory("work", "Work", "#6687F2"))],
            "routines": [{"id": "habit", "title": "Synthetic habit", "created_on": "2026-10-19", "habit_done": ["2026-10-19"]}],
            "settings": {"theme": "paper"}}, ensure_ascii=False).encode()

    def test_schema7_migration_preserves_ids_categories_habits_and_checkpoint_bytes(self):
        original = self.payload()
        self.path.write_bytes(original)
        store = Store(self.path)
        store.save()
        loaded = Store(self.path)
        self.assertIsNone(loaded.load_error)
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 8)
        self.assertEqual(loaded.events[0].id, "original")
        self.assertEqual(loaded.categories[0].id, "work")
        self.assertEqual(loaded.routines[0].habit_done, ["2026-10-19"])
        self.assertEqual(loaded.routines[0].frequency, "daily")
        backups = list((self.root / "backups").glob("*.dcalbackup"))
        self.assertEqual(read_backup(backups[0])[1], original)

    def test_supported_schema8_habit2_and_bulk_ids_survive_restart_backup_restore(self):
        self.path.write_bytes(self.payload())
        store = Store(self.path)
        store.routines[0] = RoutineItem("habit", "Synthetic", frequency="weekly", interval=2, weekdays=[0, 2, 4],
            start_on="2026-10-19", workdays_only=False, habit_done=["2026-10-19"], reminder_enabled=True, reminder_time="09:30")
        store.events.append(Event("second", "Synthetic", "2026-10-24T10:00", duration_days=4, end_as_ddl=True))
        commands = EventCommandHistory(store)
        commands.bulk(("original", "second"), "shift", days=2)
        commands.bulk(("original", "second"), "delete")
        commands.undo()
        expected_events, expected_habits = [asdict(e) for e in store.events], [asdict(h) for h in store.routines]
        archive = self.root / "snapshot.dcalbackup"
        create_backup(self.path.read_bytes(), archive)
        store.events.clear()
        store.save()
        restore_store(store, archive)
        loaded = Store(self.path)
        self.assertEqual([asdict(e) for e in loaded.events], expected_events)
        self.assertEqual([asdict(h) for h in loaded.routines], expected_habits)

    def test_too_new_schema_refused_on_load_and_save_bytes_unchanged(self):
        for schema, supported in ((8, 7), (9, 8)):
            with self.subTest(schema=schema):
                original = self.payload(schema)
                self.path.write_bytes(original)
                with patch("calendar_core.DATA_SCHEMA_VERSION", supported), patch("calendar_core.__version__", "0.9.1"):
                    store = Store(self.path)
                    self.assertIsNotNone(store.load_error)
                    self.assertEqual(store.events, [])
                    self.assertIn(f"Schema {schema}", store.load_error)
                    with self.assertRaises(DataCompatibilityError):
                        store.save()
                self.assertEqual(self.path.read_bytes(), original)
                self.assertFalse((self.root / "backups").exists())
                self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_already_open_older_store_cannot_overwrite_newer_disk_schema(self):
        self.path.write_bytes(self.payload(7))
        with patch("calendar_core.DATA_SCHEMA_VERSION", 7):
            old = Store(self.path)
            old.events[0].title = "Stale in-memory edit"
            newer = self.payload(8)
            self.path.write_bytes(newer)
            with self.assertRaises(DataVersionTooNew):
                old.save()
        self.assertEqual(self.path.read_bytes(), newer)

    def test_bad_data_is_not_confused_with_future_schema_or_overwritten(self):
        for bad in (b"broken json", b"[]", b'{"version":true}', b'{"version":"8"}', b'{"version":0}'):
            self.path.write_bytes(bad)
            store = Store(self.path)
            self.assertIsNotNone(store.load_error)
            with self.assertRaises(DataCompatibilityError):
                store.save()
            self.assertEqual(self.path.read_bytes(), bad)

    def test_access_failure_distinct_and_never_saves(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            store = Store(self.path)
        self.assertIn("无法访问", store.load_error)
        with self.assertRaises(DataCompatibilityError):
            store.save()
        self.assertFalse(self.path.exists())

    def test_backup_format_incompatibility_is_distinct_from_data_schema(self):
        archive = self.root / "bad.dcalbackup"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("manifest.json", json.dumps({"format": "DesktopCalendarBackup", "format_version": 99}))
            package.writestr("calendar_data.json", self.payload(8))
        with self.assertRaisesRegex(BackupError, "备份容器格式版本"):
            read_backup(archive)
        with patch("data_backup.SCHEMA_VERSION", 7), self.assertRaisesRegex(BackupError, "Schema 8"):
            validate_data(self.payload(8))

    def test_v091_real_tag_readers_reproduce_incident_without_writes(self):
        """Actual archived source, not a pretend old-schema reader."""
        repository = Path(__file__).resolve().parents[1]
        legacy = self.root / "v091"
        legacy.mkdir()
        files = ("calendar_core.py", "data_backup.py", "version.py", "ui_theme.py", "holiday_data.py")
        for name in files:
            result = subprocess.run(["git", "show", f"v0.9.1:{name}"], cwd=repository, capture_output=True)
            if result.returncode:
                self.skipTest("v0.9.1 tag sources unavailable; not claiming legacy execution")
            (legacy / name).write_bytes(result.stdout)
        probe = ("from calendar_core import Store\nfrom pathlib import Path\nimport json\n"
                 "p=Path('data.json')\noriginal=p.read_bytes()\ns=Store(p)\n"
                 "assert s.load_error is None\n"
                 "if json.loads(original)['version']==7:\n s.save(); assert json.loads(p.read_bytes())['version']==7\n"
                 "else:\n"
                 " try: s.save()\n"
                 " except ValueError: pass\n"
                 " else: raise AssertionError('legacy guard expected')\n"
                 " assert p.read_bytes()==original\n")
        for schema in (7, 8):
            (legacy / "data.json").write_bytes(self.payload(schema))
            run = subprocess.run([sys.executable, "-c", probe], cwd=legacy, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode(errors="replace"))


class HandoffTests(unittest.TestCase):
    def fake(self):
        calls = []
        app = SimpleNamespace(update_dialog=Mock(), update_busy=True,
            _save_window_settings=lambda: calls.append("save"),
            _close_update_dialog=Mock(), on_close=Mock(),
            after_idle=lambda callback: calls.append("exit-scheduled"))
        return app, calls

    def test_all_saves_precede_launch_and_success_schedules_exit(self):
        from app import CalendarApp
        app, calls = self.fake()
        with patch("app.launch_updater", side_effect=lambda path: calls.append("launch")):
            CalendarApp._finish_update_download(app, Path("verified.zip"), None)
        self.assertEqual(calls, ["save", "launch", "exit-scheduled"])
        self.assertTrue(app._update_handoff)

    def test_save_failure_closes_progress_does_not_launch_or_schedule_exit(self):
        from app import CalendarApp
        app, calls = self.fake()
        app._save_window_settings = Mock(side_effect=DataCompatibilityError("Schema 8 vs 7"))
        with patch("app.launch_updater") as launch, patch("app.owned_messagebox") as message:
            CalendarApp._finish_update_download(app, Path("verified.zip"), None)
        launch.assert_not_called()
        app._close_update_dialog.assert_called_once()
        message.assert_called_once()
        self.assertFalse(app.update_busy)
        self.assertEqual(calls, [])

    def test_launch_failure_closes_progress_and_keeps_app_alive(self):
        from app import CalendarApp
        app, calls = self.fake()
        with patch("app.launch_updater", side_effect=OSError("cannot start")), patch("app.owned_messagebox"):
            CalendarApp._finish_update_download(app, Path("verified.zip"), None)
        self.assertEqual(calls, ["save"])
        self.assertFalse(app.update_busy)
        self.assertFalse(getattr(app, "_update_handoff", False))

    def test_exit_after_handoff_never_saves_again(self):
        from app import CalendarApp
        app = SimpleNamespace(_global_window_move=None, _clear_feedback=Mock(), _cancel_schedule_drag=Mock(),
            search_window=None, search_service=None, _save_window_settings=Mock(), _update_handoff=True,
            instance_guard=Mock(), tray_icon=None, _startup_foreground_restore_job=None, destroy=Mock())
        CalendarApp.on_close(app)
        app._save_window_settings.assert_not_called()
        app.instance_guard.close.assert_called_once()
        app.destroy.assert_called_once()

    def test_launch_uses_new_archive_updater_never_old_install_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old install", root / "staged new"
            old.mkdir(); new.mkdir()
            (old / "DesktopCalendarUpdater.exe").write_bytes(b"MZold-schema7")
            staged = new / "DesktopCalendarUpdater.exe"
            staged.write_bytes(b"MZnew-schema8")
            with patch("update_manager.running_as_packaged_app", return_value=True), \
                 patch("update_manager.sys.executable", str(old / "DesktopCalendar.exe")), \
                 patch("update_manager.stage_verified_updater", return_value=staged), \
                 patch("update_manager.subprocess.Popen") as popen:
                launch_updater(root / "verified.zip")
            args = popen.call_args.args[0]
            self.assertEqual(args[0], str(staged))
            self.assertEqual(args[args.index("--install-dir")+1], str(old.resolve()))
            self.assertEqual((old / "DesktopCalendarUpdater.exe").read_bytes(), b"MZold-schema7")

    def test_bad_payload_never_launches_old_fallback(self):
        with patch("update_manager.running_as_packaged_app", return_value=True), \
             patch("update_manager.subprocess.Popen") as popen, self.assertRaises(UpdateError):
            launch_updater(Path("missing-verified.zip"))
        popen.assert_not_called()

    def test_archive_staging_uses_exact_release_updater_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "release.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("DesktopCalendar/DesktopCalendar.exe", b"MZapp")
                package.writestr("DesktopCalendar/DesktopCalendarUpdater.exe", b"MZnew-schema8")
                package.writestr("DesktopCalendar/_internal/runtime", b"runtime")
            staged = stage_verified_updater(archive)
            self.addCleanup(shutil.rmtree, staged.parent)
            self.assertEqual(staged.read_bytes(), b"MZnew-schema8")


class UpdaterSequenceTests(unittest.TestCase):
    def run_updater(self, failure=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / "roaming" / "DesktopCalendar" / "calendar_data.json"
        data.parent.mkdir(parents=True)
        data.write_bytes(b'{"version":8,"events":[],"routines":[]}')
        install = root / "new install"
        install.mkdir()
        calls = []
        def step(name):
            calls.append(name)
            if failure == name:
                raise RuntimeError("synthetic failure")
        @contextmanager
        def reservation():
            step("reserve")
            try:
                yield
            finally:
                calls.append("release")
        args = SimpleNamespace(pid=123, archive=root/"verified.zip", install_dir=install, executable="DesktopCalendar.exe")
        before = data.read_bytes()
        with patch("updater.parse_args", return_value=args), patch.dict(os.environ, APPDATA=str(root/"roaming")), \
             patch("updater.wait_for_process", side_effect=lambda pid: step("wait")), \
             patch("updater.ensure_no_calendar_instance", side_effect=lambda: step("instances")), \
             patch("updater.reserve_installation", side_effect=reservation), \
             patch("updater.safe_extract", side_effect=lambda *a: step("extract")), \
             patch("updater.payload_root", return_value=root), \
             patch("updater.automatic_backup", side_effect=lambda *a, **k: step("backup")), \
             patch("updater.copy_payload", side_effect=lambda *a: step("copy")), \
             patch("updater.subprocess.Popen", side_effect=lambda *a, **k: step("restart")), patch("updater.show_error"):
            code = updater.main()
        self.assertEqual(data.read_bytes(), before)
        return code, calls

    def test_parent_exit_backup_copy_restart_order_and_exact_install_path(self):
        code, calls = self.run_updater()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["wait", "instances", "reserve", "extract", "backup", "copy", "release", "restart"])

    def test_failure_before_complete_install_never_restarts_or_changes_data(self):
        for failure in ("wait", "instances", "reserve", "extract", "backup", "copy"):
            with self.subTest(failure=failure):
                code, calls = self.run_updater(failure)
                self.assertEqual(code, 1)
                self.assertNotIn("restart", calls)

    @unittest.skipUnless(os.name == "nt", "Win32 only")
    def test_other_directory_instance_blocks_update_without_termination(self):
        kernel = ctypes.windll.kernel32
        with patch.object(kernel, "OpenMutexW", return_value=0x100000001), \
             patch.object(kernel, "CloseHandle") as close, self.assertRaises(RuntimeError):
            updater.ensure_no_calendar_instance()
        self.assertEqual(close.call_args.args[0], 0x100000001)

    @unittest.skipUnless(os.name == "nt", "Win32 only")
    def test_wait_failed_and_access_denied_are_not_treated_as_exit(self):
        kernel = ctypes.windll.kernel32
        with patch.object(kernel, "OpenProcess", return_value=123), \
             patch.object(kernel, "WaitForSingleObject", return_value=0xFFFFFFFF), \
             patch.object(kernel, "CloseHandle"), self.assertRaises(OSError):
            updater.wait_for_process(123)
        with patch.object(kernel, "OpenProcess", return_value=0), \
             patch.object(kernel, "GetLastError", return_value=5), self.assertRaises(OSError):
            updater.wait_for_process(123)


class InstallationExclusionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Win32 only")
    def test_real_guard_blocks_existing_single_instance_protocol_and_releases(self):
        # Use unique names so the test never reserves the actual user's app.
        kernel = ctypes.windll.kernel32
        with patch.object(update_state, "APP_MUTEX", "Local\\CalendarQATestApp"+str(os.getpid())), \
             patch.object(update_state, "UPDATE_MUTEX", "Local\\CalendarQATestUpdate"+str(os.getpid())):
            self.assertFalse(update_state.update_in_progress())
            with update_state.reserve_installation():
                self.assertTrue(update_state.update_in_progress())
                duplicate = kernel.CreateMutexW(None, False, update_state.APP_MUTEX)
                try:
                    self.assertTrue(duplicate)
                    self.assertEqual(kernel.GetLastError(), 183)
                finally:
                    kernel.CloseHandle(duplicate)
            self.assertFalse(update_state.update_in_progress())

    @unittest.skipUnless(os.name == "nt", "Win32 only")
    def test_reservation_failure_or_exception_releases_all_handles(self):
        kernel = ctypes.windll.kernel32
        with patch.object(kernel, "CreateMutexW", side_effect=[0x100000001, 0x100000002]), \
             patch.object(kernel, "GetLastError", side_effect=[0, 183]), \
             patch.object(kernel, "CloseHandle") as close, self.assertRaises(RuntimeError):
            with update_state.reserve_installation():
                self.fail("must not enter")
        self.assertEqual([c.args[0] for c in close.call_args_list], [0x100000002, 0x100000001])

    def test_startup_during_install_never_constructs_store_or_app(self):
        import app
        with patch("app.enable_dpi_awareness"), patch("app.update_in_progress", return_value=True), \
             patch("app.tk.Tk"), patch("app.messagebox.showinfo") as message, \
             patch("app.SingleInstance") as instance, patch("app.CalendarApp") as window, \
             patch.object(sys, "excepthook"):
            app.main()
        instance.assert_not_called()
        window.assert_not_called()
        self.assertIn("更新尚未完成", message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
