"""Optional real-Tk smoke/performance checks. All data lives in a temp folder.

Run: py -3 tools/check_interactions.py [--performance]
Opens native windows; not part of the headless unittest suite.
"""

import argparse
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import CalendarApp
from calendar_core import Event, EventCategory, Store
from event_search import EventSearchIndex, SearchResult, capture_search_store


def pump(app, seconds=0.04):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.005)


def cleanup(app):
    # Multiple short-lived Tk roots in a smoke process share the Tcl timer
    # queue. Cancel those timers explicitly before destroying this test root.
    for job in app.tk.call("after", "info"):
        app.after_cancel(job)
    app.on_close()


def position_hit(app, canvas, hit, operation="move"):
    x1, y1, x2, y2 = hit.bounds
    region = tuple(float(value) for value in canvas.cget("scrollregion").split())
    if region[3] > canvas.winfo_height():
        canvas.yview_moveto(max(0, y1 - app.dpi.px(30)) / max(1, region[3]))
    if app.global_display_mode == "timeline":
        app._global_xview("moveto", max(0, x1 - app.dpi.px(50)) / max(1, region[2]))
    app.update_idletasks()
    x = x1 + app.dpi.px(2) if operation == "start" else x2 - app.dpi.px(2) if operation == "end" else (x1 + x2) / 2
    return int(x - canvas.canvasx(0)), int((y1 + y2) / 2 - canvas.canvasy(0))


def gesture(app, operation):
    canvas = app.global_flow_canvas if app.global_display_mode == "flow" else app.global_timeline_canvas
    hit = next(hit for hit in app._schedule_hits if operation in ("move", "cancel") or getattr(hit, "true_" + operation))
    x, y = position_hit(app, canvas, hit, operation)
    canvas.event_generate("<Motion>", x=x, y=y)
    canvas.event_generate("<ButtonPress-1>", x=x, y=y)
    app.update()
    assert app._schedule_drag is not None, (app.theme_name, app.global_display_mode, operation)
    width = app._global_flow_column_width if app.global_display_mode == "flow" else app._global_day_width
    offset = -width if operation == "start" else width
    canvas.event_generate("<B1-Motion>", x=x + offset, y=y)
    app.update()
    assert app._schedule_drag.active
    if operation == "cancel":
        canvas.focus_force()
        pump(app)
        canvas.event_generate("<Escape>")
        app.update()
        assert app.event_commands.undo_count == 0
    else:
        canvas.event_generate("<ButtonRelease-1>", x=x + offset, y=y)
        app.update()
        assert app.event_commands.undo_count == 1
        app.undo_event()
        app.update()
        assert app.store.events[0].due == "2026-08-07T10:30"
        assert app.store.events[0].duration_days == 5
        app.event_commands.clear()
    assert app._schedule_drag is None and app.grab_current() is None, (operation, app.focus_get(), app.grab_current())


def smoke(app):
    for theme in ("modern", "aero", "paper", "frutiger"):
        app.set_theme(theme)
        app.enter_global_view()
        app.select_day(date(2026, 8, 7))
        pump(app)
        for mode in ("flow", "timeline"):
            app.set_global_display_mode(mode)
            pump(app)
            for operation in ("move", "start", "end", "cancel"):
                # Tk interprets rapid synthetic presses as a double click.
                pump(app, 0.51)
                gesture(app, operation)
            app.open_search()
            palette = app.search_window
            app.open_search()
            assert app.search_window is palette
            pump(app, 0.12)
            palette.tree.focus_force()
            palette.tree.event_generate("<Down>")
            app.update()
            assert palette.tree.selection() == ("1",)
            palette.entry.focus_force()
            palette.query.set("task")
            deadline = time.monotonic() + 3
            while not palette._results and time.monotonic() < deadline:
                pump(app)
            assert palette._results[0].key == "task"
            assert app.grab_current() is palette
            palette.entry.event_generate("<Escape>")
            app.update()
            assert app.search_window is None and app.grab_current() is None
            app.open_search()
            palette = app.search_window
            palette.query.set("task")
            deadline = time.monotonic() + 3
            while not palette._results and time.monotonic() < deadline:
                pump(app)
            palette.entry.event_generate("<Return>")
            pump(app)
            assert app.search_window is None and app.editor_window is not None
            assert app.grab_current() is app.editor_window
            app.editor_window.close()
            pump(app)
        app.return_to_compact_view()
        app.update()
        app.open_search()
        pump(app)
        app.search_window.close()
        app.update()
        print(theme, "Compact/Flow/Timeline, move/resize, undo, search singleton/modal: passed", "DPI", app.dpi.dpi)
    app.enter_global_view()
    app._global_category_filter_ids = set()
    app._global_include_uncategorized = False
    app._activate_search_result(SearchResult("event", "task", "", ""))
    app.update()
    assert app.timeline_model.item_by_id("task") is not None
    assert not app._global_category_filter_ids and not app._global_include_uncategorized
    assert app.timeline_selection.selected_item_id == "task"
    assert app.grab_current() is app.editor_window
    app.editor_window.close()
    app.update()
    print("Filtered search result temporarily revealed; original editor modal: passed")


