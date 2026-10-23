"""Opt-in synthetic CalendarApp notification/actions/reveal gate, isolated Store.

User clicks '10 分钟后', then the body of a second test notification. Only the
injected scheduler clock advances; Windows time and real calendar are untouched.
Launch via the interactive Explorer session for the actual desktop test.
"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch
import uuid

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from app import CalendarApp
from calendar_core import Event, Store
from dpi_utils import dpi_for_window


def main():
    if "--native-notification-activation" in sys.argv:
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex[:10]
    store = Store(options.output / ("calendar-qa-" + nonce + ".json"))
    now = datetime.now().replace(microsecond=0)
    event = Event(id="synthetic-" + nonce, title="隔离合成测试（先点10分钟后，再点正文）",
                  due=now.isoformat(), has_time=True, reminder=0)
    store.events = [event]
    report = {"event_id": event.id, "source": not bool(getattr(sys, "frozen", False)),
              "snooze_received": False, "revealed": False, "errors": []}
    with patch.object(CalendarApp, "check_reminders"):
        app = CalendarApp(store)
    app.title("桌面月历 · 隔离原生提醒集成测试")
    app.report_callback_exception = lambda *parts: report["errors"].append("".join(traceback.format_exception(*parts)))
    deadline = time.monotonic() + 240
    stage = "starting"
    original_due = event.due
    app.reminder_engine.configure({"enabled": True})
    report["identity"] = app.notification_backend.identity
    report["dpi"] = dpi_for_window(app)

    def finish():
        report["event_unchanged"] = store.event_by_id(event.id).due == original_due
        if app.editor_window and app.editor_window.winfo_exists():
            app.editor_window.close()
        if app.lifecycle.hidden:
            app.restore_from_tray()
        app.on_close()

    def poll():
        nonlocal stage, deadline
        try:
            if time.monotonic() > deadline:
                report["errors"].append("Timed out waiting for real Windows notification interaction")
                finish()
                return
            if stage == "starting" and app.tray_icon and app.tray_icon.is_available:
                app.enter_global_view()
                report["hidden"] = app.hide_to_tray()
                if not report["hidden"]:
                    raise AssertionError("QA app did not hide safely")
                app.reminder_engine.tick(datetime.now())
                stage = "wait-snooze"
            elif stage == "wait-snooze":
                snooze = app.reminder_engine._state()["snoozes"].get(event.id)
                if snooze:
                    report["snooze_received"] = True
                    report["snooze_minutes"] = round((datetime.fromisoformat(snooze["at"]) - datetime.now()).total_seconds() / 60)
                    app.reminder_engine.tick(datetime.fromisoformat(snooze["at"]) + timedelta(seconds=1))
                    stage = "wait-open"
            elif stage == "wait-open" and app.editor_window and app.editor_window.winfo_exists():
                report["revealed"] = (not app.lifecycle.hidden and app.grab_current() is app.editor_window
                                      and app.editor_window.event.id == event.id)
                deadline = time.monotonic() + 300
                stage = "wait-user-close-editor"
            elif stage == "wait-user-close-editor" and (not app.editor_window or not app.editor_window.winfo_exists()):
                report["grab_released"] = app.grab_current() is None
                finish()
                return
            report["stage"] = stage
            report["backend"] = app.notification_backend.status
            (options.output / "integration-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            app.after(200, poll)
        except Exception as error:
            report["errors"].append(str(error))
            finish()
    app.after(800, poll)
    app.mainloop()
    (options.output / "integration-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["snooze_received"] and report["revealed"] and not report["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
