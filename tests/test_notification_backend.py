from datetime import datetime
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from native_notifications import NativeNotificationError
from notification_backend import WindowsNotificationBackend
from reminder_engine import ReminderPlan


class NotificationBackendTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.transport.setting.return_value = 0
        self.registration = Mock()
        self.loader = patch("notification_backend.WinRTTransport", return_value=self.transport)
        self.registrar = patch("notification_backend.NotificationRegistration", return_value=self.registration)
        self.load = self.loader.start()
        self.register = self.registrar.start()
        self.addCleanup(self.loader.stop)
        self.addCleanup(self.registrar.stop)
        self.backend = WindowsNotificationBackend(Path("."), Path("synthetic.json"))
        self.addCleanup(self.backend.close)
        self.plan = ReminderPlan("synthetic", "b" * 64, datetime(2026, 10, 23, 9), "2026-10-23", False)

    def result(self, kind):
        for _ in range(20):
            event_kind, value = self.backend.events.get(timeout=2)
            if event_kind == kind:
                return value
        self.fail("backend did not report " + kind)

    def test_constructor_has_no_registration_side_effect(self):
        self.load.assert_not_called()
        self.register.assert_not_called()
        self.assertIsNone(self.backend._thread)

    def test_send_and_disposal_use_one_non_tk_owner(self):
        owners = []
        self.transport.show.side_effect = lambda *args: owners.append(threading.get_ident())
        self.transport.close.side_effect = lambda: owners.append(threading.get_ident())
        self.backend.send(self.plan, "synthetic", "no user data", [])
        self.assertTrue(self.result("delivery")[1])
        self.backend.close()
        self.assertEqual(len(set(owners)), 1)
        self.assertNotEqual(owners[0], threading.get_ident())
        self.assertFalse(self.backend._thread.is_alive())
        self.transport.silence.assert_called_once()
        self.registration.close.assert_called_once()

    def test_native_callback_only_queues_an_event(self):
        self.backend.start()
        self.result("status")
        sink = self.load.call_args.kwargs["event_sink"]
        sink((1, "opaque-action"))
        self.assertEqual(self.result("native"), (1, "opaque-action"))

    def test_disabled_by_windows_never_shown_or_accepted(self):
        self.transport.setting.return_value = 2
        self.backend.send(self.plan, "synthetic", "body", [])
        self.assertEqual(self.result("delivery"), (self.plan.key, False))
        self.transport.show.assert_not_called()

    def test_unknown_setting_not_misreported_as_enabled(self):
        self.transport.setting.side_effect = NativeNotificationError("unknown", hresult=0x80070490)
        self.backend.send(self.plan, "synthetic", "body", [])
        self.assertIn("未知", self.result("status"))
        self.assertTrue(self.result("delivery")[1])

    def test_other_permission_error_does_not_bypass_check(self):
        self.transport.setting.side_effect = NativeNotificationError("denied", hresult=0x80070005)
        self.backend.send(self.plan, "synthetic", "body", [])
        self.assertFalse(self.result("delivery")[1])
        self.transport.show.assert_not_called()

    def test_registration_failure_safe_and_no_delivery(self):
        self.registration.register.side_effect = OSError("synthetic registration failure")
        self.backend.send(self.plan, "synthetic", "body", [])
        self.assertFalse(self.result("delivery")[1])
        self.transport.show.assert_not_called()
        self.backend.close()
        self.registration.close.assert_called_once()

    def test_native_show_failure_not_success(self):
        self.transport.show.side_effect = OSError("synthetic RPC failure")
        self.backend.send(self.plan, "synthetic", "body", [])
        self.assertFalse(self.result("delivery")[1])

    def test_many_sends_do_not_create_more_threads(self):
        self.backend.send(self.plan, "synthetic", "body", [])
        self.result("delivery")
        owner = self.backend._thread
        for _ in range(80):
            self.backend.send(self.plan, "synthetic", "body", [])
            self.result("delivery")
        self.assertIs(owner, self.backend._thread)
        self.assertEqual(self.load.call_count, 1)
        self.assertEqual(self.register.call_count, 1)

    def test_close_is_idempotent_and_late_send_rejected(self):
        self.backend.start()
        self.result("status")
        self.backend.close()
        self.backend.close()
        self.transport.close.assert_called_once()
        with self.assertRaises(RuntimeError):
            self.backend.send(self.plan, "late", "ignored", [])

    def test_canceled_pending_send_is_not_delivered(self):
        gate = threading.Event()
        self.registration.register.side_effect = lambda _: gate.wait(2)
        self.backend.send(self.plan, "synthetic", "body", [])
        self.backend.remove(self.plan.tag)
        gate.set()
        self.result("status")
        self.backend.close()
        self.transport.show.assert_not_called()


if __name__ == "__main__":
    unittest.main()
