"""Lightweight native-dialog entry points, using the app's owned-modal runner."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog, messagebox

from data_backup import BackupError, create_backup, read_backup, restore_store
from desktop_preferences import decline_sqlite_upgrade, sqlite_upgrade_declined
from calendar_flow_layout import normalize_global_display_mode
from view_mode import WindowGeometry


class BackupActionsMixin:
    def storage_banner_visible(self):
        return (bool(getattr(self, 'store', None)) and not self._sqlite_backup_boundary()
                and not getattr(self, '_storage_prompt_dismissed', False)
                and not sqlite_upgrade_declined(self.store.data_file.parent))

    def build_storage_banner(self):
        if not self.storage_banner_visible():
            return
        import tkinter as tk
        from desktop_calendar.reminders.reminder_ui import reminder_button
        frame = tk.Frame(self.shell, bg=self.theme.panel_background)
        frame.pack(fill='x')
        self.storage_banner = frame
        self.storage_upgrade_button = reminder_button(frame, self, '升级到 SQLite', self.request_storage_upgrade, primary=True)
        self.storage_upgrade_button.pack(side='left', padx=self.dpi.px(8))
        self.storage_dismiss_button = reminder_button(frame, self, '×', self._dismiss_storage_banner)
        self.storage_dismiss_button.pack(side='right')
        self.storage_decline_button = reminder_button(
            frame, self, '继续使用 JSON，不再提醒', self._decline_storage_banner)
        self.storage_decline_button.pack(side='right')

    def _dismiss_storage_banner(self):
        self._storage_prompt_dismissed = True
        self.storage_banner.destroy()
        self.render()

    def _decline_storage_banner(self):
        try:
            decline_sqlite_upgrade(self.store.data_file.parent)
        except OSError as exc:
            self._data_message(messagebox.showerror, f'无法保存提醒偏好：{exc}')
            return
        self._dismiss_storage_banner()

    def request_storage_upgrade(self):
        if self._sqlite_backup_boundary() or self.update_busy or self._active_modal_window() is not None:
            return
        if getattr(self, 'storage_session', None) is None:
            self._data_message(messagebox.showinfo, '请通过正式启动入口打开月历后升级存储。')
            return
        self._pending_storage_upgrade = True
        if not self.on_close():
            self._pending_storage_upgrade = False

    def _sqlite_backup_boundary(self):
        from desktop_backup import sqlite_store
        return sqlite_store(self.store)

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
            if self._sqlite_backup_boundary():
                from desktop_backup import export_sqlite
                export_sqlite(self.store, destination, overwrite=overwrite)
            else:
                create_backup(self.store.data_file.read_bytes(), destination, overwrite=overwrite)
        except (OSError, ValueError, RuntimeError) as exc:
            self._data_message(messagebox.showerror, f"备份未完成：{exc}")
            return
        self._data_message(messagebox.showinfo, "备份已创建。日历备份不包含外部 AI 配置、凭据或 ChatJournal。")

    def restore_data_backup(self):
        if self._active_modal_window() is not None or self.update_busy:
            return
        filename = self._owned_data_dialog(lambda: filedialog.askopenfilename(
            parent=self, title="恢复数据", filetypes=(("月历数据备份", "*.dcalbackup"),)))
        if not filename:
            return
        if self._sqlite_backup_boundary():
            try:
                from desktop_calendar.storage.calendar_sqlite_backup import read_container
                from desktop_calendar.storage.calendar_storage_lifecycle import digest
                fingerprint = digest(filename)
                manifest, _, _ = read_container(Path(filename))
                if not self._data_message(messagebox.askyesno,
                        f"备份时间：{manifest['created_at']}\n\n恢复将覆盖此后变更，当前数据库会先保护。"
                        "\n旧备份可能遗忘已投递提醒，继续表示接受恢复后可能再次提醒。"
                        "\n窗口将重新打开；撤销历史和旧待执行 AI 操作将失效，原聊天历史保留归档。是否继续？"):
                    return
                if getattr(self, 'storage_session', None) is None:
                    raise RuntimeError('恢复需要通过正式存储启动入口打开应用。')
                self._pending_storage_restore = (Path(filename), fingerprint)
                if not self.on_close():
                    self._pending_storage_restore = None
            except (OSError, ValueError, RuntimeError) as exc:
                self._data_message(messagebox.showerror, f"恢复未完成：{exc}")
            return
        try:
            manifest, _data = read_backup(Path(filename))
            created = datetime.fromisoformat(manifest["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            if not self._data_message(messagebox.askyesno,
                    f"备份时间：{created}\n来源版本：{manifest['app_version']}\n\n"
                    "将替换全部事项、分类、习惯记录和设置。\n当前数据会先自动备份。是否继续？"):
                return
            self._apply_json_replacement(Path(filename))
        except Exception as exc:
            self._data_message(messagebox.showerror, f"恢复未完成：{exc}")
            return
        self._data_message(messagebox.showinfo, "数据已恢复；恢复前的备份已保存在数据目录的 backups 文件夹。")

    def import_legacy_calendar(self):
        """Import raw JSON explicitly, separate from backup restore and upgrade."""
        if self._active_modal_window() is not None or self.update_busy:
            return
        from desktop_calendar.storage.calendar_storage_lifecycle import empty_calendar
        from desktop_calendar.storage.calendar_migration_validation import validate_source
        from desktop_calendar.storage.calendar_disaster_recovery import read_reviewed_json
        sqlite = self._sqlite_backup_boundary()
        if sqlite and not empty_calendar(self.store):
            self._data_message(messagebox.showinfo,
                '当前 SQLite 日历已有数据，不能导入 JSON 覆盖或合并。若要替换为历史备份，请使用「恢复数据」。')
            return
        filename = self._owned_data_dialog(lambda: filedialog.askopenfilename(
            parent=self, title="导入旧 JSON", filetypes=(("旧月历数据", "*.json"),)))
        if not filename:
            return
        try:
            source = Path(filename)
            if source.resolve() == self.store.data_file.resolve():
                raise BackupError('这已经是当前日历数据。要切换存储，请使用「升级到 SQLite」。')
            validation = validate_source(source)
            if not validation.valid:
                raise BackupError('该文件未通过适用于 Schema 8/9 的严格校验，当前日历与源文件未修改。')
            fingerprint = validation.source['sha256']
            plan = ('将旧 JSON 转换到当前空 SQLite 日历，继续使用 SQLite。'
                    if sqlite else '将用旧 JSON 完整替换当前日历，继续使用 JSON；不会合并数据。')
            if not self._data_message(messagebox.askyesno, f'{plan}\n当前数据会先保护，原文件不会修改。'
                    '\n历史提醒可能再次投递。是否继续？'):
                return
            if sqlite:
                if getattr(self, 'storage_session', None) is None:
                    raise RuntimeError('请通过正式存储启动入口打开月历后导入。')
                self._pending_storage_restore = (source, fingerprint, 'import_json')
                if not self.on_close():
                    self._pending_storage_restore = None
            else:
                data = read_reviewed_json(source, fingerprint)
                with TemporaryDirectory() as folder:
                    capsule = create_backup(data, Path(folder) / 'legacy-import.dcalbackup', reason='legacy-import')
                    self._apply_json_replacement(capsule)
                self._data_message(messagebox.showinfo, '旧 JSON 已导入；原文件和导入前的保护备份均已保留。')
        except (OSError, ValueError, RuntimeError) as exc:
            self._data_message(messagebox.showerror, f'导入未完成：{exc}')

    def _apply_json_replacement(self, backup):
        refreshed = False
        def refresh():
            nonlocal refreshed
            refreshed = True
            self._refresh_restored_data()
        try:
            restore_store(self.store, backup, apply=refresh)
        except Exception as exc:
            # A refresh error rolls back the store too; repaint its original data.
            if refreshed:
                try:
                    self._refresh_restored_data()
                except Exception:
                    pass  # Preserve the actual transaction error in the dialog.
            raise
        self.event_commands.clear()

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
