from datetime import datetime
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reminder_engine import ReminderPlan, notification_arguments
from reminder_ui import NativeReminderMixin


class FakeApp(NativeReminderMixin):
    def __init__(self):
        self.lifecycle = SimpleNamespace(exiting=False)
        self.store = SimpleNamespace(data_file=Path("isolated-test.json"), event_by_id=Mock(return_value=SimpleNamespace(id="test-event")))
        self._init_native_reminders(Path("."))
        self.notification_backend = Mock(events=queue.Queue(), status="QA")
        self.reminder_engine = Mock()
        self.reminder_engine.config.return_value = {"enabled": True}
        self.reminder_engine.activation_is_current.return_value = True
        self._lifecycle_blocker = Mock(return_value=None)
        self.restore_from_tray = Mock(return_value=True)
        self._activate_search_result = Mock()
        self._show_feedback = Mock()
        self._active_modal_window = Mock(return_value=None)
        self.after_idle = Mock(return_value="pending-job")
        self.update_busy = False

    def send(self, arguments):
        self.notification_backend.events.put(("native", (1, arguments)))


class ReminderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.plan = ReminderPlan("test-event", "a" * 64, datetime(2026, 10, 23, 9), "2026-10-23", False)

    def test_invalid_payload_is_ignored(self):
        self.app.send("untrusted-malformed")
        self.app.poll_native_notifications()
        self.app.restore_from_tray.assert_not_called()

    def test_reuses_search_reveal_after_restore(self):
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app.restore_from_tray.assert_called_once()
        result = self.app._activate_search_result.call_args.args[0]
        self.assertEqual((result.kind, result.key), ("event", "test-event"))

    def test_modal_defers_without_stealing_grab(self):
        self.app._lifecycle_blocker.return_value = object()
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app._activate_search_result.assert_not_called()
        self.assertEqual(len(self.app._pending_notification_actions), 1)
        self.app._lifecycle_blocker.return_value = None
        self.app.poll_native_notifications()
        self.app._activate_search_result.assert_called_once()

    def test_exit_drops_callbacks(self):
        self.app.lifecycle.exiting = True
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app.restore_from_tray.assert_not_called()

    def test_deleted_event_not_recreated_or_opened(self):
        self.app.store.event_by_id.return_value = None
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app._activate_search_result.assert_not_called()
        self.app._show_feedback.assert_called_once_with("这项日程已删除")

    def test_changed_completed_or_disabled_plan_not_opened(self):
        self.app.reminder_engine.activation_is_current.return_value = False
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app._activate_search_result.assert_not_called()

    def test_repeated_callback_does_not_open_multiple_editors(self):
        for _ in range(10):
            self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app._activate_search_result.assert_called_once()

    def test_native_snooze_actions_are_not_open_actions(self):
        for minutes in (10, 30, 60):
            self.app.send(notification_arguments(self.plan, str(minutes)))
            self.app.poll_native_notifications()
            self.assertEqual(self.app.reminder_engine.snooze.call_args.args[2], minutes)
        self.app._activate_search_result.assert_not_called()

    def test_queued_action_after_global_off_is_ignored(self):
        self.app.reminder_engine.config.return_value = {"enabled": False}
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.app.restore_from_tray.assert_not_called()

    def test_reentry_restore_failure_keeps_action_for_later(self):
        self.app.restore_from_tray.return_value = False
        self.app.send(notification_arguments(self.plan))
        self.app.poll_native_notifications()
        self.assertEqual(len(self.app._pending_notification_actions), 1)
        self.app._activate_search_result.assert_not_called()

    def test_refresh_invalidations_coalesce(self):
        for _ in range(10):
            self.app.invalidate_reminders()
        self.app.after_idle.assert_called_once()
        self.assertEqual(self.app.reminder_engine.invalidate.call_count, 10)

    def test_read_only_poll_no_results_does_not_persist_or_replan(self):
        self.app.poll_native_notifications()
        self.app.reminder_engine.tick.assert_not_called()
        self.app.reminder_engine.acknowledge.assert_not_called()

    def test_explicit_test_allowed_while_off_without_configuring_engine(self):
        self.app.reminder_engine.config.return_value = {"enabled": False}
        self.app.notification_backend.send_test.return_value = "c" * 32
        self.app.test_native_notification()
        self.assertTrue(self.app._notification_test_pending)
        self.app.notification_backend.send_test.assert_called_once()
        self.app.reminder_engine.configure.assert_not_called()
        self.app.reminder_engine.acknowledge.assert_not_called()
        self.app.test_native_notification()
        self.app.notification_backend.send_test.assert_called_once()

    def test_test_results_do_not_acknowledge_event_delivery(self):
        self.app._notification_test_token = "c" * 32
        self.app._notification_test_pending = True
        self.app.notification_backend.events.put(("test_delivery", ("c" * 32, True)))
        self.app.poll_native_notifications()
        self.assertFalse(self.app._notification_test_pending)
        self.assertIn("不代表", self.app._notification_test_status)
        self.app.reminder_engine.acknowledge.assert_not_called()

    def test_test_click_does_not_open_event_or_change_data(self):
        self.app._notification_test_token = "c" * 32
        self.app.send("test=" + "c" * 32)
        self.app.poll_native_notifications()
        self.assertIn("点击", self.app._notification_test_status)
        self.app._activate_search_result.assert_not_called()
        self.app.reminder_engine._state.assert_not_called()
        self.app.reminder_engine.acknowledge.assert_not_called()

    def test_stale_test_result_does_not_replace_current_status(self):
        self.app._notification_test_token = "c" * 32
        self.app._notification_test_status = "current"
        self.app.notification_backend.events.put(("test_error", ("d" * 32, "old error")))
        self.app.poll_native_notifications()
        self.assertEqual(self.app._notification_test_status, "current")

    def test_test_timeout_does_not_claim_delivery(self):
        self.app._notification_test_pending = True
        self.app._notification_test_deadline = 0
        self.app.poll_native_notifications()
        self.assertFalse(self.app._notification_test_pending)
        self.assertIn("暂未收到", self.app._notification_test_status)

    def test_test_lease_prevents_premature_shutdown_while_off(self):
        self.app.reminder_engine.config.return_value = {"enabled": False}
        self.app._notification_test_until = 200
        with patch("reminder_ui.time.monotonic", return_value=100):
            self.app._native_reminders_tick()
        self.app.notification_backend.close.assert_not_called()

    def test_disabled_backend_stops_when_test_lease_expires(self):
        self.app.reminder_engine.config.return_value = {"enabled": False}
        old_backend = self.app.notification_backend
        with patch("reminder_ui.time.monotonic", return_value=201), \
             patch("reminder_ui.WindowsNotificationBackend") as constructor:
            self.app._native_reminders_tick()
        old_backend.close.assert_called_once()
        constructor.assert_called_once()

    def test_test_not_sent_while_exiting(self):
        self.app.lifecycle.exiting = True
        self.app.test_native_notification()
        self.app.notification_backend.send_test.assert_not_called()

    def test_closed_plan_view_is_not_rebuilt_each_poll(self):
        self.app.poll_native_notifications()
        self.app.reminder_engine.overview.assert_not_called()

    def test_plan_refresh_coalesces_and_uses_existing_poll(self):
        dialog = Mock(revision=1, refreshed_at=100)
        self.app.notification_plan_window = dialog
        self.app.reminder_engine.revision = 1
        with patch("reminder_ui.time.monotonic", return_value=101):
            self.app._refresh_reminder_views()
            self.app._refresh_reminder_views()
        dialog.refresh.assert_called_once()

    def test_plan_revision_change_refreshes_immediately(self):
        dialog = Mock(revision=1, refreshed_at=100)
        self.app.notification_plan_window = dialog
        self.app.reminder_engine.revision = 2
        self.app._reminder_views_dirty = False
        with patch("reminder_ui.time.monotonic", return_value=101):
            self.app._refresh_reminder_views()
        dialog.refresh.assert_called_once()

    def test_native_test_failure_does_not_mark_real_reminders_failed(self):
        self.app._notification_test_token = "c" * 32
        self.app.notification_backend.events.put(("native", (2, "test" + "c" * 12 + ":error")))
        self.app.poll_native_notifications()
        self.assertIn("失败", self.app._notification_test_status)
        self.app.reminder_engine.acknowledge.assert_not_called()

    def test_late_show_result_does_not_replace_confirmed_test_click(self):
        self.app._notification_test_token = "c" * 32
        self.app.send("test=" + "c" * 32)
        self.app.notification_backend.events.put(("test_delivery", ("c" * 32, True)))
        self.app.poll_native_notifications()
        self.assertIn("点击", self.app._notification_test_status)

    def test_status_and_send_failure_are_safely_recorded(self):
        self.app.notification_backend.events.put(("delivery", ("a" * 64, False)))
        self.app.notification_backend.events.put(("error", "synthetic API failure"))
        self.app.poll_native_notifications()
        self.assertEqual(self.app.reminder_engine.error, "synthetic API failure")
        self.assertFalse(self.app.reminder_engine.acknowledge.call_args.args[1])

    def test_cold_activation_returns_before_store_or_root(self):
        import app
        with patch.object(app.sys, "argv", ["app.py", "--native-notification-activation"]), \
             patch.object(app, "CalendarApp") as root, patch.object(app, "Store") as store:
            app.main()
            root.assert_not_called()
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
