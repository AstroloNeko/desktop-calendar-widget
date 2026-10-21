import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

from calendar_core import Event, EventCategory, Store
from event_commands import EventCommandHistory
from schedule_drag import ScheduleDrag, ScheduleHit, hit_at


class ScheduleDragTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "calendar.json")
        self.store.events = [Event("event", "同名任务", "2026-08-07T10:30", duration_days=5, end_as_ddl=True)]
        self.store.save()
        self.history = EventCommandHistory(self.store)

    def gesture(self, operation="move", anchor=date(2026, 8, 9), threshold=5):
        return ScheduleDrag(self.history, "event", operation, anchor, (100, 100), threshold=threshold)

    def move(self, drag, day):
        return drag.update(day, (200, 100))

    def test_threshold_preserves_click_and_no_history(self):
        drag = self.gesture()
        self.assertIsNone(drag.update(date(2026, 8, 10), (103, 101)))
        self.assertIsNone(drag.finish())
        self.assertEqual(self.history.undo_count, 0)

    def test_move_from_middle_keeps_duration_time_and_identity(self):
        original = asdict(self.store.events[0])
        drag = self.gesture()
        preview = self.move(drag, date(2026, 8, 12))
        self.assertEqual(preview.effective_dates[0], date(2026, 8, 10))
        self.assertEqual(preview.deadline, date(2026, 8, 14))
        self.assertEqual(preview.event().due, "2026-08-10T10:30")
        self.assertEqual(asdict(self.store.events[0]), original)
        self.history.commit(drag.finish())
        self.assertEqual(self.history.undo_count, 1)
        self.history.undo()
        self.assertEqual(asdict(self.store.events[0]), original)
        self.history.redo()
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 10))

    def test_many_previews_write_nothing_and_one_release_writes_once(self):
        drag = self.gesture()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            for day in range(10, 24):
                self.move(drag, date(2026, 8, day))
            save.assert_not_called()
            self.history.commit(drag.finish())
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.history.undo_count, 1)
        self.assertIsNone(drag.finish())

    def test_no_change_does_not_clear_snooze_or_redo(self):
        self.store.events[0].snooze_until = "2026-08-08T10:30"
        self.history.update("event", title="changed")
        self.history.undo()
        drag = self.gesture()
        self.move(drag, date(2026, 8, 9))
        self.assertIsNone(drag.finish())
        self.assertEqual(self.history.redo_count, 1)
        self.assertIsNotNone(self.store.events[0].snooze_until)

    def test_cancel_restores_original_without_saving(self):
        drag = self.gesture()
        self.move(drag, date(2026, 9, 1))
        drag.cancel()
        self.assertIsNone(drag.finish())
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 7))

    def test_release_outside_invalidates_previous_preview(self):
        drag = self.gesture()
        self.move(drag, date(2026, 8, 10))
        self.move(drag, None)
        self.assertIsNone(drag.finish())

    def test_resize_end_preserves_start(self):
        drag = self.gesture("end")
        preview = self.move(drag, date(2026, 8, 15))
        self.assertEqual(preview.event().duration_days, 9)
        self.assertEqual(preview.effective_dates[0], date(2026, 8, 7))

    def test_resize_start_preserves_actual_end(self):
        drag = self.gesture("start")
        preview = self.move(drag, date(2026, 8, 5))
        self.assertEqual(preview.effective_dates[-1], date(2026, 8, 11))
        self.assertEqual(preview.event().duration_days, 7)

    def test_invalid_range_and_overlong_span_cannot_commit(self):
        for operation, day in (("end", date(2026, 8, 6)), ("start", date(2026, 8, 12)), ("end", date(2028, 8, 10))):
            with self.subTest(operation=operation, day=day):
                drag = self.gesture(operation)
                self.assertIsNone(self.move(drag, day))
                self.assertTrue(drag.error)
                self.assertIsNone(drag.finish())

    def test_skip_weekend_holiday_and_leave_use_store_dates(self):
        self.store.events[0].skip_non_working_days = True
        self.store.set_date_status(date(2026, 8, 10), "leave")
        drag = self.gesture("end")
        preview = self.move(drag, date(2026, 8, 12))
        self.assertEqual(preview.effective_dates, (date(2026, 8, 7), date(2026, 8, 11), date(2026, 8, 12)))
        self.assertEqual(preview.deadline, date(2026, 8, 12))
        drag = self.gesture("move", anchor=date(2026, 8, 7))
        preview = self.move(drag, date(2026, 10, 1))
        self.assertEqual(preview.effective_dates, self.store.event_dates(preview.event()))
        self.assertNotEqual(preview.effective_dates[0], date(2026, 10, 1))

    def test_external_edit_or_delete_rejects_drag(self):
        for deleted in (False, True):
            self.setUp() if deleted else None
            drag = self.gesture()
            if deleted:
                self.history.delete("event")
            else:
                self.history.update("event", title="external")
            self.assertIsNone(self.move(drag, date(2026, 8, 10)))
            self.assertIsNone(drag.finish())

    def test_commit_failure_keeps_data_and_history_intact(self):
        drag = self.gesture()
        self.move(drag, date(2026, 8, 10))
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.history.commit(drag.finish())
        self.assertEqual(self.history.undo_count, 0)
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 7))

    def test_completed_native_ddl_and_category_color_preserved(self):
        self.store.categories = [EventCategory("art", "绘画", "#52B788")]
        event = self.store.events[0]
        event.event_type, event.category_id, event.color_mode, event.done = "ddl", "art", "inherit", True
        drag = self.gesture()
        preview = self.move(drag, date(2026, 9, 1))
        self.assertEqual(preview.event().event_type, "ddl")
        self.assertTrue(preview.event().done)
        self.assertEqual(preview.effective_color, "#52B788")
        self.assertEqual(preview.deadline, preview.effective_dates[-1])

    def test_boundary_overflow_is_invalid(self):
        drag = self.gesture()
        self.assertIsNone(self.move(drag, date.max))
        self.assertIsNone(drag.finish())

    def test_dpi_threshold_and_real_endpoint_hits(self):
        for dpi in (96, 120, 144, 168, 192):
            scale = dpi / 96
            drag = self.gesture(threshold=5 * scale)
            self.assertIsNone(drag.update(date(2026, 8, 12), (100 + 4 * scale, 100)))
            self.assertIsNotNone(drag.update(date(2026, 8, 12), (100 + 6 * scale, 100)))
        continuation = ScheduleHit("event", (10, 10, 100, 40), False, True)
        self.assertEqual(continuation.operation_at(11, 6), "move")
        self.assertEqual(continuation.operation_at(99, 6), "end")
        self.assertIs(hit_at([continuation], 40, 30), continuation)
        self.assertIsNone(hit_at([continuation], 101, 30))
