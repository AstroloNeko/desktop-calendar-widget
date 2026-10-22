"""Opt-in Windows/Tk multi-select smoke. Isolated data; no network or release.

Run: py -3 tools/check_bulk_actions.py [--performance] [--screenshots]
Exercises actual Tk widgets using generated mouse/keyboard events (not a claim
of physical hardware input). Reports native DPI separately from simulations.
"""
import argparse
import ctypes
from datetime import date
from pathlib import Path
import sys
import tempfile
from time import perf_counter
import traceback
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from bulk_ui import BulkDateDialog
from calendar_core import Event, EventCategory, RoutineItem, Store
from dpi_utils import dpi_for_window
from global_timeline_ui import GLOBAL_TIMELINE_LAYOUT
from tools.check_interactions import cleanup, pump, position_hit
from tools.check_habit_schedules import capture_own_window


def click_item(app, identifier, state=4):
    before = set(app.bulk_selection.ids)
    canvas = app.global_flow_canvas if app.global_display_mode == "flow" else app.global_timeline_canvas
    hit = next(h for h in app._schedule_hits if h.event_id == identifier)
    x, y = position_hit(app, canvas, hit)
    with patch.object(app, "_bulk_press", wraps=app._bulk_press) as press:
        canvas.event_generate("<Motion>", x=x, y=y)
        canvas.event_generate("<ButtonPress-1>", x=x, y=y, state=state)
        canvas.event_generate("<ButtonRelease-1>", x=x, y=y, state=state)
        pump(app)
        calls = press.call_count
    assert not app._schedule_drag, "modifier selection started a drag"
    if state == 4:
        assert app.bulk_selection.ids == before ^ {identifier}, (identifier, before, app.bulk_selection.ids, x, y, calls)


def select_pair(app):
    app.clear_bulk_selection()
    click_item(app, "a")
    click_item(app, "b")
    assert app.bulk_selection.ids == {"a", "b"}, (app.theme_name, app.global_display_mode, app.bulk_selection.ids)
    assert app.bulk_toolbar.winfo_ismapped()
    assert app.grab_current() is None


