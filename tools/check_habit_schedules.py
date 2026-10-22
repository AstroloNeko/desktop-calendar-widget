"""Opt-in real Windows/Tk Habit 2.0 smoke, isolated data, no network.

Run: py -3 tools/check_habit_schedules.py [--screenshots]
Native DPI is reported separately from simulated 96..192 DPI layout checks.
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import date
from pathlib import Path
import sys
import struct
import tempfile
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from dpi_utils import dpi_for_window
from habit_ui import CHOICES
from tools.check_interactions import cleanup, pump
from tools.check_window_drag import G, U, native_signatures, rect, save_png
from win_integration import window_handle


def capture_own_window(editor, path):
    """Print only this QA window, not unrelated desktop windows covering it."""
    native_signatures()
    U.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    U.PrintWindow.restype = wintypes.BOOL
    hwnd = window_handle(editor)
    area = rect(hwnd)
    width, height = area[2] - area[0], area[3] - area[1]
    screen = U.GetDC(None)
    memory = G.CreateCompatibleDC(screen)
    bitmap = G.CreateCompatibleBitmap(screen, width, height)
    old = G.SelectObject(memory, bitmap)
    try:
        assert U.PrintWindow(hwnd, memory, 2), "QA window capture failed"
        G.SelectObject(memory, old)
        info = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        pixels = ctypes.create_string_buffer(width * height * 4)
        assert G.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) == height
        save_png(path, (0, 0, width, height), pixels.raw)
    finally:
        G.DeleteObject(bitmap)
        G.DeleteDC(memory)
        U.ReleaseDC(None, screen)


def buttons_visible(editor):
    return (editor.actions.winfo_ismapped() and editor.save_button.winfo_ismapped()
            and editor.actions.winfo_rooty() >= editor.winfo_rooty()
            and editor.actions.winfo_rooty() + editor.actions.winfo_height() <= editor.winfo_rooty() + editor.winfo_height())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--native-only", action="store_true")
    args = parser.parse_args()
    errors = []
    cases = 0
    with tempfile.TemporaryDirectory(prefix="calendar-habit2-gui-") as temporary:
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(Store(Path(temporary) / "data.json"))
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .2)
                native_dpi = dpi_for_window(app)
                print("Native monitor DPI:", native_dpi, flush=True)
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for view in ("compact", "global"):
                        if view == "global":
                            app.enter_global_view()
                        else:
                            app.return_to_compact_view()
                        for dpi in ((native_dpi,) if args.native_only else (96, 120, 144, 168, 192)):
                            with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                                app._apply_dpi_change(dpi)
                                app.select_day(date(2026, 8, 3))
                                app.open_routine_editor()
                                pump(app)
                                editor = app.routine_editor
                                assert editor.winfo_ismapped() and buttons_visible(editor)
                                assert app.grab_current() is editor and str(editor.transient()) == str(app)
                                assert not editor.schedule_form.workdays.get()
                                assert editor.schedule_form.value()["frequency"] == "daily"
                                editor.kind_var.set("todo")
                                editor._update_schedule_controls()
                                pump(app)
                                assert not editor.schedule_form.winfo_ismapped()
                                editor.kind_var.set("habit")
                                editor._update_schedule_controls()
                                form = editor.schedule_form
                                for choice in CHOICES:
                                    form.choice.set(choice)
                                    form.refresh()
                                    pump(app)
                                    assert buttons_visible(editor), (theme, view, dpi, choice)
                                    if choice == "自定义…":
                                        for unit in ("天", "周", "月", "年"):
                                            form.unit.set(unit)
                                            form.refresh()
                                            pump(app)
                                            form.value()
                                form.choice.set("每周")
                                form.refresh()
                                form.interval.set("2")
                                form.start.set("2026-08-03")
                                form.ending.set("指定日期")
                                form.end.set("2027-08-03")
                                for var in form.weekdays:
                                    var.set(False)
                                editor.title_var.set("周期习惯 smoke")
                                with patch("app.messagebox.showinfo") as validation:
                                    editor.save_button.invoke()
                                assert validation.call_count == 1
                                assert editor.winfo_exists() and app.grab_current() is editor
                                for index in (0, 2, 4):
                                    form.weekdays[index].set(True)
                                editor.save_button.invoke()
                                pump(app, .15)
                                assert app.routine_editor is None and app.grab_current() is None
                                item = app.store.routines[-1]
                                assert (item.frequency, item.interval, item.weekdays) == ("weekly", 2, [0, 2, 4])
                                item.habit_done = ["2026-08-04", "2026-08-05"]
                                app.store.upsert_routine(item)
                                app.open_routine_editor(item)
                                pump(app)
                                editor = app.routine_editor
                                editor.schedule_form.choice.set("每年")
                                editor.schedule_form.year_month.set("2")
                                editor.schedule_form.month_day.set("29")
                                editor.schedule_form.refresh()
                                editor.content_canvas.yview_moveto(1)
                                pump(app)
                                assert buttons_visible(editor)
                                editor.content_canvas.event_generate("<MouseWheel>", delta=120)
                                pump(app)
                                assert editor.content_canvas.yview()[0] < 1
                                if args.screenshots and dpi == native_dpi and view == "global":
                                    output = Path(__file__).resolve().parents[1] / "build/habit2-smoke"
                                    output.mkdir(parents=True, exist_ok=True)
                                    editor.content_canvas.yview_moveto(0)
                                    pump(app)
                                    capture_own_window(editor, output / f"{theme}.png")
                                editor.save_button.invoke()
                                pump(app, .15)
                                loaded = Store(app.store.data_file).routines[-1]
                                assert loaded.frequency == "yearly" and loaded.month_day == 29
                                assert loaded.habit_done == ["2026-08-04", "2026-08-05"]
                                assert app.grab_current() is None and app.modal_depth == 0
                                cases += 1
                    # The same editor must remain properly owned when launched
                    # from either the manager or a Global/Compact day detail.
                    app.open_routine_manager()
                    pump(app)
                    manager = app.routine_manager
                    app.open_routine_editor(app.store.routines[-1])
                    pump(app)
                    editor = app.routine_editor
                    assert str(editor.transient()) == str(manager)
                    editor.close()
                    editor.close()  # Late/double close must not strand a grab.
                    pump(app, .15)
                    manager.close()
                    pump(app, .15)
                    app.open_day_detail(date(2026, 8, 5))
                    pump(app)
                    detail = app.day_detail_window
                    app.open_routine_editor(app.store.routines[-1])
                    pump(app)
                    assert str(app.routine_editor.transient()) == str(detail)
                    app.routine_editor.close()
                    pump(app, .15)
                    assert app.grab_current() is detail
                    detail.close()
                    pump(app, .15)
                    assert app.grab_current() is None
                    app.focus_force()
                    app.event_generate("<Control-k>")
                    pump(app)
                    assert app.search_window is not None and app.search_window.winfo_ismapped()
                    app.search_window.close()
                    pump(app, .15)
                    print("PASS", theme, flush=True)
                assert not errors, errors
                print(f"PASS {cases} editor cases; four themes; parent modal/close/Ctrl+K; callback errors {errors}", flush=True)
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
