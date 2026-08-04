import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from calendar_core import COLORS, Event, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = Path(self.temp_dir.name) / "calendar.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bad_event_does_not_hide_good_events(self):
        self.data_file.write_text(
            json.dumps(
                {
                    "events": [
                        {"id": "good", "title": "有效日程", "due": "2026-08-04T10:00"},
                        {"id": "bad", "title": "损坏日程", "due": "not-a-date"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = Store(self.data_file)
        self.assertEqual([event.id for event in store.events], ["good"])

    def test_old_topmost_setting_migrates_to_desktop_mode(self):
        self.data_file.write_text(json.dumps({"events": [], "settings": {"topmost": True}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["window_mode"], "desktop")

    def test_new_install_defaults_to_crisp_opacity(self):
        store = Store(self.data_file)
        self.assertEqual(store.settings["opacity"], 1.0)

    def test_legacy_hex_color_is_preserved(self):
        event = Event("legacy", "旧版颜色", "2026-08-04T10:00", color="#F05252")
        self.assertEqual(event.color, "#F05252")

    def test_non_schedule_edit_keeps_notification_history(self):
        store = Store(self.data_file)
        original = Event("one", "旧标题", "2026-08-04T10:00")
        store.upsert(original)
        store.notified.add("one:reminder:2026-08-04T09:00")
        edited = Event("one", "新标题", "2026-08-04T10:00", color=COLORS["薄荷绿"])
        store.upsert(edited)
        self.assertIn("one:reminder:2026-08-04T09:00", store.notified)

    def test_schedule_edit_clears_notification_history(self):
        store = Store(self.data_file)
        store.upsert(Event("one", "事项", "2026-08-04T10:00"))
        store.notified.add("one:reminder:2026-08-04T09:00")
        store.upsert(Event("one", "事项", "2026-08-04T11:00"))
        self.assertFalse(any(key.startswith("one:") for key in store.notified))

    def test_quick_event_is_scheduled_in_the_future_for_today(self):
        store = Store(self.data_file)
        before = datetime.now()
        event = store.create_quick("快速事项", date.today())
        self.assertGreater(event.due_at, before)

    def test_events_on_places_unfinished_before_finished(self):
        store = Store(self.data_file)
        day = date(2026, 8, 4)
        finished = Event("done", "已完成", "2026-08-04T08:00", done=True)
        open_event = Event("open", "未完成", "2026-08-04T18:00")
        store.events = [finished, open_event]
        self.assertEqual([event.id for event in store.events_on(day)], ["open", "done"])

    def test_upcoming_includes_overdue_and_next_week(self):
        store = Store(self.data_file)
        now = datetime.now().replace(second=0, microsecond=0)
        store.events = [
            Event("late", "逾期", (now - timedelta(hours=1)).isoformat(timespec="minutes")),
            Event("soon", "即将到期", (now + timedelta(days=2)).isoformat(timespec="minutes")),
            Event("later", "以后", (now + timedelta(days=10)).isoformat(timespec="minutes")),
        ]
        self.assertEqual([event.id for event in store.upcoming(7)], ["late", "soon"])


if __name__ == "__main__":
    unittest.main()
