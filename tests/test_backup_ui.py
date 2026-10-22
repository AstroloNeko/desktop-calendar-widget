from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backup_ui import BackupActionsMixin
from calendar_core import Store, Event
from data_backup import create_backup, read_backup


class Owner(BackupActionsMixin):
    def __init__(self, root):
        self.store = Store(root / "data.json")
        self.store.save()
        self.update_busy = False
        self.event_commands = SimpleNamespace(clear=Mock())
        self._refresh_restored_data = Mock()

    def _active_modal_window(self):
        return None

    def _owned_data_dialog(self, callback):
        return callback()

    def _save_window_settings(self):
        self.store.save()


class BackupUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = Owner(self.root)
        self.path = self.root / "export.dcalbackup"

    def test_manual_export(self):
        with patch("backup_ui.filedialog.asksaveasfilename", return_value=str(self.path)), \
             patch("backup_ui.messagebox.showinfo") as info:
            self.owner.export_backup()
        read_backup(self.path)
        info.assert_called_once()

    def test_cancel_export_changes_nothing(self):
        previous = self.owner.store.data_file.read_bytes()
        with patch("backup_ui.filedialog.asksaveasfilename", return_value=""), \
             patch.object(self.owner, "_save_window_settings") as save:
            self.owner.export_backup()
        save.assert_not_called()
        self.assertEqual(previous, self.owner.store.data_file.read_bytes())

    def test_export_failure_message(self):
        with patch("backup_ui.filedialog.asksaveasfilename", return_value=str(self.root / "missing" / "x")), \
             patch("backup_ui.messagebox.showerror") as error:
            self.owner.export_backup()
        error.assert_called_once()

    def test_cancel_restore_file_or_confirmation(self):
        create_backup(self.owner.store.data_file.read_bytes(), self.path)
        previous = self.owner.store.data_file.read_bytes()
        for filename in ("", str(self.path)):
            with patch("backup_ui.filedialog.askopenfilename", return_value=filename), \
                 patch("backup_ui.messagebox.askyesno", return_value=False):
                self.owner.restore_data_backup()
        self.assertEqual(previous, self.owner.store.data_file.read_bytes())
        self.owner._refresh_restored_data.assert_not_called()
        self.assertFalse((self.root / "backups").exists())

    def test_restore_reloads_and_clears_undo(self):
        self.owner.store.events = [Event("old-id", "恢复的事项", "2026-10-22T10:00")]
        self.owner.store.save()
        create_backup(self.owner.store.data_file.read_bytes(), self.path)
        self.owner.store.events.clear()
        self.owner.store.save()
        with patch("backup_ui.filedialog.askopenfilename", return_value=str(self.path)), \
             patch("backup_ui.messagebox.askyesno", return_value=True), patch("backup_ui.messagebox.showinfo"):
            self.owner.restore_data_backup()
        self.assertEqual(self.owner.store.events[0].id, "old-id")
        self.owner._refresh_restored_data.assert_called_once()
        self.owner.event_commands.clear.assert_called_once()

    def test_invalid_restore_does_not_repaint_or_write(self):
        self.path.write_text("invalid")
        previous = self.owner.store.data_file.read_bytes()
        with patch("backup_ui.filedialog.askopenfilename", return_value=str(self.path)), \
             patch("backup_ui.messagebox.showerror") as error:
            self.owner.restore_data_backup()
        self.owner._refresh_restored_data.assert_not_called()
        self.assertEqual(previous, self.owner.store.data_file.read_bytes())
        error.assert_called_once()

    def test_restore_blocked_during_update_or_editor(self):
        with patch("backup_ui.filedialog.askopenfilename") as chooser:
            self.owner.update_busy = True
            self.owner.restore_data_backup()
            self.owner.update_busy = False
            self.owner._active_modal_window = lambda: object()
            self.owner.restore_data_backup()
        chooser.assert_not_called()

    def test_compact_reload_does_not_touch_destroyed_global_widgets(self):
        owner = SimpleNamespace(
            store=SimpleNamespace(settings={}), view_mode="compact",
            _cancel_schedule_drag=Mock(), _reset_global_category_filter=Mock(),
            _invalidate_search=Mock(), set_theme=Mock(), _apply_theme_opacity=Mock(),
            _update_global_display_mode_widgets=Mock(), _set_initial_geometry=Mock(), render=Mock(),
        )
        BackupActionsMixin._refresh_restored_data(owner)
        owner._update_global_display_mode_widgets.assert_not_called()
        owner._set_initial_geometry.assert_called_once()
        owner.render.assert_called_once()

    def test_update_checkpoint_precedes_program_copy(self):
        import updater
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
             patch("updater.parse_args", return_value=SimpleNamespace(pid=1, archive=Path(folder)/"archive.zip", install_dir=Path(folder), executable="a.exe")), \
             patch("updater.wait_for_process"), patch("updater.safe_extract"), \
             patch("updater.payload_root", return_value=Path(folder)), \
             patch("updater.automatic_backup", side_effect=lambda *a, **k: calls.append("backup")), \
             patch("updater.copy_payload", side_effect=lambda *a: calls.append("copy")), \
             patch("updater.subprocess.Popen"), patch("updater.show_error"):
            self.assertEqual(updater.main(), 0)
        self.assertEqual(calls, ["backup", "copy"])

    def test_update_checkpoint_failure_aborts_copy(self):
        import updater
        with tempfile.TemporaryDirectory() as folder, \
             patch("updater.parse_args", return_value=SimpleNamespace(pid=1, archive=Path(folder)/"archive.zip", install_dir=Path(folder), executable="a.exe")), \
             patch("updater.wait_for_process"), patch("updater.safe_extract"), \
             patch("updater.payload_root", return_value=Path(folder)), \
             patch("updater.automatic_backup", side_effect=OSError("backup failed")), \
             patch("updater.copy_payload") as copy, patch("updater.show_error"):
            self.assertEqual(updater.main(), 1)
        copy.assert_not_called()
