"""Real Tk backup/restore smoke; native file/confirmation responses are injected.

No network, real user data or actual update. Run: py -3 tools/check_backup_gui.py
"""
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store, Event
from data_backup import read_backup
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from win_integration import is_window_topmost


def main():
    errors = []
    cases = 0
    with tempfile.TemporaryDirectory(prefix="calendar-backup-gui-") as temporary:
        root = Path(temporary)
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(Store(root / "data.json"))
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .2)
                print("Native DPI:", dpi_for_window(app), flush=True)
                for theme in ("modern", "aero", "paper", "frutiger"):
                    for view in ("compact", "global"):
                        if view == "compact":
                            app.return_to_compact_view()
                        else:
                            app.enter_global_view()
                        app.set_theme(theme)
                        for pinned in (False, True):
                            app.window_mode = "pinned" if pinned else "desktop"
                            app.desktop_session_active = True
                            app.apply_window_mode()
                            app.store.events = [Event("stable-id", "备份与恢复 测试", "2026-10-22T10:00", event_type="ddl", duration_days=5)]
                            path = root / f"{theme}-{view}-{pinned}.dcalbackup"
                            with patch("backup_ui.filedialog.asksaveasfilename", return_value=str(path)), \
                                 patch("backup_ui.messagebox.showinfo") as success:
                                app.export_backup()
                                assert success.call_count == 1
                            read_backup(path)
                            app.store.events.clear()
                            app.store.save()
                            with patch("backup_ui.filedialog.askopenfilename", return_value=str(path)), \
                                 patch("backup_ui.messagebox.askyesno", return_value=True), \
                                 patch("backup_ui.messagebox.showinfo") as success, \
                                 patch("backup_ui.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))):
                                app.restore_data_backup()
                                assert not errors, errors
                                assert success.call_count == 1
                            pump(app, .2)
                            assert [e.id for e in app.store.events] == ["stable-id"]
                            assert app.grab_current() is None and app.modal_depth == 0
                            assert is_window_topmost(app) == pinned
                            app.focus_force()
                            app.event_generate("<Control-k>")
                            pump(app)
                            assert app.search_window is not None and app.search_window.winfo_ismapped()
                            app.search_window.close()
                            pump(app, .15)
                            cases += 1
                    print("PASS", theme, flush=True)
                assert not errors, errors
                print(f"PASS {cases} backup/restore UI cases; callback errors: {errors}", flush=True)
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
