"""Seven-path native benchmark; identical fixture/measurements to Phase 3 baseline."""

import json
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
from tools.check_interactions import cleanup, position_hit, pump


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "data.json")
        store.categories = [EventCategory("art", "Art", "#52B788")]
        store.save()
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(parts)
            app.update()
            app.enter_global_view()
            pump(app)
            try:
                for count in (100, 500, 1000):
                    store.events = [Event(str(i), f"task {i}", f"2026-08-{i % 28 + 1:02}T10:30",
                                          duration_days=i % 5 + 1, category_id="art" if i % 2 else None,
                                          event_type="ddl" if i % 7 == 0 else "general") for i in range(count)]
                    app._global_category_filter_ids = None
                    app._global_include_uncategorized = True
                    app.event_commands.clear()
                    app.select_day(date(2026, 8, 7))
                    metrics = {"events": count}
                    for mode in ("flow", "timeline"):
                        app.set_global_display_mode(mode)
                        pump(app)
                        start = time.perf_counter()
                        app._render_global_timeline(refresh_sidebar=False)
                        app.update_idletasks()
                        metrics[mode + "_render_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    app.set_global_display_mode("flow")
                    pump(app)
                    start = time.perf_counter()
                    app.change_month(1)
                    app.update_idletasks()
                    metrics["month_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    app.change_month(-1)
                    pump(app)
                    start = time.perf_counter()
                    app._toggle_global_category("art")
                    app.update_idletasks()
                    metrics["filter_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    app._select_all_global_categories()
                    pump(app)
                    app._invalidate_search()
                    app.open_search()
                    palette = app.search_window
                    pump(app, .12)
                    start = time.perf_counter()
                    palette.query.set("task 9")
                    deadline = time.monotonic() + 3
                    while (not palette._results or getattr(palette.tree, "_draw_job", None)) and time.monotonic() < deadline:
                        pump(app, .005)
                    assert palette._results, "search timed out"
                    metrics["search_visible_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    palette.close()
                    pump(app)
                    app.set_global_display_mode("timeline")
                    pump(app)
                    canvas, hit = app.global_timeline_canvas, app._schedule_hits[0]
                    x, y = position_hit(app, canvas, hit)
                    pointer = SimpleNamespace(widget=canvas, x=x, y=y, x_root=x + canvas.winfo_rootx(), y_root=y + canvas.winfo_rooty())
                    app._schedule_press(pointer)
                    frames = []
                    for step in range(20):
                        offset = (step % 5 + 1) * app._global_day_width
                        motion = SimpleNamespace(widget=canvas, x=x + offset, y=y, x_root=pointer.x_root + offset, y_root=pointer.y_root)
                        start = time.perf_counter()
                        app._schedule_motion(motion)
                        if app._schedule_render_job:
                            app.after_cancel(app._schedule_render_job)
                        app._draw_schedule_preview()
                        app.update_idletasks()
                        frames.append((time.perf_counter() - start) * 1000)
                    metrics["drag_frame_ms"] = round(statistics.median(frames), 2)
                    start = time.perf_counter()
                    app._schedule_release(motion)
                    app.update_idletasks()
                    metrics["drop_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    print(json.dumps(metrics), flush=True)
                assert not errors, errors
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