def smoke(app, screenshots):
    native = dpi_for_window(app)
    print("Native Windows DPI:", native, flush=True)
    checks = 0
    for theme in ("modern", "aero", "paper", "frutiger"):
        app.set_theme(theme)
        app.enter_global_view()
        app.select_day(date(2026, 8, 7))
        pump(app, .15)
        for mode in ("flow", "timeline"):
            app.set_global_display_mode(mode)
            pump(app, .15)
            with patch.object(app.store, "save", wraps=app.store.save) as save:
                select_pair(app)
                # A settled toolbar must not rebuild the active renderer on Ctrl.
                with patch.object(app, "_draw_active_global_view", wraps=app._draw_active_global_view) as draw:
                    click_item(app, "b")
                    click_item(app, "b")
                    assert app.bulk_selection.ids == {"a", "b"}
                    # Crossing the >=2 threshold changes the toolbar geometry.
                    pump(app, .15)
                    draw.reset_mock()
                    click_item(app, "c")
                    click_item(app, "c")
                    assert draw.call_count == 0, "local selection rebuilt the renderer"
                save.assert_not_called()
            assert app.event_commands.undo_count == 0
            assert app.editor_window is None
            pump(app, .51)  # Deliberately test a single, not a double, plain click.
            click_item(app, "a", state=0)
            assert app.bulk_selection.ids == {"a"}
            assert app.timeline_selection.selected_item_id == "a"
            click_item(app, "b")
            assert app.bulk_selection.ids == {"a", "b"}
            if screenshots:
                destination = Path(tempfile.gettempdir()) / "calendar-bulk-qa"
                destination.mkdir(exist_ok=True)
                capture_own_window(app, destination / f"{theme}-{mode}.png")
            # Each segment of a multi-day item shares exactly the same selection ID.
            assert sum(hit.event_id == "a" for hit in app._schedule_hits) >= 1
            assert len(app.bulk_selection.ids) == 2
            button = app.bulk_buttons["完成"]
            button.event_generate("<ButtonPress-1>", x=5, y=5)
            button.event_generate("<ButtonRelease-1>", x=5, y=5)
            pump(app)
            assert app.store.event_by_id("a").done and app.store.event_by_id("b").done
            assert app.event_commands.undo_count == 1 and not app.bulk_selection.ids
            assert not app.bulk_toolbar.winfo_ismapped()
            app.undo_event()
            pump(app)
            assert not app.store.event_by_id("a").done
            app.redo_event()
            pump(app)
            app.undo_event()
            app.event_commands.clear()
            select_pair(app)
            original = [(e.id, e.category_id, e.color_mode, e.color) for e in app.store.events]
            # Windows implements Menu as a native popup; winfo_ismapped is not
            # a reliable indicator of its presentation. Exercise its actual
            # command inside the native popup loop: tk_popup can block until
            # dismissal, so invoking only after it returns is a deadlock.
            chosen = []
            def choose_category():
                menu = next(child for child in reversed(app.winfo_children()) if isinstance(child, tk.Menu))
                assert menu.entrycget(1, "label") == "绘画"
                result = menu.invoke(1)
                menu.unpost()
                if sys.platform == "win32":
                    ctypes.windll.user32.EndMenu()  # End this thread's native popup loop.
                chosen.append((menu, result))
            with patch.object(app, "_execute_bulk", wraps=app._execute_bulk) as execute:
                app.after(100, choose_category)
                app.show_bulk_categories()
            assert chosen, "native menu command did not run"
            menu, result = chosen[0]
            menu.destroy()
            pump(app)
            assert app.store.event_by_id("b").category_id == "art", (result, execute.call_args_list,
                app.bulk_selection.ids, app.global_status_label.cget("text"))
            assert app.store.event_by_id("b").color_mode == "override"
            assert app.store.event_by_id("a").color_mode == "inherit"
            app.undo_event()
            pump(app)
            assert [(e.id, e.category_id, e.color_mode, e.color) for e in app.store.events] == original
            app.event_commands.clear()
            select_pair(app)
            app.show_bulk_dates()
            pump(app)
            dialog = app._active_modal_window()
            assert isinstance(dialog, BulkDateDialog) and dialog.winfo_ismapped()
            assert str(dialog.transient()) == str(app) and app.grab_current() is dialog
            assert dialog.winfo_rooty()+dialog.winfo_height() <= app.dpi.work_area().bottom
            app._handle_escape()
            assert app.bulk_selection.ids == {"a", "b"}, "root Esc consumed modal selection"
            dialog.days.set("invalid")
            with patch("bulk_ui.messagebox.showerror") as error:
                dialog.apply()
                assert error.call_args.kwargs["parent"] is dialog
            assert app.grab_current() is dialog
            dialog.days.set("2")
            dialog.apply()
            pump(app)
            assert not dialog.winfo_exists() and app.grab_current() is None
            assert app.store.event_by_id("a").due_date == date(2026, 8, 9)
            app.undo_event()
            app.event_commands.clear()
            pump(app)
            select_pair(app)
            with patch("bulk_ui.messagebox.askyesno", side_effect=[True, False]) as confirm:
                assert not app.bulk_delete()
                assert confirm.call_count == 2
            assert app.store.event_by_id("a") is not None
            with patch("bulk_ui.messagebox.askyesno", return_value=True) as confirm:
                assert app.bulk_delete()
                assert confirm.call_count == 2
            pump(app)
            assert app.store.event_by_id("a") is None and not app.bulk_selection.ids
            app.undo_event()
            app.event_commands.clear()
            pump(app)
            select_pair(app)
            app._handle_escape()
            assert not app.bulk_selection.ids and app.grab_current() is None
            if mode == "timeline":
                order = app._bulk_visible_order()
                app.select_timeline_item(order[0])
                label = app.global_label_canvas
                row_height = app.dpi.px(GLOBAL_TIMELINE_LAYOUT.row_height)
                x, y = app.dpi.px(30), int((len(order)-.5)*row_height-label.canvasy(0))
                label.event_generate("<Motion>", x=x, y=y)
                label.event_generate("<ButtonPress-1>", x=x, y=y, state=1)
                label.event_generate("<ButtonRelease-1>", x=x, y=y, state=1)
                pump(app)
                assert app.bulk_selection.ids == set(order)
            select_pair(app)
            app._global_category_filter_ids = {"art"}
            app._render_global_timeline()
            pump(app)
            assert not app.bulk_selection.ids
            app._reset_global_category_filter()
            pump(app)
            select_pair(app)
            app.select_day(date(2026, 9, 1))
            pump(app)
            assert not app.bulk_selection.ids
            app.select_day(date(2026, 8, 7))
            pump(app)
            select_pair(app)
            app.set_global_display_mode("timeline" if mode == "flow" else "flow")
            pump(app)
            assert not app.bulk_selection.ids
            checks += 1
            print(theme, mode, "selection/commands/modal/Esc/filter/month/view: passed", flush=True)
        app.return_to_compact_view()
        pump(app)
        assert not app.bulk_selection.ids and app.view_mode == "compact"
        app.open_day_detail()
        pump(app)
        app.day_detail_window.close()
        pump(app)
        assert app.grab_current() is None
    # Layout simulation is separate from the actual 192-DPI monitor above.
    app.enter_global_view()
    app.select_day(date(2026, 8, 7))
    for dpi in (96, 120, 144, 168, 192):
        with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
            app._apply_dpi_change(dpi)
            pump(app, .12)
            select_pair(app)
            bar = app.bulk_toolbar
            assert bar.winfo_rootx()+bar.winfo_width() <= app.winfo_rootx()+app.winfo_width()
            for button in app.bulk_buttons.values():
                assert button.winfo_rootx()+button.winfo_width() <= bar.winfo_rootx()+bar.winfo_width()
            app.clear_bulk_selection()
    print(checks, "theme/view cases passed; 96/120/144/168/192 DPI layout simulations passed", flush=True)