def performance(app):
    app.set_theme("modern")
    app.enter_global_view()
    for count in (100, 500, 1000):
        app.store.events = [Event(str(index), f"task {index}", f"2026-08-{index % 28 + 1:02}T10:30",
                                  duration_days=index % 5 + 1, category_id="art" if index % 2 else None,
                                  event_type="ddl" if index % 7 == 0 else "general") for index in range(count)]
        app.event_commands.clear()
        app._search_reveal_event_id = None
        app._global_category_filter_ids = None
        app._global_include_uncategorized = True
        start = time.perf_counter()
        index = EventSearchIndex.from_store(capture_search_store(app.store))
        build = (time.perf_counter() - start) * 1000
        queries = []
        for _ in range(20):
            start = time.perf_counter()
            index.query("task")
            queries.append((time.perf_counter() - start) * 1000)
        app.select_day(date(2026, 8, 7))
        app.set_global_display_mode("flow")
        pump(app)
        start = time.perf_counter()
        app.global_flow_canvas.yview_moveto(0)
        for step in range(20):
            app.global_flow_canvas.yview_scroll(1 if step % 2 == 0 else -1, "units")
            app.update_idletasks()
        scroll = (time.perf_counter() - start) * 1000 / 20
        start = time.perf_counter()
        app._toggle_global_category("art")
        app.update_idletasks()
        filtering = (time.perf_counter() - start) * 1000
        app._select_all_global_categories()
        app.set_global_display_mode("timeline")
        pump(app)
        canvas, hit = app.global_timeline_canvas, app._schedule_hits[0]
        x, y = position_hit(app, canvas, hit)
        pointer = SimpleNamespace(widget=canvas, x=x, y=y, x_root=x + canvas.winfo_rootx(), y_root=y + canvas.winfo_rooty())
        app._schedule_press(pointer)
        frames = []
        for step in range(30):
            offset = (step % 5 + 1) * app._global_day_width
            motion = SimpleNamespace(widget=canvas, x=x + offset, y=y, x_root=pointer.x_root + offset, y_root=pointer.y_root)
            start = time.perf_counter()
            app._schedule_motion(motion)
            if app._schedule_render_job:
                app.after_cancel(app._schedule_render_job)
            app._draw_schedule_preview()
            app.update_idletasks()
            frames.append((time.perf_counter() - start) * 1000)
        app._cancel_schedule_drag()
        print(count, "events; ms: index", round(build, 2), "query median", round(statistics.median(queries), 2),
              "Flow scroll", round(scroll, 2), "category redraw", round(filtering, 2),
              "drag median/max", round(statistics.median(frames), 2), round(max(frames), 2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--performance", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        store = Store(Path(temporary) / "data.json")
        store.categories = [EventCategory("art", "绘画", "#52B788")]
        store.events = [Event("task", "task smoke", "2026-08-07T10:30", duration_days=5, end_as_ddl=True)]
        store.save()
        # Avoid real tray registration and scheduled notifications in a test.
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(parts)
            try:
                smoke(app)
                if args.performance:
                    performance(app)
                assert not errors, errors
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
