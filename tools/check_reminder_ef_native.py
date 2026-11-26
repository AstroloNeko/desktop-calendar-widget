"""Opt-in real WinRT reminder acceptance on synthetic calendars."""
from argparse import ArgumentParser
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
from pathlib import Path
from queue import SimpleQueue
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from app import CalendarApp
from calendar_core import Event, Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from native_notifications import WinRTTransport
from tools.check_interactions import cleanup, pump


def until(app, observe, timeout=12):
    deadline = monotonic()+timeout
    while monotonic() < deadline:
        pump(app,.05)
        if value := observe():
            return value
    return None


def visible_test_banner(title):
    # Match only this short-lived synthetic title. Do not collect unrelated UI.
    script = """
    Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
    $root=[Windows.Automation.AutomationElement]::RootElement
    $name=[Windows.Automation.AutomationElement]::NameProperty
    $condition=[Windows.Automation.PropertyCondition]::new($name,'TITLE')
    $shells=@(Get-Process explorer,ShellExperienceHost,ShellHost,StartMenuExperienceHost -ErrorAction SilentlyContinue |
              ForEach-Object { $_.Id })
    $until=(Get-Date).AddSeconds(7)
    while ((Get-Date) -lt $until) {
        $windows=$root.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.Condition]::TrueCondition)
        foreach ($window in $windows) {
            if ($window.Current.ProcessId -notin $shells) { continue }
            $item=$window.FindFirst([Windows.Automation.TreeScope]::Descendants,$condition)
            if ($item -and -not $item.Current.IsOffscreen) { 'visible'; exit 0 }
        }
        Start-Sleep -Milliseconds 100
    }
    exit 1
    """.replace("TITLE",title)
    return subprocess.Popen(["powershell","-NoProfile","-NonInteractive","-Command",script],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)


def minimized(app):
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = (wintypes.HWND,wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsIconic.argtypes = (wintypes.HWND,)
    return bool(user32.IsIconic(user32.GetAncestor(app.winfo_id(),2)))


def scenario(mode, root):
    store = Store(root/f"{mode}-{uuid4().hex}.sqlite3",repository_factory=SqliteCalendarRepository)
    observations = SimpleQueue()
    report = {"mode":mode,"windows_submitted":False,"human_activation":False,"on_screen_marker":None,"winrt_history":[],
              "errors":[]}

    class ObservedTransport(WinRTTransport):
        def show(self, tag, xml):
            super().show(tag,xml)
            try:
                history = self.history_count()
            except OSError:
                history = None
            observations.put({"tag":tag,"history_count":history})

    app = monitor = None
    with patch("notification_backend.WinRTTransport",ObservedTransport):
        try:
            app = CalendarApp(store)
            app.report_callback_exception = lambda *_error: report["errors"].append("Tk callback failed")
            engine = app.reminder_engine
            trigger = (datetime.now()+timedelta(seconds=5)).replace(microsecond=0)
            app.event_commands.upsert(Event("isolated",f"EF合成事项 {mode}",trigger.isoformat(),reminder=0))
            engine.configure({"enabled":mode not in ("brief","test"),"qq":False,
                              "brief":{"enabled":mode == "brief","time":datetime.now().strftime("%H:%M")}})
            if mode == "settings":
                app.open_reminder_settings()
                report["mode_active"] = app.grab_current() is app.notification_settings_window
            elif mode == "editor":
                app._activate_search_result(SimpleNamespace(kind="event",key="isolated"))
                report["mode_active"] = app.grab_current() is app.editor_window
            elif mode == "minimized":
                app.enter_global_view()
                pump(app,.6)
                app.minimize_global()
                report["mode_active"] = bool(until(app,lambda: minimized(app),timeout=2))
            elif mode == "tray":
                pump(app,.7)
                report["mode_active"] = bool(app.hide_to_tray() and app.lifecycle.hidden)
            elif mode == "test":
                marker = "EF Windows QA "+uuid4().hex[:8]
                monitor = visible_test_banner(marker)
                send_test = app.notification_backend.send_test
                with patch.object(app.notification_backend,"send_test",lambda: send_test(title=marker)):
                    app.test_native_notification()
                until(app,lambda: not app._notification_test_pending,timeout=18)
                try:
                    output, _ = monitor.communicate(timeout=12)
                    report["on_screen_marker"] = output.strip() == b"visible"
                    report["on_screen_probe_exit"] = monitor.returncode
                except subprocess.TimeoutExpired:
                    monitor.kill()
                    monitor.communicate()
                    report["on_screen_probe_timeout"] = True
                report["human_activation"] = app._notification_test_clicked
                report["status"] = app._notification_test_status
                report["windows_submitted"] = report["human_activation"] or "已提交给 Windows" in report["status"]

            def submitted(kind):
                return [(key,row) for key,row in engine._state()["batches"].items()
                        if row["kind"] == kind and row["channels"].get("windows",{}).get("status") == "submitted"]

            if mode != "test":
                kind = "brief" if mode == "brief" else "event"
                report["windows_submitted"] = bool(until(app,lambda: submitted(kind)))
                report["batches"] = len(submitted(kind))
            if mode == "snooze" and report["windows_submitted"]:
                original = store.event_by_id("isolated").due
                now = datetime.now()
                row = next(row for row in engine.runtime_snapshot(now)[1].values() if row["current"])
                proof = engine.apply_runtime_action({"action":"snooze","minutes":1},
                    [{"key":row["key"],"revision":row["revision"],"event_id":"isolated"}],now)
                report["snoozed_until"] = proof["until"]
                report["event_time_preserved"] = store.event_by_id("isolated").due == original
                report["resubmitted_after_snooze"] = bool(until(app,lambda: len(submitted("event")) == 2,timeout=70))
            while not observations.empty():
                report["winrt_history"].append(observations.get())
            report["backend"] = app.notification_backend.status
        except (OSError, RuntimeError, ValueError) as error:
            report["errors"].append(str(error))
        finally:
            if monitor is not None and monitor.poll() is None:
                monitor.kill()
                monitor.communicate()
            try:
                if app is not None:
                    for window in (app.notification_settings_window, app.editor_window):
                        if window is not None and window.winfo_exists():
                            window.close()
                    if app.lifecycle.hidden:
                        app.restore_from_tray()
                    cleanup(app)
            finally:
                store.repository.close()
    return report


def main():
    if "--native-notification-activation" in sys.argv:
        return 0
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--modes",nargs="+",choices=("settings","editor","minimized","tray","test","brief","snooze"),
                        default=("settings","editor","minimized","tray","test","brief","snooze"))
    parser.add_argument("--send-native", action="store_true", help="Send real synthetic Windows notifications")
    args = parser.parse_args()
    if not args.send_native:
        parser.error("real Windows notifications require --send-native")
    args.output.mkdir(parents=True,exist_ok=True)
    reports = []
    for mode in args.modes:
        row = scenario(mode,args.output)
        reports.append(row)
        print(f"{mode}: WinRT submitted={row['windows_submitted']}",flush=True)
        (args.output/"native-acceptance.json").write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding="utf-8")
    return 0 if all(row["windows_submitted"] and row.get("mode_active",True)
                    and row.get("resubmitted_after_snooze",True) and not row["errors"] for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
