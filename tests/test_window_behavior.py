import unittest
from datetime import date

from app import CalendarApp, DayCell, EVENT_STRIPE_WIDTH, event_stripe_color, main_region_visibility, parse_event_due
from calendar_core import Event


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
    def test_collapsed_layout_keeps_quick_add_and_pinned_ddl_visible(self) -> None:
        visible = main_region_visibility(False, pinned_ddl_count=2, regular_ddl_count=3)
        self.assertTrue(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.agenda_header)
        self.assertTrue(visible.footer)
        self.assertFalse(visible.daily_content)
        self.assertFalse(visible.regular_ddl)

    def test_expanded_layout_shows_daily_and_regular_ddl_content(self) -> None:
        visible = main_region_visibility(True, pinned_ddl_count=0, regular_ddl_count=2)
        self.assertFalse(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.daily_content)
        self.assertTrue(visible.regular_ddl)

    def test_empty_collapsed_layout_keeps_persistent_regions(self) -> None:
        visible = main_region_visibility(False, pinned_ddl_count=0, regular_ddl_count=0)
        self.assertFalse(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.agenda_header)
        self.assertTrue(visible.footer)
        self.assertFalse(visible.daily_content)
        self.assertFalse(visible.regular_ddl)

    def test_regular_ddl_region_adds_height_without_reducing_daily_viewport(self) -> None:
        fake = type(
            "FakeCalendar",
            (),
            {
                "agenda_open": True,
                "winfo_screenheight": lambda self: 2000,
                "_ddl_canvas_height": CalendarApp._ddl_canvas_height,
                "_ddl_region_height": CalendarApp._ddl_region_height,
            },
        )()
        without_regular = CalendarApp._desired_window_height(fake, 0, 0)
        with_regular = CalendarApp._desired_window_height(fake, 0, 1)
        self.assertGreater(with_regular, without_regular)
        self.assertEqual(with_regular - without_regular, fake._ddl_region_height(1))

        fake.agenda_open = False
        self.assertEqual(
            CalendarApp._desired_window_height(fake, 0, 1),
            CalendarApp._desired_window_height(fake, 0, 0),
        )

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

    def test_double_click_opens_day_detail_instead_of_editor(self) -> None:
        selected: list[date] = []
        details: list[date] = []

        class FakeApp:
            def select_day(self, day: date) -> None:
                selected.append(day)

            def open_day_detail(self, day: date) -> None:
                details.append(day)

            def open_editor(self, **_kwargs) -> None:
                raise AssertionError("double click must not open the editor directly")

        fake_cell = type("FakeCell", (), {"app": FakeApp(), "day": date(2026, 8, 5)})()
        result = DayCell._double_click(fake_cell)
        self.assertEqual(result, "break")
        self.assertEqual(selected, [date(2026, 8, 5)])
        self.assertEqual(details, [date(2026, 8, 5)])

    def test_event_stripe_uses_item_color_independently_from_type(self) -> None:
        theme = type("FakeTheme", (), {"event_done": "#A0A0A0"})()
        event = Event("ddl", "DDL", "2026-08-05T10:00", color="#E65D67", event_type="ddl")
        self.assertEqual(event_stripe_color(theme, event), "#E65D67")
        self.assertEqual(EVENT_STRIPE_WIDTH, 4)
        event.done = True
        self.assertEqual(event_stripe_color(theme, event), "#A0A0A0")

    def test_quick_add_passes_selected_color_and_event_type(self) -> None:
        captured: dict[str, object] = {}

        class FakeVar:
            value = "快速事项"

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class FakeStore:
            def create_quick(self, title: str, day: date, **options) -> None:
                captured.update(title=title, day=day, **options)

        class FakeEntry:
            def focus_set(self) -> None:
                captured["focused"] = True

        fake = type(
            "FakeCalendar",
            (),
            {
                "quick_var": FakeVar(),
                "quick_placeholder_active": False,
                "quick_color": "#E65D67",
                "quick_event_type": "ddl",
                "selected": date(2026, 8, 5),
                "store": FakeStore(),
                "quick_entry": FakeEntry(),
                "render": lambda self: captured.update(rendered=True),
            },
        )()
        self.assertEqual(CalendarApp.quick_add(fake), "break")
        self.assertEqual(captured["event_type"], "ddl")
        self.assertEqual(captured["color"], "#E65D67")
        self.assertTrue(captured["rendered"])
        self.assertTrue(captured["focused"])


if __name__ == "__main__":
    unittest.main()
