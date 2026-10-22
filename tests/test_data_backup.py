from dataclasses import asdict
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from calendar_core import Store, Event, EventCategory, RoutineItem
from data_backup import (AUTO_KEEP, BackupError, automatic_backup, create_backup,
                         read_backup, restore_store, validate_data)


class DataBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "数据 空间🙂" / "calendar_data.json")
        self.store.save()
        self.backup = self.root / "备份 🐱.dcalbackup"

    def populate(self, count=3):
        self.store.categories = [EventCategory.from_dict({"id": "cat", "name": "分类", "color": "#123456"})]
        self.store.events = [Event.from_dict({"id": str(i), "title": f"事项 {i}", "due": "2026-10-23T15:00",
            "event_type": ("general", "urgent", "ddl")[i % 3], "category_id": "cat", "color_mode": "inherit",
            "duration_days": 5, "skip_non_working_days": True, "end_as_ddl": True, "done": i % 2 == 0}) for i in range(count)]
        self.store.events[0].color_mode = "override"
        self.store.events[0].color = "#abcdef"
        self.store.routines = [RoutineItem("habit", "习惯", habit_done=["2026-10-22"], reminder_enabled=True, reminder_time="09:30")]
        self.store.date_states = {"2026-10-26": "leave", "2026-10-27": "holiday"}
        self.store.settings.update(theme="frutiger", compact_geometry={"width": 372, "height": 548, "x": -100, "y": 40})
        self.store.notified = {"habit:2026-10-22"}
        self.store.save()

    def make(self):
        return create_backup(self.store.data_file.read_bytes(), self.backup)

    def rewrite(self, *, mutate_manifest=None, mutate_data=None, entries=None):
        with zipfile.ZipFile(self.backup) as p:
            manifest = json.loads(p.read("manifest.json"))
            data = p.read("calendar_data.json")
        if mutate_manifest:
            mutate_manifest(manifest)
        if mutate_data:
            data = mutate_data(data)
        with zipfile.ZipFile(self.backup, "w") as p:
            p.writestr("manifest.json", json.dumps(manifest))
            p.writestr("calendar_data.json", data)
            for name in entries or []:
                p.writestr(name, b"unsafe")

    def test_empty_round_trip(self):
        self.make()
        protection = restore_store(self.store, self.backup)
        self.assertEqual(self.store.events, [])
        self.assertTrue(protection.is_file())

    def test_all_data_and_ids_round_trip_unicode_path(self):
        self.populate(1000)
        expected = self.store.data_file.read_bytes()
        self.make()
        self.store.events.clear()
        self.store.categories.clear()
        self.store.routines.clear()
        self.store.settings["theme"] = "modern"
        self.store.date_states.clear()
        self.store.notified.clear()
        self.store.save()
        restore_store(self.store, self.backup)
        self.assertEqual(self.store.data_file.read_bytes(), expected)
        self.assertEqual(len(self.store.events), 1000)
        self.assertEqual(self.store.events[0].id, "0")
        self.assertEqual(self.store.events[0].color, "#abcdef")
        self.assertEqual(self.store.routines[0].habit_done, ["2026-10-22"])
        self.assertEqual(self.store.settings["theme"], "frutiger")
        self.assertEqual(Store(self.store.data_file).events, self.store.events)

    def test_manifest_integrity_and_no_extra_user_files(self):
        self.populate()
        self.make()
        manifest, data = read_backup(self.backup)
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["schema_version"], 8)
        self.assertEqual(manifest["files"]["calendar_data.json"]["sha256"], hashlib.sha256(data).hexdigest())
        with zipfile.ZipFile(self.backup) as p:
            self.assertEqual(set(p.namelist()), {"manifest.json", "calendar_data.json"})

    def test_existing_manual_backup_never_overwritten_without_permission(self):
        self.make()
        first = self.backup.read_bytes()
        with self.assertRaises(FileExistsError):
            self.make()
        self.assertEqual(self.backup.read_bytes(), first)
        create_backup(self.store.data_file.read_bytes(), self.backup, overwrite=True)
        read_backup(self.backup)

    def test_bad_zip(self):
        self.backup.write_bytes(b"not a zip")
        with self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_missing_member(self):
        with zipfile.ZipFile(self.backup, "w") as p:
            p.writestr("manifest.json", "{}")
        with self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_manifest_tampering(self):
        self.make()
        self.rewrite(mutate_manifest=lambda m: m.update(format_version=99))
        with self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_sha_mismatch(self):
        self.make()
        self.rewrite(mutate_data=lambda d: d.replace(b'"version": 8', b'"version": 6'))
        with self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_future_schema_and_bad_json(self):
        for data in (b'{"version": 999}', b'bad json', b'[]', b'{"events": [null]}'):
            with self.subTest(data=data), self.assertRaises(BackupError):
                create_backup(data, self.backup)
        self.assertFalse(self.backup.exists())

    def test_malicious_paths_extra_members_and_duplicates(self):
        for name in ("../outside", "/absolute", "C:/escape", "calendar_data.json", "debug.log"):
            with self.subTest(name=name):
                self.make()
                self.rewrite(entries=[name])
                with self.assertRaises(BackupError):
                    read_backup(self.backup)
                self.backup.unlink()

    def test_symlink_member(self):
        with zipfile.ZipFile(self.backup, "w") as p:
            p.writestr("manifest.json", "{}")
            info = zipfile.ZipInfo("calendar_data.json")
            info.external_attr = 0o120777 << 16
            p.writestr(info, "target")
        with self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_oversized_archive_and_member(self):
        self.make()
        with patch("data_backup.MAX_ARCHIVE_BYTES", 1), self.assertRaises(BackupError):
            read_backup(self.backup)
        with patch("data_backup.MAX_DATA_BYTES", 10), self.assertRaises(BackupError):
            read_backup(self.backup)

    def test_write_failure_and_disk_full_leave_original_and_no_output(self):
        original = self.store.data_file.read_bytes()
        for error in (PermissionError("read only"), OSError(errno.ENOSPC, "disk full")):
            with self.subTest(error=error), patch("data_backup.zipfile.ZipFile.writestr", side_effect=error):
                with self.assertRaises(OSError):
                    self.make()
            self.assertFalse(self.backup.exists())
            self.assertEqual(self.store.data_file.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".calendar-backup-*")), [])

    def test_failed_final_publish_preserves_old_manual_backup(self):
        self.make()
        previous = self.backup.read_bytes()
        with patch("data_backup.os.replace", side_effect=PermissionError("locked")), self.assertRaises(OSError):
            create_backup(self.store.data_file.read_bytes(), self.backup, overwrite=True)
        self.assertEqual(self.backup.read_bytes(), previous)

    def test_restore_failure_before_replace_preserves_data(self):
        self.populate()
        self.make()
        old = self.store.data_file.read_bytes()
        with patch("data_backup._stage_bytes", side_effect=OSError("no space")), self.assertRaises(OSError):
            restore_store(self.store, self.backup)
        self.assertEqual(self.store.data_file.read_bytes(), old)
        self.assertEqual(len(self.store.events), 3)

    def test_restore_reload_failure_rolls_back_disk_memory_and_retains_checkpoint(self):
        self.populate()
        self.make()
        self.store.events.clear()
        self.store.save()
        old = self.store.data_file.read_bytes()
        def failed_ui():
            raise RuntimeError("refresh failed")
        with self.assertRaises(RuntimeError):
            restore_store(self.store, self.backup, apply=failed_ui)
        self.assertEqual(self.store.data_file.read_bytes(), old)
        self.assertEqual(self.store.events, [])
        protections = list((self.store.data_file.parent / "backups").glob("*.dcalbackup"))
        self.assertTrue(protections)
        self.assertEqual(read_backup(protections[-1])[1], old)

    def test_protection_backup_failure_aborts_restore(self):
        self.make()
        original = self.store.data_file.read_bytes()
        with patch("data_backup.automatic_backup", side_effect=PermissionError("locked")), self.assertRaises(OSError):
            restore_store(self.store, self.backup)
        self.assertEqual(self.store.data_file.read_bytes(), original)

    def test_invalid_input_does_not_write_current_state(self):
        original = self.store.data_file.read_bytes()
        self.backup.write_bytes(b"bad zip")
        with patch.object(self.store, "save") as save, self.assertRaises(BackupError):
            restore_store(self.store, self.backup)
        save.assert_not_called()
        self.assertEqual(self.store.data_file.read_bytes(), original)

    def test_daily_interval_retention_and_manual_files(self):
        self.make()
        first = automatic_backup(self.store.data_file)
        self.assertTrue(first)
        self.assertIsNone(automatic_backup(self.store.data_file))
        directory = self.store.data_file.parent / "backups"
        unrelated = directory / "other.txt"
        unrelated.write_text("keep")
        invalid = directory / "auto-unknown.dcalbackup"
        invalid.write_text("keep")
        for _ in range(AUTO_KEEP + 3):
            automatic_backup(self.store.data_file, force=True)
        valid = [p for p in directory.glob("auto-*.dcalbackup") if p != invalid]
        self.assertEqual(len(valid), AUTO_KEEP)
        self.assertTrue(self.backup.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertEqual(invalid.read_text(), "keep")

    def test_failed_auto_backup_does_not_prune(self):
        first = automatic_backup(self.store.data_file)
        with patch("data_backup.create_backup", side_effect=OSError("no space")), self.assertRaises(OSError):
            automatic_backup(self.store.data_file, force=True)
        self.assertTrue(first.is_file())

    def test_legacy_migration_checkpoint_preserves_original(self):
        raw = b'{"events": [{"id":"old", "title":"legacy", "due":"2026-01-01T12:00", "priority":"low"}]}'
        self.store.data_file.write_bytes(raw)
        legacy = Store(self.store.data_file)
        legacy.save()
        paths = list((self.store.data_file.parent / "backups").glob("*.dcalbackup"))
        self.assertEqual(read_backup(paths[0])[1], raw)
        self.assertEqual(Store(self.store.data_file).events[0].event_type, "general")

    def test_migration_backup_failure_does_not_overwrite_legacy_file(self):
        raw = b'{"events": []}'
        self.store.data_file.write_bytes(raw)
        legacy = Store(self.store.data_file)
        with patch("data_backup.automatic_backup", side_effect=OSError("full")), self.assertRaises(OSError):
            legacy.save()
        self.assertEqual(self.store.data_file.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
