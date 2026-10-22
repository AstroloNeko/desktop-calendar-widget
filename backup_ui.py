"""Lightweight native-dialog entry points, using the app's owned-modal runner."""
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from data_backup import BackupError, create_backup, read_backup, restore_store
from calendar_flow_layout import normalize_global_display_mode
from view_mode import WindowGeometry


class BackupActionsMixin:
    def _data_message(self, function, text):
        return self._owned_data_dialog(lambda: function("桌面月历 · 数据管理", text, parent=self))

    def export_backup(self):
        if self._active_modal_window() is not None or self.update_busy:
            return
        name = f"桌面月历备份-{datetime.now():%Y%m%d-%H%M%S}.dcalbackup"
        filename = self._owned_data_dialog(lambda: filedialog.asksaveasfilename(
            parent=self, title="备份数据", defaultextension=".dcalbackup", initialfile=name,
            filetypes=(("月历数据备份", "*.dcalbackup"),)))
        if not filename:
            return
        try:
            if self.store.load_error:
                raise BackupError("当前数据未能完整加载，请先保留原文件并排查，未导出空数据。")
            destination = Path(filename)
            overwrite = destination.exists()
            if overwrite and not self._data_message(messagebox.askyesno, "此备份已存在，是否覆盖？"):
                return
            if destination.resolve() == self.store.data_file.resolve():
                raise BackupError("不能用备份覆盖当前日历数据文件。")
            self._save_window_settings()
            create_backup(self.store.data_file.read_bytes(), destination, overwrite=overwrite)
        except (OSError, BackupError) as exc:
            self._data_message(messagebox.showerror, f"备份未完成：{exc}")
            return
        self._data_message(messagebox.showinfo, "备份已创建。")

    def restore_data_backup(self):
        if self._active_modal_window() is not None or self.update_busy:
            return
        filename = self._owned_data_dialog(lambda: filedialog.askopenfilename(
            parent=self, title="恢复数据", filetypes=(("月历数据备份", "*.dcalbackup"),)))
        if not filename:
            return
        refreshed = False
        def refresh():
            nonlocal refreshed
            refreshed = True
            self._refresh_restored_data()
        try:
            manifest, _data = read_backup(Path(filename))
            created = datetime.fromisoformat(manifest["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            if not self._data_message(messagebox.askyesno,
                    f"备份时间：{created}\n来源版本：{manifest['app_version']}\n\n"
                    "将替换全部事项、分类、习惯记录和设置。\n当前数据会先自动备份。是否继续？"):
                return
            restore_store(self.store, Path(filename), apply=refresh)
        except Exception as exc:
            # A refresh error rolls back the store too; repaint its original data.
            if refreshed:
                try:
                    self._refresh_restored_data()
                except Exception:
                    pass  # Preserve the actual transaction error in the dialog.
            self._data_message(messagebox.showerror, f"恢复未完成：{exc}")
            return
        self.event_commands.clear()
        self._data_message(messagebox.showinfo, "数据已恢复；恢复前的备份已保存在数据目录的 backups 文件夹。")

    def _refresh_restored_data(self):
        self._cancel_schedule_drag()
        if hasattr(self, "bulk_selection"):
            self.clear_bulk_selection()
        self.show_holidays = bool(self.store.settings.get("show_holidays", True))
        self.agenda_open = bool(self.store.settings.get("agenda_open", True))
        mode = self.store.settings.get("window_mode", "desktop")
        self.window_mode = mode if mode in ("desktop", "pinned") else "desktop"
        self._reset_global_category_filter()
        self._invalidate_search()
        self.global_display_mode = normalize_global_display_mode(self.store.settings.get("global_display_mode"))
        self.set_theme(self.store.settings.get("theme"))
        self._apply_theme_opacity()
        if self.view_mode == "global":
            self._update_global_display_mode_widgets()
        if self.view_mode == "compact":
            self._set_initial_geometry()
        else:
            geometry = WindowGeometry.from_mapping(self.store.settings.get("global_geometry"))
            if geometry is not None:
                self._apply_saved_geometry(geometry)
        self.render()
