import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

from calendar_core import Event, EventCategory, RoutineItem, Store
from event_commands import CommandConflictError, EventCommandHistory
from timeline_model import build_month_timeline
from calendar_flow_layout import build_calendar_flow_layout


class EventCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "calendar.json"
        self.store = Store(self.path)
        self.store.save()
        self.history = EventCommandHistory(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, identifier="task", **fields):
        return Event(identifier, "事项", "2026-08-07T18:00", **fields)

    def seed(self, event):
        self.store.upsert(event)
        self.history.clear()
        return self.store.event_by_id(event.id)

    def assert_persisted(self):
        loaded = Store(self.path)
        self.assertEqual([asdict(e) for e in loaded.events], [asdict(e) for e in self.store.events])
        self.assertEqual(loaded.notified, self.store.notified)

    def test_create_undo_redo_restores_id_and_all_fields(self):
        event = self.event(event_type="urgent", duration_days=5, end_as_ddl=True, notes="备注", has_time=False)
        self.history.upsert(event)
        original = asdict(self.store.events[0])
        self.history.undo()
        self.assertEqual(self.store.events, [])
        self.assert_persisted()
        self.history.redo()
        self.assertEqual(asdict(self.store.events[0]), original)
        self.assert_persisted()

    def test_delete_undo_restores_identity_order_category_color_and_deadline(self):
        self.store.upsert_category(EventCategory("art", "绘画", "#52B788"))
        event = self.store.prepare_event(self.event(event_type="ddl", category_id="art", color_mode="inherit", duration_days=3))
        self.store.events = [self.event("before"), event, self.event("after")]
        self.store.save()
        original = asdict(event)
        self.history.delete(event.id)
        self.history.undo()
        self.assertEqual([e.id for e in self.store.events], ["before", "task", "after"])
        self.assertEqual(asdict(self.store.event_by_id("task")), original)
        self.assert_persisted()

    def test_complete_and_cancel_complete_are_reversible(self):
        self.seed(self.event(event_type="ddl"))
        self.history.update("task", done=True, snooze_until=None)
        self.assertFalse(self.store.has_ddl_on(date(2026, 8, 7)))
        self.history.update("task", done=False)
        self.history.undo()
        self.assertTrue(self.store.event_by_id("task").done)
        self.history.undo()
        self.assertTrue(self.store.has_ddl_on(date(2026, 8, 7)))
        self.history.redo()
        self.assertTrue(self.store.event_by_id("task").done)

    def test_category_and_color_override_restore(self):
        self.store.upsert_category(EventCategory("art", "绘画", "#52B788"))
        self.seed(self.event(color="#6687F2"))
        self.history.update("task", category_id="art", color_mode="inherit")
        self.history.update("task", color="#E65D67", color_mode="override")
        self.history.undo()
        event = self.store.event_by_id("task")
        self.assertEqual((event.category_id, event.color_mode, self.store.effective_event_color(event)), ("art", "inherit", "#52B788"))
        self.history.undo()
        event = self.store.event_by_id("task")
        self.assertEqual((event.category_id, event.color_mode, event.color), (None, "override", "#6687F2"))
        self.assert_persisted()

    def test_multiple_undo_redo_and_new_branch(self):
        for identifier in ("a", "b", "c"):
            self.history.upsert(self.event(identifier))
        for _ in range(3):
            self.history.undo()
        self.assertEqual(self.store.events, [])
        for _ in range(3):
            self.history.redo()
        self.assertEqual([e.id for e in self.store.events], ["a", "b", "c"])
        self.history.undo()
        self.history.upsert(self.event("d"))
        self.assertFalse(self.history.can_redo)
        self.assertEqual([e.id for e in self.store.events], ["a", "b", "d"])

    def test_at_least_fifty_steps_and_empty_history_are_safe(self):
        self.seed(self.event())
        self.history = EventCommandHistory(self.store, limit=1)
        for number in range(55):
            self.history.update("task", title=str(number))
        self.assertEqual(self.history.undo_count, 50)
        for _ in range(50):
            self.history.undo()
        self.assertEqual(self.store.event_by_id("task").title, "4")
        self.assertIsNone(self.history.undo())

    def test_noop_does_not_write_or_discard_redo(self):
        self.seed(self.event())
        self.history.update("task", title="新版")
        self.history.undo()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            self.assertIsNone(self.history.upsert(self.store.event_by_id("task")))
            save.assert_not_called()
        self.assertTrue(self.history.can_redo)

    def test_commit_undo_redo_each_write_once(self):
        self.seed(self.event())
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            self.history.update("task", notes="修改")
            self.assertEqual(save.call_count, 1)
            self.history.undo()
            self.assertEqual(save.call_count, 2)
            self.history.redo()
            self.assertEqual(save.call_count, 3)

    def test_failed_commit_restores_memory_file_notifications_and_history(self):
        self.seed(self.event(snooze_until="2026-08-07T19:00"))
        self.store.notified = {"task:reminder:old", "other:reminder:keep"}
        self.store.save()
        before, original = self.path.read_bytes(), asdict(self.store.events[0])
        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.history.update("task", due="2026-08-10T18:00")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(asdict(self.store.events[0]), original)
        self.assertEqual(self.store.notified, {"task:reminder:old", "other:reminder:keep"})
        self.assertEqual(self.history.undo_count, 0)
        self.assert_persisted()

    def test_failed_undo_and_redo_leave_stacks_and_data_unchanged(self):
        self.seed(self.event())
        self.history.update("task", title="修改")
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.history.undo()
        self.assertEqual((self.history.undo_count, self.history.redo_count), (1, 0))
        self.assertEqual(self.path.read_bytes(), before)
        self.history.undo()
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.history.redo()
        self.assertEqual((self.history.undo_count, self.history.redo_count), (0, 1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_persisted()

    def test_preview_is_detached_and_one_release_is_one_write(self):
        event = self.seed(self.event(duration_days=3, event_type="urgent", end_as_ddl=True))
        original = asdict(event)
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            for day in range(1, 25):
                preview = self.history.preview_schedule("task", start_date=date(2026, 9, day))
            preview.event().title = "不能修改预览的历史状态"
            self.assertEqual(asdict(event), original)
            self.assertEqual(self.history.undo_count, 0)
            save.assert_not_called()
            self.history.commit(preview)
            save.assert_called_once()
        self.assertEqual(self.history.undo_count, 1)
        self.assertEqual(self.store.event_by_id("task").title, original["title"])

    def test_skip_weekend_leave_and_end_as_ddl_use_existing_rules(self):
        self.store.set_date_status(date(2026, 8, 10), "leave")
        self.seed(self.event(duration_days=2, skip_non_working_days=True, event_type="urgent", end_as_ddl=True))
        preview = self.history.preview_schedule("task", start_date=date(2026, 8, 7))
        self.assertEqual(preview.effective_dates, (date(2026, 8, 7), date(2026, 8, 11)))
        self.assertEqual(preview.deadline, date(2026, 8, 11))
        self.assertEqual(preview.event().event_type, "urgent")

    def test_resize_resolves_holiday_and_adjusted_workdays(self):
        self.seed(self.event(skip_non_working_days=True))
        preview = self.history.preview_schedule("task", start_date=date(2026, 9, 30), end_date=date(2026, 10, 10))
        self.assertEqual(preview.effective_dates, (date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)))
        self.assertEqual(preview.event().duration_days, 4)

    def test_resize_endpoint_in_skipped_day_uses_last_actual_day(self):
        self.seed(self.event(skip_non_working_days=True))
        preview = self.history.preview_schedule("task", start_date=date(2026, 8, 6), end_date=date(2026, 8, 9))
        self.assertEqual(preview.effective_dates, (date(2026, 8, 6), date(2026, 8, 7)))

    def test_move_from_weekend_starts_on_next_effective_day(self):
        self.seed(self.event(skip_non_working_days=True, event_type="ddl", duration_days=2))
        preview = self.history.preview_schedule("task", start_date=date(2026, 8, 8))
        self.assertEqual(preview.effective_dates, (date(2026, 8, 10), date(2026, 8, 11)))
        self.assertEqual(preview.deadline, date(2026, 8, 11))
        self.history.commit(preview)
        self.history.undo()
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 7))

    def test_resize_to_one_day_and_undo_restores_endpoint_deadline(self):
        self.seed(self.event(duration_days=4, end_as_ddl=True))
        preview = self.history.preview_schedule("task", duration_days=1)
        self.assertIsNone(preview.deadline)
        self.history.commit(preview)
        self.assertFalse(self.store.events[0].end_as_ddl)
        self.history.undo()
        self.assertEqual(self.store.event_end_date(self.store.events[0]), date(2026, 8, 10))
        self.assertTrue(self.store.events[0].end_as_ddl)

    def test_native_ddl_completed_and_inherited_color_survive_move(self):
        self.store.upsert_category(EventCategory("art", "绘画", "#52B788"))
        self.seed(self.event(event_type="ddl", done=True, duration_days=3, category_id="art", color_mode="inherit"))
        preview = self.history.preview_schedule("task", start_date=date(2026, 9, 29))
        self.assertEqual((preview.deadline, preview.effective_color), (date(2026, 10, 1), "#52B788"))
        self.history.commit(preview)
        self.assertTrue(self.store.events[0].done)
        self.assertEqual(self.store.ddl_events(), [])
        self.history.undo()
        self.assertEqual(self.store.event_end_date(self.store.events[0]), date(2026, 8, 9))

    def test_stale_preview_cannot_overwrite_later_edit(self):
        self.seed(self.event())
        preview = self.history.preview_update("task", title="预览")
        self.history.update("task", title="新操作")
        with self.assertRaises(CommandConflictError):
            self.history.commit(preview)
        self.assertEqual(self.store.events[0].title, "新操作")
        self.assertEqual(self.history.undo_count, 1)

    def test_workday_change_invalidates_schedule_preview(self):
        self.seed(self.event(skip_non_working_days=True, duration_days=2))
        preview = self.history.preview_schedule("task", start_date=date(2026, 8, 7))
        self.store.set_date_status(date(2026, 8, 10), "holiday")
        with self.assertRaises(CommandConflictError):
            self.history.commit(preview)

    def test_invalid_edit_does_not_mutate_or_write(self):
        self.seed(self.event())
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            for duration in (0, 366, 1.5, True):
                with self.subTest(duration=duration), self.assertRaises(ValueError):
                    self.history.preview_schedule("task", duration_days=duration)
            with self.assertRaises(ValueError):
                self.history.update("task", title="")
            with self.assertRaises(ValueError):
                self.history.preview_update("task", id="changed")
            with self.assertRaises(ValueError):
                self.history.preview_schedule("task", start_date=date(2026, 9, 2), end_date=date(2026, 9, 1))
            save.assert_not_called()

    def test_notifications_restore_without_touching_other_events(self):
        self.seed(self.event(snooze_until="2026-08-07T19:00"))
        self.store.notified = {"task:reminder:old", "other:reminder:keep"}
        self.history.update("task", due="2026-08-10T18:00")
        self.assertIsNone(self.store.events[0].snooze_until)
        self.assertEqual(self.store.notified, {"other:reminder:keep"})
        self.history.undo()
        self.assertEqual(self.store.events[0].snooze_until, "2026-08-07T19:00")
        self.assertEqual(self.store.notified, {"task:reminder:old", "other:reminder:keep"})
        self.assert_persisted()

    def test_title_undo_preserves_reminders_delivered_after_the_command(self):
        self.seed(self.event())
        self.history.update("task", title="修改标题")
        self.store.notified.add("task:reminder:delivered")
        self.store.save()
        self.history.undo()
        self.assertIn("task:reminder:delivered", self.store.notified)
        self.history.redo()
        self.assertIn("task:reminder:delivered", self.store.notified)
        self.assert_persisted()

    def test_unrelated_user_data_is_not_reverted_and_restart_has_no_history(self):
        self.history.upsert(self.event())
        self.store.settings["theme"] = "paper"
        self.store.set_date_status(date(2026, 8, 12), "leave")
        self.store.upsert_routine(RoutineItem("habit", "习惯"))
        self.history.undo()
        loaded = Store(self.path)
        self.assertEqual(loaded.settings["theme"], "paper")
        self.assertEqual(loaded.date_status(date(2026, 8, 12)), "leave")
        self.assertEqual([r.id for r in loaded.routines], ["habit"])
        fresh_history = EventCommandHistory(loaded)
        self.assertFalse(fresh_history.can_undo or fresh_history.can_redo)
        self.assertNotIn("history", json.loads(self.path.read_text(encoding="utf-8")))

    def test_untracked_mutation_does_not_get_silently_overwritten(self):
        self.history.upsert(self.event())
        self.store.events[0].title = "外部变动"
        with self.assertRaises(CommandConflictError):
            self.history.undo()
        self.assertEqual(self.store.events[0].title, "外部变动")

    def test_both_global_models_observe_same_move_and_undo(self):
        self.seed(self.event(duration_days=2, end_as_ddl=True))
        preview = self.history.preview_schedule("task", start_date=date(2026, 8, 31))
        self.history.commit(preview)
        model = build_month_timeline(self.store, 2026, 9)
        self.assertEqual(model.items[0].ddl_date, date(2026, 9, 1))
        self.assertTrue(build_calendar_flow_layout(model).block_by_item_id("task"))
        self.history.undo()
        self.assertEqual(build_month_timeline(self.store, 2026, 9).items, ())


if __name__ == "__main__":
    unittest.main()
