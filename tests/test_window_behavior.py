import unittest

from app import CalendarApp, parse_event_due


class _AvailableTray:
    error = None
    is_available = True


class _FakeCalendar:
    def __init__(self) -> None:
        self.tray_icon = _AvailableTray()
        self._lower_job = None
        self.desktop_session_active = True
        self.saved = False
        self.hidden = False

    def _start_tray_icon(self) -> None:
        raise AssertionError("available tray icon should be reused")

    def _save_window_settings(self) -> None:
        self.saved = True

    def withdraw(self) -> None:
        self.hidden = True


class WindowBehaviorTests(unittest.TestCase):
    def test_event_time_can_be_left_blank(self) -> None:
        due, has_time = parse_event_due("2026-08-05", "")
        self.assertFalse(has_time)
        self.assertEqual(due.isoformat(timespec="minutes"), "2026-08-05T23:59")

    def test_event_time_is_kept_when_supplied(self) -> None:
        due, has_time = parse_event_due("2026-08-05", "09:30")
        self.assertTrue(has_time)
        self.assertEqual(due.isoformat(timespec="minutes"), "2026-08-05T09:30")

    def test_hide_to_tray_keeps_app_alive(self) -> None:
        fake = _FakeCalendar()
        CalendarApp.hide_to_tray(fake)
        self.assertTrue(fake.saved)
        self.assertTrue(fake.hidden)
        self.assertFalse(fake.desktop_session_active)
        self.assertIsNotNone(fake.tray_icon)


if __name__ == "__main__":
    unittest.main()
