"""Isolated real-Tk four-theme reminder settings/navigation smoke; no Toasts.

Uses a fake transport to exercise queued activation and Snooze with a real
CalendarApp. Native delivery is verified separately by check_native_notifications.
"""
import json
from pathlib import Path
import queue
import sys
import tempfile
import traceback
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Event, Store
from dpi_utils import dpi_for_window
from reminder_engine import notification_arguments
from reminder_ui import ReminderSettingsDialog
from tools.check_interactions import cleanup, pump
from win_integration import is_window_topmost


def visible(widget, window):
    return (widget.winfo_ismapped() and widget.winfo_rooty() >= window.winfo_rooty()
            and widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height())


def main():
    errors, cases = [], []
    with tempfile.TemporaryDirectory(prefix="calendar-native-reminders-") as directory:
        store = Store(Path(directory) / "calendar_data.json")
        store.events = [Event(id="synthetic-qa-event", title="隔离提醒测试", due="2031-10-23T23:59", has_time=False)]
        with patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .4)
                native_dpi = dpi_for_window(app)
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for view in ("compact", "global"):
                        (app.enter_global_view if view == "global" else app.return_to_compact_view)()
                        for pinned in (False, True):
                            app.window_mode = "pinned" if pinned else "desktop"
                            app.apply_window_mode()
                            dialog = ReminderSettingsDialog(app)
                            app.notification_settings_window = dialog
                            pump(app, .12)
                            assert str(dialog.transient()) == str(app)
                            assert app.grab_current() is dialog
                            assert dialog.winfo_height() <= app.dpi.work_area().height
                            assert not app.notification_backend._thread, "Disabled mode registered a transport"
                            dialog.clock.set("invalid")
                            dialog.save()
                            pump(app, .3)
                            assert "HH:MM" in dialog.status.get(), "validation feedback was lost"
                            dialog.scroll_canvas.yview_moveto(1)
                            pump(app, .04)
                            assert visible(dialog.cancel_button, dialog), "bottom content cannot be reached"
                            dialog.close()
                            pump(app, .15)
                            assert app.grab_current() is None
                            assert is_window_topmost(app) == pinned
                            app.open_search()
                            pump(app, .06)
                            assert app.search_window.winfo_exists()
                            app.search_window.close()
                            pump(app, .06)
                            cases.append((theme, view, pinned))
                app.reminder_engine.configure({"enabled": True})
                # Future synthetic event prevents real delivery while testing the
                # real navigation path with fake, queued Windows callbacks.
                backend = Mock()
                backend.events, backend.status = queue.Queue(), "QA fake transport"
                app.notification_backend = backend
                app.reminder_engine.backend = backend
                app._native_reminders_tick()
                plan = app.reminder_engine.plans["synthetic-qa-event"]
                app.enter_global_view()
                app._global_category_filter_ids = set()
                assert app.hide_to_tray()
                backend.events.put(("native", (1, notification_arguments(plan))))
                app.poll_native_notifications()
                pump(app, .15)
                assert not app.lifecycle.hidden
                assert app.editor_window and app.grab_current() is app.editor_window
                assert app.selected.isoformat() == "2031-10-23"
                app.editor_window.close()
                pump(app, .15)
                backend.events.put(("native", (1, notification_arguments(plan, "30"))))
                app.poll_native_notifications()
                assert app.reminder_engine._state()["snoozes"][plan.event_id]
                assert store.events[0].due == "2031-10-23T23:59"
                assert not errors, errors
                print(json.dumps({"native_dpi": native_dpi, "settings_cases": len(cases),
                                  "modal_search_pinned": "passed", "fake_queued_hidden_reveal": "passed",
                                  "fake_action_snooze": "passed", "errors": errors}, ensure_ascii=False))
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
