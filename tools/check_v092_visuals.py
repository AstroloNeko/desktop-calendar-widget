"""Isolated v0.9.2 layout/screenshots and real native confirmation acceptance."""
import argparse
import ctypes
from ctypes import wintypes as w
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Event, EventCategory, RoutineItem, Store
from data_backup import create_backup, restore_store
from dpi_utils import scaled_geometry
from tools.check_bulk_actions import click_item
from tools.check_habit_schedules import buttons_visible, capture_own_window
from tools.check_interactions import cleanup, pump


def answer_own_confirmation(app, yes, attempt=0):
    user = ctypes.windll.user32
    user.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    user.FindWindowW.restype = w.HWND
    hwnd = user.FindWindowW("#32770", "批量管理")
    pid = w.DWORD()
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    if hwnd:
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    import os
    if not hwnd or pid.value != os.getpid():
        assert attempt < 20, "Own confirmation was not presented"
        app.after(50, lambda: answer_own_confirmation(app, yes, attempt + 1))
        return
    user.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user.PostMessageW(hwnd, 0x111, 6 if yes else 7, 0)  # IDYES / IDNO, own PID only.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-only", action="store_true")
    options = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "build/v092-ui"
    output.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    with tempfile.TemporaryDirectory(prefix="calendar-v092-visual-") as temporary:
        store = Store(Path(temporary) / "data.json")
        store.categories = [EventCategory("work", "项目计划", "#6687F2"), EventCategory("life", "生活", "#52B788")]
        store.events = [Event(str(i), f"{('方案评审', '整理资料', '出行准备')[i % 3]} · {i + 1}",
            f"2026-08-{3 + i // 2:02}T18:00", category_id="work" if i % 2 else "life", color_mode="inherit",
            event_type="ddl" if i == 3 else "urgent" if i == 5 else "general", duration_days=2 if i == 6 else 1)
            for i in range(10)]
        store.routines = [RoutineItem("weekly", "每周阅读", frequency="weekly", weekdays=[0, 2, 4],
            start_on="2026-08-03", workdays_only=False, habit_done=["2026-08-03", "2026-08-05"])]
        store.save()
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            native = app.dpi.dpi
            try:
                for dpi in ((native,) if options.native_only else (96, 120, 144, 168, 192)):
                    with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                        app._apply_dpi_change(dpi)
                        for theme in ("modern", "aero", "paper", "frutiger"):
                            app.set_theme(theme)
                            app.enter_global_view()
                            app.select_day(date(2026, 8, 3))
                            app.geometry(scaled_geometry(1000, 620, 70, 40, dpi))
                            pump(app, .12)
                            app.open_routine_editor(store.routines[0])
                            pump(app)
                            editor = app.routine_editor
                            form = editor.schedule_form
                            assert "周一、三、五" in form.summary.get()
                            for child in form.week_row.winfo_children():
                                assert child.winfo_x() + child.winfo_width() <= form.week_row.winfo_width()
                            assert buttons_visible(editor)
                            if dpi == native:
                                capture_own_window(editor, output / f"{theme}-habit.png")
                            editor.close()
                            for mode in ("flow", "timeline"):
                                app.set_global_display_mode(mode)
                                pump(app, .1)
                                for identifier in app._bulk_visible_order()[:10]:
                                    click_item(app, identifier)
                                assert len(app.bulk_selection.ids) >= 5
                                for width in (720, 1000):
                                    app.geometry(scaled_geometry(width, 620, 70, 40, dpi))
                                    pump(app, .08)
                                    children = [app.bulk_count_label, *app.bulk_buttons.values()]
                                    for child in children:
                                        assert child.winfo_x() + child.winfo_width() <= app.bulk_toolbar.winfo_width()
                                if dpi == native:
                                    capture_own_window(app, output / f"{theme}-{mode}-selection.png")
                                app.show_bulk_dates()
                                pump(app)
                                dialog = app._active_modal_window()
                                dialog.days.set("-3")
                                assert dialog.preview.get() == "提前 3 天"
                                if dpi == native and mode == "flow":
                                    capture_own_window(dialog, output / f"{theme}-dates.png")
                                dialog.close()
                                app.clear_bulk_selection()
                            assert app.grab_current() is None
                            results.append(dict(theme=theme, dpi=dpi, status="passed"))
                            print(theme, dpi, "habit / Flow / Timeline / toolbar / date dialog: passed", flush=True)
                # Real OS confirmation dialogs, not a mock of askyesno.
                app.set_global_display_mode("timeline")
                for decisions in ((False,), (True, False), (True, True)):
                    app.clear_bulk_selection()
                    for identifier in app._bulk_visible_order()[:2]:
                        click_item(app, identifier)
                    before = [asdict(item) for item in store.events]
                    history = app.event_commands.undo_count
                    answers = iter(decisions)
                    original = app._bulk_message
                    def confirm(text, **kwargs):
                        if kwargs.get("confirm"):
                            app.after(100, lambda yes=next(answers): answer_own_confirmation(app, yes))
                        return original(text, **kwargs)
                    with patch.object(app, "_bulk_message", side_effect=confirm):
                        result = app.bulk_delete()
                    assert result == all(decisions)
                    assert app.grab_current() is None and app.modal_depth == 0
                    if result:
                        assert app.event_commands.undo_count == history + 1
                        app.undo_event()
                    assert [asdict(item) for item in store.events] == before
                    print("Native delete confirmations", decisions, "cancel/undo/data/grab: passed", flush=True)
                store.routines = [RoutineItem(str(i), "周期性能", frequency="weekly", weekdays=[0, 2, 4],
                    interval=1 + i % 3, start_on="2026-08-03", workdays_only=False) for i in range(100)]
                start = perf_counter()
                for offset in range(365):
                    store.routines_on(date(2026, 8, 3) + timedelta(days=offset))
                elapsed = perf_counter() - start
                store.save()
                before = [asdict(item) for item in store.routines]
                backup = Path(temporary) / "rules.dcalbackup"
                create_backup(store.data_file.read_bytes(), backup)
                store.routines.clear()
                restore_store(store, backup)
                assert [asdict(item) for item in store.routines] == before
                assert not errors, errors
                print("100 habit rules / 365 days:", round(elapsed, 3), "seconds; backup restore passed", flush=True)
            finally:
                cleanup(app)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
