"""Run one opt-in real Windows toast using eight isolated SQLite events."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
import traceback
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import CalendarApp
from calendar_core import Event, Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from dpi_utils import dpi_for_window
from tools.check_habit_schedules import capture_own_window


def check_lifecycle(output):
    """Drive actual Tk windows with a stubbed toast transport and real timers."""
    from tools.check_interactions import cleanup, pump

    results = {}
    for mode in ("settings", "plans", "editor", "minimized", "tray"):
        store = Store(output / f"{mode}-{uuid.uuid4().hex}.json")
        app = CalendarApp(store)
        errors = []
        app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
        backend = Mock(events=queue.Queue(), status="Stub")
        def accept(notice, _title, _text, _actions):
            backend.events.put(("notice_delivery", (notice.key, notice.attempt, "submitted", "")))
        backend.send.side_effect = accept
        app.notification_backend = app.reminder_engine.backend = backend
        try:
            trigger = (datetime.now() + timedelta(seconds=3)).replace(microsecond=0)
            event = Event(id="qa-event", title="Lifecycle fixture", due=trigger.isoformat(), has_time=True, reminder=0)
            app.event_commands.upsert(event)
            app.reminder_engine.configure({"enabled": True})
            if mode == "settings":
                app.open_reminder_settings()
            elif mode == "plans":
                app.open_reminder_plans()
            elif mode == "editor":
                app._activate_search_result(SimpleNamespace(kind="event", key=event.id))
            elif mode == "minimized":
                app.enter_global_view()
                app.minimize_global()
            else:
                pump(app, .7)
                assert app.hide_to_tray()
            def mode_active():
                if mode == "settings":
                    settings = app.notification_settings_window
                    return settings is not None and app.grab_current() is settings
                if mode == "plans":
                    return app.notification_plan_window is not None
                if mode == "editor":
                    return app.editor_window is not None and app.grab_current() is app.editor_window
                if mode == "minimized":
                    return app.state() == "iconic"
                return app.lifecycle.hidden
            deadline = time.monotonic() + 8
            submitted = False
            while not submitted and not errors and time.monotonic() < deadline:
                pump(app, .05)
                submitted = bool(app.reminder_engine.overview(datetime.now()).submitted)
            results[mode] = {"submitted": submitted, "mode_active": mode_active(),
                             "show_calls": backend.send.call_count, "errors": errors}
        finally:
            for window in (app.notification_settings_window, app.editor_window):
                if window is not None and window.winfo_exists():
                    window.close()
            if app.lifecycle.hidden:
                app.restore_from_tray()
            cleanup(app)
    (output / "lifecycle-result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(case["submitted"] and case["mode_active"] and case["show_calls"] == 1 and not case["errors"]
                    for case in results.values()) else 2


def main():
    if "--native-notification-activation" in sys.argv:
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--theme", default="modern", choices=("modern", "paper", "aero"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--visual-only", action="store_true")
    mode.add_argument("--lifecycle-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.lifecycle_only:
        return check_lifecycle(args.output)
    store = Store(args.output / f"isolated-{uuid.uuid4().hex}.sqlite3", repository_factory=SqliteCalendarRepository)
    app = CalendarApp(store)
    app.set_theme(args.theme)
    report = {"theme": args.theme, "frozen": bool(getattr(sys, "_MEIPASS", None)),
              "errors": [], "dpi": dpi_for_window(app), "database": "isolated SQLite",
              "identity": app.notification_backend.identity}
    app.report_callback_exception = lambda *parts: report["errors"].append("".join(traceback.format_exception(*parts)))
    report_path = args.output / f"{args.theme}-result.json"
    max_heartbeat = 0
    last_heartbeat = time.monotonic()
    deadline = last_heartbeat + 55
    start = (datetime.now() + timedelta(seconds=16)).replace(microsecond=0)
    if args.visual_only:
        preview_now = datetime(2031, 4, 16, 8)
        app._reminder_preview_clock = lambda: preview_now
        start = preview_now.replace(hour=10)
    window = None

    def finish():
        report["max_event_loop_gap_ms"] = round(max_heartbeat * 1000)
        report["backend"] = app.notification_backend.status
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if window and window.winfo_exists():
            window.close()
        app.on_close()

    def heartbeat():
        nonlocal last_heartbeat, max_heartbeat
        now = time.monotonic()
        max_heartbeat = max(max_heartbeat, now - last_heartbeat)
        last_heartbeat = now
        app.after(35, heartbeat)

    def screenshot(name):
        app.update_idletasks()
        capture_own_window(window, args.output / f"{args.theme}-{name}.png")

    def setup():
        nonlocal window
        for i in range(1, 9):
            app.event_commands.upsert(Event(id=f"qa-{i}", title=f"合成事项 {i} · 同一分钟提醒",
                                            due=start.isoformat(timespec="seconds"), has_time=True, reminder=0))
        app.reminder_engine.configure({"enabled": True, "windows": not args.visual_only})
        app.open_reminder_settings()
        window = app.notification_settings_window
        report["tabs"] = [window.tabs.tab(tab, "text") for tab in window.tabs.tabs()]
        assert len(report["tabs"]) == 4
        app.after(150, lambda: capture_tab(0))

    def capture_tab(index):
        names = ("rules", "brief", "channels", "plans")
        window.tabs.select(index)
        if index == 1:
            window.refresh_preview()
        if index == 3:
            window.show_plans()
        screenshot(names[index])
        if index == 1 and args.visual_only:
            assert "8 项" in window.preview_heading.cget("text")
            window.brief_canvas.yview_moveto(1)
            from tools.check_interactions import pump
            pump(app, .18)
            screenshot("brief-preview")
            def widget_text(widget):
                value = widget.cget("text") if isinstance(widget, tk.Label) else ""
                return value + "".join(widget_text(child) for child in widget.winfo_children())
            assert "合成事项" in widget_text(window.preview_body)
            window.private.set(True)
            assert "合成事项" not in widget_text(window.preview_body)
            window.private.set(False)
        if index == 3 and args.visual_only:
            assert window.plan.counts["upcoming"].get() == "8"
            window.plan.filter_cards["attention"].invoke()
            assert not window.plan.rows and window.plan.counts["upcoming"].get() == "8"
            window.plan.show_all_button.invoke()
            report["plan_filter"] = "passed"
            report["brief_preview_privacy"] = "passed"
        if index < 3:
            app.after(160, lambda: capture_tab(index + 1))
        elif args.visual_only:
            report["visible_plan_members"] = len(window.plan.rows)
            finish()
        else:
            app.after(400, start_measurement)

    def start_measurement():
        nonlocal last_heartbeat, max_heartbeat
        last_heartbeat = time.monotonic()
        max_heartbeat = 0
        poll()

    def poll():
        state = app.reminder_engine._state()
        batches = list(state["batches"].items())
        if len(batches) == 1 and batches[0][1]["channels"]["windows"]["status"] == "submitted":
            key, batch = batches[0]
            start_count = len(batch["members"])
            report.update(trigger=start.isoformat(), batch=key, windows=batch["channels"]["windows"],
                          batches=len(batches), member_count=start_count,
                          distinct_members=len({item[0] for item in batch["members"]}),
                          plan_tab_open=app.grab_current() is window and window.tabs.index("current") == 3)
            app.after(4300, lambda: verify(key))
            return
        if time.monotonic() > deadline:
            report["errors"].append("Windows submission not recorded before deadline")
            report["batches"] = state["batches"]
            finish()
            return
        app.after(100, poll)

    def verify(key):
        state = app.reminder_engine._state()
        report["still_one_batch"] = len(state["batches"]) == 1
        report["attempts"] = state["batches"][key]["channels"]["windows"]["attempts"]
        report["distinct_visible_plan_rows"] = len(window.plan.rows)
        window.plan.show_notice(key)
        report["all_items_in_detail"] = all(f"合成事项 {i} " in window.plan.detail.get() for i in range(1, 9))
        screenshot("submitted")
        finish()

    app.after(35, heartbeat)
    app.after(200, setup)
    app.after(59000, finish)
    app.mainloop()
    if args.visual_only:
        return 0 if not report["errors"] and report.get("visible_plan_members") == 8 else 2
    return 0 if (not report["errors"] and report.get("member_count") == report.get("distinct_members") == 8
                 and report.get("plan_tab_open") and report.get("still_one_batch") and report.get("attempts") == 1
                 and report.get("all_items_in_detail") and report["max_event_loop_gap_ms"] < 300) else 2


if __name__ == "__main__":
    raise SystemExit(main())