def performance(app):
    app.set_theme("modern")
    app.enter_global_view()
    app.set_global_display_mode("timeline")
    app.store.events = [Event(str(i), f"事项{i}", f"2026-08-{i%28+1:02}T10:00", category_id="art" if i%2 else None)
                        for i in range(500)]
    app.store.save()
    app.select_day(date(2026, 8, 7))
    app._render_global_timeline()
    pump(app, .2)
    for count in (10, 50, 100):
        app.clear_bulk_selection()
        order = app._bulk_visible_order()
        start = perf_counter()
        # Toolbar appearance legitimately changes workspace geometry (and may
        # trigger more than one Configure as scrollbars settle). Measure local
        # selection separately from that one-time layout transition.
        for identifier in order[:2]:
            app._bulk_press(SimpleNamespace(state=4, widget=app.global_timeline_canvas), identifier)
        pump(app, .2)
        with patch.object(app, "_draw_active_global_view", wraps=app._draw_active_global_view) as draw:
            for identifier in order[2:count]:
                app._bulk_press(SimpleNamespace(state=4, widget=app.global_timeline_canvas), identifier)
            pump(app, .1)
            assert draw.call_count == 0, draw.call_count
        selected = perf_counter()-start
        assert len(app.bulk_selection.ids) == count
        start = perf_counter()
        app.bulk_complete(True)
        app.undo_event()
        app.redo_event()
        pump(app)
        print(f"GUI 500 events / {count}: select {selected:.3f}s, complete+undo+redo {perf_counter()-start:.3f}s", flush=True)
        app.undo_event()
        app.event_commands.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--performance", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="calendar-bulk-gui-") as temporary:
        store = Store(Path(temporary) / "data.json")
        store.categories = [EventCategory("art", "绘画", "#52B788")]
        store.events = [Event("a", "跨周末日 DDL", "2026-08-07T10:30", duration_days=5, end_as_ddl=True,
                              category_id="art", color_mode="inherit", skip_non_working_days=True),
                        Event("b", "紧急任务", "2026-08-04T10:30", event_type="urgent"),
                        Event("c", "DDL", "2026-08-17T10:30", event_type="ddl")]
        store.events = [store.prepare_event(e) for e in store.events]
        store.routines = [RoutineItem("habit", "不纳入多选", frequency="weekly", weekdays=[0, 2, 4],
                                      start_on="2026-08-03", workdays_only=False, habit_done=["2026-08-03"])]
        store.save()
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .2)
                smoke(app, args.screenshots)
                if args.performance:
                    performance(app)
                assert not errors, errors
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
