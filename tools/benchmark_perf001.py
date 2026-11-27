"""Isolated L4 GUI + headless-safe performance benchmark for PERF-001.

Uses an isolated temp directory with production SQLiteCalendarRepository,
synthetic calendar data, mocked notification backends, no real LLM/remote
workers, and restores all instrumentation hooks cleanly on exit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import cProfile
from datetime import date, datetime, timedelta
import io
import json
import os
from pathlib import Path
import pstats
import queue
import statistics
import sys
import tempfile
import time
import tkinter as tk
import tracemalloc
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module
from app import CalendarApp, DayCell, ThemeButton
from calendar_core import Event, EventCategory, RoutineItem
from calendar_event_service import event_service
from desktop_calendar.global_view.global_drag_ui import GlobalDragMixin
from desktop_calendar.global_view.global_header import GlobalHeader
from desktop_calendar.global_view.global_sidebar import GlobalSidebar
from desktop_calendar.reminders import notification_dispatch, reminder_engine
from desktop_calendar.reminders.reminder_engine import STATE_KEY
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from desktop_storage_startup import StartupOptions, open_desktop_storage, stop_desktop_storage
from tools.check_interactions import cleanup, position_hit, pump


DATASETS = {
    "small": {"events": 20, "routines": 4, "batches": 10},
    "medium": {"events": 300, "routines": 12, "batches": 100},
    "large": {"events": 1000, "routines": 24, "batches": 300},
}


def summarize(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        return {"count": 0, "median_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "total_ms": 0.0}
    ordered = sorted(samples_ms)
    p95_idx = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1) + 0.5))
    return {
        "count": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_idx], 3),
        "max_ms": round(ordered[-1], 3),
        "total_ms": round(sum(ordered), 3),
    }


class PerfCounters:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.timings_ms: dict[str, float] = {}

    def inc(self, key: str, delta: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + delta

    def add_time(self, key: str, ms: float) -> None:
        self.timings_ms[key] = self.timings_ms.get(key, 0.0) + ms

    def snapshot(self) -> tuple[dict[str, int], dict[str, float]]:
        return dict(self.counts), dict(self.timings_ms)

    def delta(self, before: tuple[dict[str, int], dict[str, float]]) -> dict[str, object]:
        b_counts, b_times = before
        counts = {
            k: self.counts.get(k, 0) - b_counts.get(k, 0)
            for k in sorted(set(self.counts) | set(b_counts))
            if self.counts.get(k, 0) - b_counts.get(k, 0) != 0
        }
        times = {
            k: round(self.timings_ms.get(k, 0.0) - b_times.get(k, 0.0), 3)
            for k in sorted(set(self.timings_ms) | set(b_times))
            if abs(self.timings_ms.get(k, 0.0) - b_times.get(k, 0.0)) > 1e-4
        }
        return {"counts": counts, "timings_ms": times}


@contextmanager
def instrument_app(counters: PerfCounters):
    patches = []

    def wrap(obj, attr, key, *, count_only=False, timing_key=None):
        had_own = isinstance(obj, type) and attr in obj.__dict__
        orig = getattr(obj, attr)

        def wrapped(*args, **kwargs):
            counters.inc(key)
            if count_only:
                return orig(*args, **kwargs)
            t0 = time.perf_counter()
            try:
                return orig(*args, **kwargs)
            finally:
                counters.add_time(timing_key or key, (time.perf_counter() - t0) * 1000.0)

        patches.append((obj, attr, had_own, orig))
        setattr(obj, attr, wrapped)

    wrap(CalendarApp, "render", "render")
    wrap(CalendarApp, "_draw_calendar_flow", "flow_draw")
    wrap(CalendarApp, "_draw_global_timeline", "timeline_draw")
    wrap(CalendarApp, "_render_global_timeline", "timeline_model_rebuild")
    wrap(CalendarApp, "_on_global_workspace_configure", "workspace_configure", count_only=True)
    wrap(CalendarApp, "_schedule_dpi_check", "dpi_check_schedule", count_only=True)
    wrap(CalendarApp, "_check_dpi_change", "dpi_check_exec")
    wrap(CalendarApp, "_schedule_view_geometry_save", "geometry_save_schedule", count_only=True)
    wrap(CalendarApp, "_save_global_geometry", "save_global_geometry")
    wrap(CalendarApp, "geometry", "wm_geometry", count_only=True)
    wrap(DayCell, "draw", "day_cell_draw")
    wrap(ThemeButton, "draw", "theme_button_draw")
    wrap(GlobalHeader, "_layout", "header_layout")
    wrap(GlobalSidebar, "arrange", "sidebar_arrange")
    wrap(GlobalDragMixin, "_schedule_hover", "hover")
    wrap(GlobalDragMixin, "_draw_schedule_preview", "drag_preview_draw")
    wrap(SqliteCalendarRepository, "_commit", "sqlite_commit", timing_key="sqlite_commit")
    wrap(SqliteCalendarRepository, "write_current", "sqlite_write_current", timing_key="sqlite_write_current")
    wrap(reminder_engine.ReminderEngine, "_rebuild", "reminder_rebuild")
    wrap(tk.Widget, "__init__", "widget_create", count_only=True)
    wrap(tk.Misc, "destroy", "widget_destroy", count_only=True)

    # Track copy.deepcopy specifically across both reminder modules and copy itself.
    orig_deepcopy = copy.deepcopy

    def counted_deepcopy(*args, **kwargs):
        counters.inc("deepcopy")
        t0 = time.perf_counter()
        try:
            return orig_deepcopy(*args, **kwargs)
        finally:
            counters.add_time("deepcopy", (time.perf_counter() - t0) * 1000.0)

    for mod in (copy, reminder_engine, notification_dispatch):
        if hasattr(mod, "deepcopy"):
            patches.append((mod, "deepcopy", True, getattr(mod, "deepcopy")))
            setattr(mod, "deepcopy", counted_deepcopy)

    orig_canvas_cfg = tk.Canvas.configure

    def wrapped_canvas_cfg(self, cnf=None, **kwargs):
        if "cursor" in kwargs or (isinstance(cnf, dict) and "cursor" in cnf):
            counters.inc("canvas_cursor_configure")
        return orig_canvas_cfg(self, cnf, **kwargs)

    patches.append((tk.Canvas, "configure", "configure" in tk.Canvas.__dict__, orig_canvas_cfg))
    tk.Canvas.configure = wrapped_canvas_cfg

    orig_label_cfg = tk.Label.configure

    def wrapped_label_cfg(self, cnf=None, **kwargs):
        if "text" in kwargs or (isinstance(cnf, dict) and "text" in cnf):
            counters.inc("label_text_configure")
        return orig_label_cfg(self, cnf, **kwargs)

    patches.append((tk.Label, "configure", "configure" in tk.Label.__dict__, orig_label_cfg))
    tk.Label.configure = wrapped_label_cfg

    try:
        yield
    finally:
        for obj, attr, had_own, orig in reversed(patches):
            if isinstance(obj, type) and not had_own:
                delattr(obj, attr)
            else:
                setattr(obj, attr, orig)


def seed_synthetic_calendar(app: CalendarApp, spec: dict[str, int]) -> None:
    store = app.store
    categories = [
        EventCategory("cat-work", "科研项目", "#6273D9", sort_order=0),
        EventCategory("cat-life", "生活杂务", "#4F927C", sort_order=1),
        EventCategory("cat-ddl", "重要节点", "#D9515D", sort_order=2),
    ]
    events: list[Event] = []
    base_day = date(2026, 8, 1)
    for i in range(spec["events"]):
        # Cluster a rich active month in 2026-08 plus historical/other-month items.
        if i < min(spec["events"], max(12, spec["events"] // 3)):
            day = base_day + timedelta(days=i % 28)
        else:
            day = base_day - timedelta(days=(i % 180) + 1)
        is_ddl = i % 7 == 0
        events.append(
            Event(
                id=f"perf-evt-{i}",
                title=f"合成事项 {i:04d} · 方案评审与对齐",
                due=f"{day.isoformat()}T{(8 + i % 10):02d}:{(i * 15) % 60:02d}",
                event_type="ddl" if is_ddl else ("urgent" if i % 11 == 0 else "general"),
                reminder=30 if i % 3 == 0 else None,
                has_time=True,
                duration_days=(i % 4) + 1,
                skip_non_working_days=(i % 5 == 0),
                end_as_ddl=(not is_ddl and i % 9 == 0 and ((i % 4) + 1) > 1),
                category_id=categories[i % len(categories)].id if i % 4 != 3 else None,
                color_mode="inherit" if i % 4 != 3 else "override",
                done=(i % 6 == 0),
            )
        )
    routines = [
        RoutineItem(
            id=f"perf-rt-{i}",
            title=f"习惯项 {i}",
            kind="habit" if i % 2 == 0 else "todo",
            frequency="daily" if i % 2 == 0 else "weekly",
            weekdays=[0, 2, 4] if i % 2 else [],
            workdays_only=False,
        )
        for i in range(spec["routines"])
    ]
    batches = {}
    deliveries = {}
    for b in range(spec["batches"]):
        b_key = f"{b:064x}"
        batches[b_key] = {
            "kind": "event",
            "when": f"2026-07-{(b % 28) + 1:02d}T09:00:00",
            "expires": f"2026-07-{(b % 28) + 2:02d}T09:00:00",
            "members": [[f"perf-evt-{b}", f"{b:064x}"]],
            "scope": "",
            "channels": {"windows": {"status": "submitted", "attempts": 1, "at": "2026-07-01T09:00:05"}},
        }
        deliveries[f"{b:064x}"] = {"batch": b_key}
    reminder_cfg = {
        "version": 1,
        "enabled": True,
        "time": "09:00",
        "general_days": 0,
        "ddl_days": 1,
        "private": False,
        "events": {},
    }
    reminder_state = {
        "version": 1,
        "batches": batches,
        "deliveries": deliveries,
        "snoozes": {},
        "handled": {},
        "qq_cutover": {},
        "runtime_actions": {},
        "config_versions": {},
        "config_actions": {},
        "config_cutovers": {},
        "qq_capability_required": {},
        "qq_capabilities": {},
    }
    store.repository.apply_changes(
        {
            "categories": categories,
            "events": events,
            "routines": routines,
            "date_states": {"2026-08-12": "leave"},
            "notified": set(),
        },
        preference_updates={
            "native_reminders": reminder_cfg,
            STATE_KEY: reminder_state,
            "theme": app.theme_name,
        },
    )
    app.select_day(date(2026, 8, 7))
    app.invalidate_reminders()
    app.reminder_engine.tick(datetime(2026, 8, 7, 9, 0))


def measure_step(counters: PerfCounters, fn) -> tuple[float, dict[str, object]]:
    snap = counters.snapshot()
    t0 = time.perf_counter()
    fn()
    dt = (time.perf_counter() - t0) * 1000.0
    return dt, counters.delta(snap)


def run_dataset(name: str, spec: dict[str, int], *, theme: str, do_profile: bool = False) -> dict[str, object]:
    profiler = cProfile.Profile() if do_profile else None
    with tempfile.TemporaryDirectory() as tmp:
        old_appdata = os.environ.get("APPDATA")
        old_local = os.environ.get("LOCALAPPDATA")
        os.environ["APPDATA"] = tmp
        os.environ["LOCALAPPDATA"] = tmp
        app = None
        session = None
        errors = []
        try:
            tracemalloc.start()
            t_cpu_start = time.process_time()
            session = open_desktop_storage(StartupOptions(isolated_directory=Path(tmp)))
            with patch.object(CalendarApp, "_start_tray_icon"), \
                 patch.object(CalendarApp, "check_reminders"), \
                 patch.object(CalendarApp, "_schedule_calendar_reminders"):
                app = CalendarApp.__new__(CalendarApp)
                app.tk = None
                app.storage_session = session
                CalendarApp.__init__(app, store=session.store)
                # Replace backends with inert mocks to guarantee zero real WinRT/QQ sends.
                silent_backend = Mock(events=queue.Queue(), status="isolated-mock", _thread=None)
                silent_qq = Mock(events=queue.Queue(), binding=Mock(return_value=None))
                app.notification_backend = silent_backend
                app.qq_reminder_backend = silent_qq
                app.reminder_engine.backend = silent_backend
                app.reminder_engine.delivery.qq = silent_qq
                app.report_callback_exception = lambda *parts: errors.append(repr(parts[1]))
                app.set_theme(theme)
                app.update()
                seed_synthetic_calendar(app, spec)
                pump(app, 0.08)

                counters = PerfCounters()
                with instrument_app(counters):
                    if profiler:
                        profiler.enable()

                    # 1. Compact date switches (same month + cross month)
                    same_times = []
                    same_snap = counters.snapshot()
                    for d in range(1, 21):
                        target = date(2026, 8, (d % 28) + 1)
                        dt, _ = measure_step(counters, lambda target=target: (app.select_day(target), app.update_idletasks()))
                        same_times.append(dt)
                    same_delta = counters.delta(same_snap)

                    cross_times = []
                    cross_snap = counters.snapshot()
                    for month in (9, 8, 7, 8):
                        target = date(2026, month, 10)
                        dt, _ = measure_step(counters, lambda target=target: (app.select_day(target), app.update_idletasks()))
                        cross_times.append(dt)
                    cross_delta = counters.delta(cross_snap)

                    # 2. Compact window drag
                    app.update_idletasks()
                    sx, sy = app.winfo_x(), app.winfo_y()
                    drag_snap = counters.snapshot()
                    drag_times = []
                    app._start_drag(SimpleNamespace(x_root=sx + 50, y_root=sy + 12))
                    for step in range(40):
                        ev = SimpleNamespace(x_root=sx + 50 + (step % 15) * 6, y_root=sy + 12 + (step % 7) * 4)
                        dt, _ = measure_step(counters, lambda ev=ev: (app._drag_window(ev), app.update()))
                        drag_times.append(dt)
                    end_compact_ms, _ = measure_step(counters, lambda: (app._end_drag(), pump(app, 0.1)))
                    drag_delta = counters.delta(drag_snap)

                    # 3. Enter Global (Flow) & measure pure window move (size unchanged)
                    app.enter_global_view()
                    app.set_global_display_mode("flow")
                    app.geometry(f"{app.dpi.px(1080)}x{app.dpi.px(740)}+120+80")
                    pump(app, 0.15)
                    gw, gh = app.winfo_width(), app.winfo_height()
                    gx, gy = app.winfo_x(), app.winfo_y()

                    move_snap = counters.snapshot()
                    move_times = []
                    for step in range(30):
                        nx = gx + (step % 10) * 8
                        ny = gy + (step % 6) * 5
                        dt, _ = measure_step(counters, lambda nx=nx, ny=ny: (app.geometry(f"+{nx}+{ny}"), app.update()))
                        move_times.append(dt)
                    move_end_ms, _ = measure_step(counters, lambda: (app._end_drag(), pump(app, 0.25)))
                    move_delta = counters.delta(move_snap)

                    # 4. Global window continuous resize (Flow), crossing 1000px dock/drawer threshold
                    resize_flow_snap = counters.snapshot()
                    resize_flow_times = []
                    widths = [940 + i * 20 for i in range(16)] + [1240 - i * 20 for i in range(16)]
                    for idx, lw in enumerate(widths):
                        lh = 660 + (idx % 8) * 18
                        pw, ph = app.dpi.px(lw), app.dpi.px(lh)
                        dt, _ = measure_step(counters, lambda pw=pw, ph=ph: (app.geometry(f"{pw}x{ph}+120+80"), app.update()))
                        resize_flow_times.append(dt)
                        time.sleep(0.012)
                    pump(app, 0.25)
                    resize_flow_delta = counters.delta(resize_flow_snap)

                    # 5. Global window continuous resize (Timeline)
                    app.set_global_display_mode("timeline")
                    pump(app, 0.15)
                    resize_tl_snap = counters.snapshot()
                    resize_tl_times = []
                    for idx, lw in enumerate(widths):
                        lh = 660 + (idx % 8) * 18
                        pw, ph = app.dpi.px(lw), app.dpi.px(lh)
                        dt, _ = measure_step(counters, lambda pw=pw, ph=ph: (app.geometry(f"{pw}x{ph}+120+80"), app.update()))
                        resize_tl_times.append(dt)
                        time.sleep(0.012)
                    pump(app, 0.25)
                    resize_tl_delta = counters.delta(resize_tl_snap)

                    # 6. Sidebar grip drag resize
                    app.geometry(f"{app.dpi.px(1180)}x{app.dpi.px(740)}+120+80")
                    pump(app, 0.15)
                    sidebar = app.global_detail_frame
                    right_edge = sidebar.winfo_rootx() + sidebar.winfo_width()
                    grip_snap = counters.snapshot()
                    grip_times = []
                    for step in range(20):
                        target_w = app.dpi.px(240 + (step % 10) * 18)
                        ev = SimpleNamespace(x_root=right_edge - target_w)
                        dt, _ = measure_step(counters, lambda ev=ev: (sidebar._resize_from_left(ev), app.update()))
                        grip_times.append(dt)
                    pump(app, 0.15)
                    grip_delta = counters.delta(grip_snap)

                    # 7. Global Flow single-click & continuous hover Motion
                    app.set_global_display_mode("flow")
                    pump(app, 0.15)
                    flow_canvas = app.global_flow_canvas
                    click_snap = counters.snapshot()
                    click_times = []
                    col_w = max(1, app._global_flow_column_width)
                    row_h = max(1, app._global_flow_week_height)
                    for step in range(16):
                        cx = int((step % 7 + 0.5) * col_w)
                        cy = int((1 + (step // 7) % 2) * row_h + app.dpi.px(8))
                        def click_once(cx=cx, cy=cy):
                            ev = SimpleNamespace(widget=flow_canvas, x=cx, y=cy,
                                                 x_root=flow_canvas.winfo_rootx() + cx,
                                                 y_root=flow_canvas.winfo_rooty() + cy, state=0)
                            app._start_flow_drag(ev)
                            app._finish_flow_drag(ev)
                            app.update_idletasks()
                        dt, _ = measure_step(counters, click_once)
                        click_times.append(dt)
                    click_delta = counters.delta(click_snap)

                    hover_snap = counters.snapshot()
                    hover_times = []
                    w_c = max(10, flow_canvas.winfo_width())
                    h_c = max(10, flow_canvas.winfo_height())
                    for step in range(120):
                        hx = 10 + (step * 17) % max(10, w_c - 20)
                        hy = 10 + (step * 11) % max(10, h_c - 20)
                        ev = SimpleNamespace(widget=flow_canvas, x=hx, y=hy,
                                             x_root=flow_canvas.winfo_rootx() + hx,
                                             y_root=flow_canvas.winfo_rooty() + hy)
                        dt, _ = measure_step(counters, lambda ev=ev: app._schedule_hover(ev))
                        hover_times.append(dt)
                    hover_delta = counters.delta(hover_snap)

                    # 8. Schedule drag preview (both same-day micro motion and cross-day motion)
                    app.set_global_display_mode("timeline")
                    pump(app, 0.15)
                    tl_canvas = app.global_timeline_canvas
                    drag_bar_times = []
                    drop_ms = 0.0
                    bar_delta = {}
                    if app._schedule_hits:
                        hit = app._schedule_hits[0]
                        px, py = position_hit(app, tl_canvas, hit)
                        press_ev = SimpleNamespace(widget=tl_canvas, x=px, y=py,
                                                   x_root=tl_canvas.winfo_rootx() + px,
                                                   y_root=tl_canvas.winfo_rooty() + py, state=0)
                        bar_snap = counters.snapshot()
                        app._schedule_press(press_ev)
                        day_w = max(1, app._global_day_width)
                        for step in range(40):
                            # Half steps jiggle within a single day cell; half move across days.
                            dx = (step % 4) * 2 if step < 20 else ((step - 19) % 5 + 1) * day_w
                            dy = (step % 5) * 2
                            mot_ev = SimpleNamespace(widget=tl_canvas, x=px + dx, y=py + dy,
                                                     x_root=press_ev.x_root + dx, y_root=press_ev.y_root + dy)
                            def one_drag_frame(mot_ev=mot_ev):
                                app._schedule_motion(mot_ev)
                                if app._schedule_render_job:
                                    app.after_cancel(app._schedule_render_job)
                                app._draw_schedule_preview()
                                app.update_idletasks()
                            dt, _ = measure_step(counters, one_drag_frame)
                            drag_bar_times.append(dt)
                        rel_ev = SimpleNamespace(widget=tl_canvas, x=px + 2 * day_w, y=py,
                                                 x_root=press_ev.x_root + 2 * day_w, y_root=press_ev.y_root)
                        drop_ms, _ = measure_step(counters, lambda: (app._schedule_release(rel_ev), app.update_idletasks()))
                        bar_delta = counters.delta(bar_snap)

                    # 9. Background idle heartbeat ticks
                    idle_snap = counters.snapshot()
                    idle_times = []
                    for _ in range(20):
                        dt, _ = measure_step(counters, lambda: (app._native_reminders_tick(), app.poll_native_notifications()))
                        idle_times.append(dt)
                    idle_delta = counters.delta(idle_snap)

                    if profiler:
                        profiler.disable()

            cpu_s = round(time.process_time() - t_cpu_start, 3)
            _cur, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert not errors, errors

            report = {
                "dataset": name,
                "spec": spec,
                "theme": theme,
                "process_cpu_s": cpu_s,
                "peak_traced_mb": round(peak_bytes / (1024 * 1024), 2),
                "compact_select_same_month": {**summarize(same_times), **same_delta},
                "compact_select_cross_month": {**summarize(cross_times), **cross_delta},
                "compact_window_drag": {**summarize(drag_times), "end_drag_ms": round(end_compact_ms, 3), **drag_delta},
                "global_window_move": {**summarize(move_times), "end_drag_ms": round(move_end_ms, 3), **move_delta},
                "global_window_resize_flow": {**summarize(resize_flow_times), **resize_flow_delta},
                "global_window_resize_timeline": {**summarize(resize_tl_times), **resize_tl_delta},
                "global_sidebar_grip_resize": {**summarize(grip_times), **grip_delta},
                "global_flow_click": {**summarize(click_times), **click_delta},
                "global_hover_motion": {**summarize(hover_times), **hover_delta},
                "global_schedule_drag": {**summarize(drag_bar_times), "drop_ms": round(drop_ms, 3), **bar_delta},
                "idle_heartbeat": {**summarize(idle_times), **idle_delta},
            }
            if profiler:
                buf = io.StringIO()
                pstats.Stats(profiler, stream=buf).sort_stats("cumtime").print_stats(45)
                buf.write("\n--- SORT BY TOTTIME ---\n")
                pstats.Stats(profiler, stream=buf).sort_stats("tottime").print_stats(45)
                report["_profile_text"] = buf.getvalue()
            return report
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
            if app is not None:
                try:
                    cleanup(app)
                except Exception:
                    stop_desktop_storage(app)
            elif session is not None:
                session.close()
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata
            if old_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_local


def main() -> None:
    parser = argparse.ArgumentParser(description="PERF-001 isolated GUI/SQLite performance benchmark")
    parser.add_argument("--datasets", default="small,medium,large", help="Comma-separated: small,medium,large")
    parser.add_argument("--theme", default="slate", choices=("modern", "slate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, help="Optional cProfile output path (collected on last dataset)")
    args = parser.parse_args()

    selected = [name.strip() for name in args.datasets.split(",") if name.strip()]
    results = {}
    profile_text = ""
    for index, name in enumerate(selected):
        collect_prof = bool(args.profile and index == len(selected) - 1)
        data = run_dataset(name, DATASETS[name], theme=args.theme, do_profile=collect_prof)
        if "_profile_text" in data:
            profile_text = data.pop("_profile_text")
        results[name] = data
        print(f"[{name}] flow_resize_p95={data['global_window_resize_flow']['p95_ms']}ms "
              f"tl_resize_p95={data['global_window_resize_timeline']['p95_ms']}ms "
              f"move_p95={data['global_window_move']['p95_ms']}ms "
              f"select_same_p95={data['compact_select_same_month']['p95_ms']}ms "
              f"idle_p95={data['idle_heartbeat']['p95_ms']}ms", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.profile and profile_text:
        args.profile.parent.mkdir(parents=True, exist_ok=True)
        args.profile.write_text(profile_text, encoding="utf-8")


if __name__ == "__main__":
    main()
