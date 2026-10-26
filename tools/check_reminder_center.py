"""Real Windows/Tk reminder center checks, synthetic Store + fake notifications.

No real user calendar, registry registration or Windows Toast is accessed.
The fake backend tests UI result routing, not native delivery visibility.
"""
from dataclasses import asdict
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import sys
import tempfile
import time
import traceback
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Event, Store
from dpi_utils import WorkArea, dpi_for_window
from reminder_engine import STATE_KEY, plans_for_store
from tools.check_interactions import cleanup, pump
from tools.check_reminder_ui import visible
from win_integration import is_window_topmost


def check_native_test(report_path):
    """Opt-in single real Toast through the actual settings button, defaults off."""
    report = {"accepted": False, "clicked": False, "errors": []}
    with tempfile.TemporaryDirectory(prefix="calendar-center-native-") as directory:
        store = Store(Path(directory) / "calendar_data.json")
        with patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
        try:
            pump(app, .3)
            app.open_reminder_settings()
            window = app.notification_settings_window
            pump(app, .1)
            before = store.data_file.read_bytes() if store.data_file.exists() else None
            window.test_button.invoke()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                pump(app, .05)
                app.poll_native_notifications()
                status = app._notification_test_status
                if "已提交给 Windows" in status or app._notification_test_clicked:
                    report["accepted"] = True
                if app._notification_test_clicked:
                    report["clicked"] = True
                    break
                if "未成功" in status or "报告测试通知失败" in status:
                    report["errors"].append(status)
                    break
            report["status"] = app._notification_test_status
            report["dpi"] = dpi_for_window(app)
            report["reminders_still_off"] = not app.reminder_engine.config()["enabled"]
            report["data_unchanged"] = before == (store.data_file.read_bytes() if store.data_file.exists() else None)
            report["no_event_ledger"] = not app.reminder_engine._state()["deliveries"]
            window.close()
            pump(app, .12)
            report["grab_released"] = app.grab_current() is None
            assert report["accepted"], report
            assert report["reminders_still_off"] and report["data_unchanged"] and report["no_event_ledger"], report
            assert report["grab_released"] and not report["errors"], report
        finally:
            window = app.notification_settings_window
            if window and window.winfo_exists():
                window.close()
            cleanup(app)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-test", action="store_true", help="Explicitly send one synthetic Windows notification")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    if arguments.native_test:
        build = Path(__file__).resolve().parents[1] / "build"
        report = (arguments.report or build / "reminder-center/native-test.json").resolve()
        if not report.is_relative_to(build):
            parser.error("Native QA report must be under this repository's ignored build directory")
        return check_native_test(report)
    errors, cases = [], []
    with tempfile.TemporaryDirectory(prefix="calendar-reminder-center-") as directory:
        store = Store(Path(directory) / "calendar_data.json")
        due = (datetime.now() + timedelta(days=30)).date().isoformat() + "T23:59"
        store.events = [Event(id=f"synthetic-{index}", title=f"隔离计划 {index} 中文 English 🔔", due=due, has_time=False)
                        for index in range(65)]
        original_events = [asdict(event) for event in store.events]
        with patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .3)
                native_dpi = dpi_for_window(app)
                backend = Mock()
                backend.events, backend.status, backend._thread = queue.Queue(), "隔离假通知服务", None
                backend.send_test.return_value = "f" * 32
                app.notification_backend = backend
                app.reminder_engine.backend = backend
                app.reminder_engine.configure({"enabled": True})
                plans = {p.event_id: p for p in plans_for_store(store, app.reminder_engine.config())}
                failed = plans["synthetic-0"]
                app.reminder_engine._save(STATE_KEY, {"version": 1, "deliveries": {
                    failed.key: {"status": "failed", "attempts": 1, "at": datetime.now().isoformat()}}})
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for view in ("compact", "global"):
                        (app.enter_global_view if view == "global" else app.return_to_compact_view)()
                        for pinned in (False, True):
                            app.window_mode = "pinned" if pinned else "desktop"
                            app.apply_window_mode()
                            app.open_reminder_settings()
                            settings = app.notification_settings_window
                            pump(app, .1)
                            assert settings.title() == "后台与提醒"
                            assert app.grab_current() is settings
                            assert not app.hide_to_tray(), "settings modal could be hidden"
                            before = store.data_file.read_bytes()
                            settings.test_button.invoke()
                            assert settings.test_button.cget("state") == "disabled"
                            backend.events.put(("test_delivery", ("f" * 32, True)))
                            app.poll_native_notifications()
                            assert "不代表" in settings.test_status.get()
                            assert settings.test_button.cget("state") == "normal"
                            assert before == store.data_file.read_bytes(), "test notification wrote calendar data"
                            settings.show_plans()
                            plan_window = app.notification_plan_window
                            pump(app, .12)
                            assert not settings.winfo_exists()
                            assert app.grab_current() is plan_window
                            assert str(plan_window.transient()) == str(app)
                            assert not app.hide_to_tray(), "plan modal could be hidden"
                            assert not app.on_close(), "exit bypassed plan modal"
                            assert not app.lifecycle.exiting
                            assert len(plan_window.rows) == 65
                            assert visible(plan_window.close_button, plan_window)
                            plan_window.tree.yview_moveto(0)
                            plan_window.tree.event_generate("<MouseWheel>", delta=-120, x=60, y=60)
                            pump(app, .04)
                            assert plan_window.tree.yview()[0] > 0, "mouse wheel did not scroll"
                            plan_window.scrollbar.tk.call(plan_window.scrollbar.cget("command"), "moveto", 1)
                            pump(app, .04)
                            last = "event:synthetic-64"
                            assert plan_window.tree.bbox(last), "last entry cannot be reached"
                            before = store.data_file.read_bytes()
                            plan_window.refresh()
                            plan_window.refresh()
                            assert before == store.data_file.read_bytes(), "read-only list wrote calendar data"
                            snooze = plans["synthetic-1"]
                            app.reminder_engine.snooze(snooze.event_id, snooze.key, 30, datetime.now())
                            app.poll_native_notifications()
                            assert plan_window.rows["event:synthetic-1"].snoozed
                            plan_window.tree.selection_set("event:synthetic-1")
                            plan_window.show_selection()
                            assert plan_window.cancel_button.cget("state") == "normal"
                            plan_window.cancel_button.invoke()
                            assert "event:synthetic-1" not in plan_window.rows
                            # Renew just this synthetic base for the next UI case.
                            state = app.reminder_engine._state()
                            state["deliveries"].pop(snooze.key, None)
                            app.reminder_engine._save(STATE_KEY, state)
                            plan_window.tree.selection_set(last)
                            plan_window.show_selection()
                            plan_window.open_button.invoke()
                            pump(app, .12)
                            assert app.notification_plan_window is None
                            assert app.editor_window and app.grab_current() is app.editor_window
                            assert app.editor_window.winfo_viewable()
                            app.editor_window.close()
                            pump(app, .12)
                            assert app.grab_current() is None
                            assert is_window_topmost(app) == pinned
                            app.open_search()
                            pump(app, .05)
                            assert app.search_window.winfo_exists()
                            app.search_window.close()
                            pump(app, .1)
                            cases.append((theme, view, pinned))
                # Small work area / high DPI: both windows retain reachable actions.
                with patch.object(app.dpi, "work_area", return_value=WorkArea(0, 0, 1280, 800)):
                    app.open_reminder_plans()
                    window = app.notification_plan_window
                    pump(app, .1)
                    assert window.winfo_height() <= 800
                    assert visible(window.close_button, window)
                    window.close()
                    app.open_reminder_settings()
                    window = app.notification_settings_window
                    pump(app, .1)
                    assert window.winfo_height() <= 800
                    window.scroll_canvas.yview_moveto(1)
                    pump(app, .05)
                    assert visible(window.cancel_button, window)
                    window.close()
                assert original_events == [asdict(event) for event in store.events]
                assert app.event_commands.undo_count == 0
                assert not errors, errors
                print(json.dumps({"dpi": native_dpi, "theme_view_pinned_cases": len(cases),
                                  "plan_groups_scroll_edit_cancel": "passed", "synthetic_test_ui": "passed",
                                  "modal_focus_search": "passed", "small_work_area": "passed",
                                  "event_data_unchanged": True, "errors": errors}, ensure_ascii=False))
            finally:
                for window in (app.notification_settings_window, app.notification_plan_window, app.editor_window):
                    if window and window.winfo_exists():
                        window.close()
                cleanup(app)


if __name__ == "__main__":
    main()
