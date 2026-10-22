from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import tempfile
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from calendar_core import Event, EventCategory, RoutineItem, Store
from data_backup import create_backup, restore_store
from event_commands import BatchPreview, CommandConflictError, EventCommandHistory
from event_selection import EventSelection
from bulk_ui import BulkActionsMixin
from timeline_model import TimelineSelection, build_month_timeline


class EventSelectionTests(unittest.TestCase):
    def setUp(self):
        self.selection = EventSelection()
        self.order = ("a", "b", "c", "d")
        self.selection.reconcile(self.order, "context")

    def test_ctrl_toggle_and_plain_collapse(self):
        self.selection.single("a", self.order)
        self.selection.toggle("b", self.order)
        self.selection.toggle("c", self.order)
        self.assertEqual(self.selection.ids, {"a", "b", "c"})
        self.selection.toggle("b", self.order)
        self.assertEqual(self.selection.ids, {"a", "c"})
        self.selection.single("d", self.order)
        self.assertEqual(self.selection.ids, {"d"})

    def test_segments_are_deduplicated_and_unknown_is_ignored(self):
        self.selection.single("a", ("a", "a", "b"))
        self.selection.toggle("unknown", self.order)
        self.assertEqual(self.selection.ids, {"a"})

    def test_timeline_range_uses_display_order_in_both_directions(self):
        self.selection.single("c", self.order)
        self.selection.range_to("a", self.order)
        self.assertEqual(self.selection.ids, {"a", "b", "c"})
        self.selection.range_to("d", self.order, additive=True)
        self.assertEqual(self.selection.ids, set(self.order))

    def test_range_missing_anchor_starts_at_target(self):
        self.selection.range_to("b", self.order)
        self.assertEqual(self.selection.ids, {"b"})

    def test_repaint_keeps_visible_ids_and_removes_deleted(self):
        self.selection.single("a", self.order)
        self.selection.toggle("b", self.order)
        self.selection.reconcile(self.order, "context")
        self.assertEqual(self.selection.ids, {"a", "b"})
        self.selection.reconcile(("b", "c"), "context")
        self.assertEqual(self.selection.ids, {"b"})

    def test_month_filter_view_context_changes_clear_all(self):
        for context in ("month", "filter", "view", "exit-global"):
            self.selection.single("a", self.order)
            self.selection.reconcile(self.order, context)
            self.assertFalse(self.selection.ids)
            self.assertIsNone(self.selection.anchor)


class BulkCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "calendar.json"
        self.store = Store(self.path)
        self.store.categories = [EventCategory("art", "绘画", "#52B788"), EventCategory("work", "工作", "#6687F2")]
        self.store.events = [Event("a", "同名", "2026-08-07T10:30", event_type="general", category_id="art", color_mode="inherit"),
                             Event("b", "同名", "2026-08-08T18:00", event_type="urgent", color="#E65D67", color_mode="override", done=True),
                             Event("c", "DDL", "2026-08-10T08:00", event_type="ddl", has_time=False)]
        self.store.events = [self.store.prepare_event(e) for e in self.store.events]
        self.store.save()
        self.history = EventCommandHistory(self.store)

    def fields(self):
        return [asdict(e) for e in self.store.events]

    def assert_persisted(self):
        loaded = Store(self.path)
        self.assertEqual([asdict(e) for e in loaded.events], self.fields())
        self.assertEqual(loaded.notified, self.store.notified)

    def test_mixed_complete_undo_redo_is_one_history_one_save(self):
        before = self.fields()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            change = self.history.bulk(("a", "b", "c"), "complete", done=True)
            self.assertTrue(all(e.done for e in self.store.events))
            self.assertEqual(self.history.undo_count, 1)
            self.assertTrue(change.ddl_changed)
            self.assertEqual(save.call_count, 1)
            self.history.undo()
            self.assertEqual(self.fields(), before)
            self.assertEqual(save.call_count, 2)
            self.history.redo()
            self.assertTrue(all(e.done for e in self.store.events))
            self.assertEqual(save.call_count, 3)
        self.assert_persisted()

    def test_explicit_uncomplete_not_toggle(self):
        self.history.bulk(("a", "b", "c"), "complete", done=False)
        self.assertTrue(all(not e.done for e in self.store.events))
        self.history.undo()
        self.assertTrue(self.store.event_by_id("b").done)
        self.assertFalse(self.store.event_by_id("a").done)

    def test_category_preserves_override_and_inherit_and_each_original(self):
        before = self.fields()
        self.history.bulk(("a", "b", "c"), "category", category_id="work")
        a, b, _c = self.store.events
        self.assertEqual((a.category_id, a.color_mode, self.store.effective_event_color(a)), ("work", "inherit", "#6687F2"))
        self.assertEqual((b.category_id, b.color_mode, b.color), ("work", "override", "#E65D67"))
        self.history.undo()
        self.assertEqual(self.fields(), before)
        self.history.redo()
        self.assertTrue(all(e.category_id == "work" for e in self.store.events))
        self.assert_persisted()

    def test_remove_category_freezes_inherited_color_and_undo_restores(self):
        self.history.bulk(("a", "b"), "category", category_id=None)
        a = self.store.event_by_id("a")
        self.assertEqual((a.category_id, a.color_mode, a.color), (None, "override", "#52B788"))
        self.history.undo()
        self.assertEqual(self.store.event_by_id("a").category_id, "art")
        self.assertEqual(self.store.event_by_id("a").color_mode, "inherit")

    def test_unknown_category_and_missing_event_fail_before_save(self):
        before = self.fields()
        with patch.object(self.store, "save") as save:
            with self.assertRaises(ValueError):
                self.history.bulk(("a", "b"), "category", category_id="gone")
            with self.assertRaises(CommandConflictError):
                self.history.bulk(("a", "gone"), "delete")
            save.assert_not_called()
        self.assertEqual(self.fields(), before)
        self.assertEqual(self.history.undo_count, 0)

    def test_shift_positive_negative_keeps_intervals_times_and_duration(self):
        self.store.events[1].duration_days = 3
        before = self.fields()
        self.history.bulk(("a", "b", "c"), "shift", days=3)
        self.assertEqual([e.due_date for e in self.store.events], [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 13)])
        self.assertEqual(self.store.events[1].duration_days, 3)
        self.assertEqual(self.store.events[1].due_at.hour, 18)
        self.history.undo()
        self.assertEqual(self.fields(), before)
        self.history.redo()
        self.history.bulk(("a", "b", "c"), "shift", days=-3)
        self.assertEqual(self.fields(), before)
        self.assert_persisted()

    def test_shift_uses_actual_workdays_leave_holiday_and_deadline(self):
        self.store.set_date_status(date(2026, 8, 10), "leave")
        self.store.events[0] = replace(self.store.events[0], due="2026-08-06T10:30", duration_days=2,
                                       skip_non_working_days=True, end_as_ddl=True)
        self.store.events[2] = replace(self.store.events[2], due="2026-09-29T08:00", duration_days=4, skip_non_working_days=True)
        preview = self.history.preview_bulk(("a", "c"), "shift", days=1)
        a, c = preview.entries
        self.assertEqual(a.effective_dates, (date(2026, 8, 7), date(2026, 8, 11)))
        self.assertEqual(a.deadline, date(2026, 8, 11))
        self.assertEqual(c.effective_dates, (date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)))
        self.assertEqual(c.deadline, date(2026, 10, 10))
        self.history.commit_bulk(preview)
        self.assertEqual(self.store.event_by_id("a").event_type, "general")
        self.assertEqual(self.store.event_by_id("c").event_type, "ddl")
        self.history.undo()
        self.assertEqual(self.store.event_by_id("a").due_date, date(2026, 8, 6))

    def test_zero_shift_is_noop_and_keeps_reminder(self):
        self.store.events[0].snooze_until = "2026-08-07T11:00"
        with patch.object(self.store, "save") as save:
            self.assertIsNone(self.history.bulk(("a", "b"), "shift", days=0))
            save.assert_not_called()
        self.assertEqual(self.store.events[0].snooze_until, "2026-08-07T11:00")

    def test_date_overflow_is_all_or_nothing(self):
        self.store.events[2].due = "9999-12-31T08:00"
        before = self.fields()
        with patch.object(self.store, "save") as save, self.assertRaises(OverflowError):
            self.history.bulk(("a", "c"), "shift", days=1)
        self.assertEqual(self.fields(), before)
        self.assertEqual(self.history.undo_count, 0)
        save.assert_not_called()

    def test_duplicate_ids_are_one_change(self):
        change = self.history.bulk(("a", "a", "c", "a"), "delete")
        self.assertEqual(len(change.changes), 2)
        self.history.undo()
        self.assertEqual([e.id for e in self.store.events], ["a", "b", "c"])

    def test_delete_undo_restores_full_fields_order_and_notifications(self):
        self.store.events[0] = replace(self.store.events[0], duration_days=5, end_as_ddl=True,
                                       skip_non_working_days=True, notes="保留", snooze_until="2026-08-07T11:00")
        self.store.events.insert(2, Event("unrelated", "未选", "2026-08-09T00:00"))
        self.store.notified = {"a:delivered", "c:delivered", "unrelated:keep"}
        self.store.save()
        before = self.fields()
        self.history.bulk(("c", "a"), "delete")
        self.assertEqual([e.id for e in self.store.events], ["b", "unrelated"])
        self.assertEqual(self.store.notified, {"unrelated:keep"})
        self.history.undo()
        self.assertEqual(self.fields(), before)
        self.assertEqual(self.store.notified, {"a:delivered", "c:delivered", "unrelated:keep"})
        self.history.redo()
        self.assertEqual([e.id for e in self.store.events], ["b", "unrelated"])
        self.assert_persisted()

    def test_stale_preview_blocks_whole_group(self):
        preview = self.history.preview_bulk(("a", "c"), "complete")
        self.store.events[2].notes = "外部修改"
        with patch.object(self.store, "save") as save, self.assertRaises(CommandConflictError):
            self.history.commit_bulk(preview)
        self.assertFalse(self.store.events[0].done)
        save.assert_not_called()

    def test_changed_workday_or_category_blocks_whole_preview(self):
        self.store.events[0].skip_non_working_days = True
        self.store.events[0].duration_days = 2
        preview = self.history.preview_bulk(("a", "c"), "shift", days=1)
        self.store.set_date_status(date(2026, 8, 10), "leave")
        with self.assertRaises(CommandConflictError):
            self.history.commit_bulk(preview)
        preview = self.history.preview_bulk(("a", "c"), "category", category_id="work")
        self.store.categories = [c for c in self.store.categories if c.id != "work"]
        with self.assertRaises(CommandConflictError):
            self.history.commit_bulk(preview)

    def test_failed_save_rolls_back_memory_disk_and_both_stacks(self):
        for operation, options in (("complete", {}), ("delete", {}), ("shift", {"days": 2}), ("category", {"category_id": "work"})):
            with self.subTest(operation=operation):
                before, disk = self.fields(), self.path.read_bytes()
                with patch.object(Path, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                    self.history.bulk(("a", "b", "c"), operation, **options)
                self.assertEqual(self.fields(), before)
                self.assertEqual(self.path.read_bytes(), disk)
                self.assertEqual((self.history.undo_count, self.history.redo_count), (0, 0))

    def test_failed_undo_and_redo_leave_data_and_history(self):
        self.history.bulk(("a", "c"), "delete")
        for action in (self.history.undo, self.history.redo):
            if action == self.history.redo:
                self.history.undo()
            before, disk = self.fields(), self.path.read_bytes()
            counts = self.history.undo_count, self.history.redo_count
            with patch.object(Path, "replace", side_effect=OSError("full")), self.assertRaises(OSError):
                action()
            self.assertEqual(self.fields(), before)
            self.assertEqual(self.path.read_bytes(), disk)
            self.assertEqual((self.history.undo_count, self.history.redo_count), counts)

    def test_undo_conflict_cannot_restore_only_half(self):
        self.history.bulk(("a", "c"), "complete")
        self.store.events[2].notes = "外部修改"
        with self.assertRaises(CommandConflictError):
            self.history.undo()
        self.assertTrue(self.store.events[0].done)
        self.assertEqual(self.history.undo_count, 1)

    def test_new_operation_discards_redo_but_noop_does_not(self):
        self.history.bulk(("a", "c"), "complete")
        self.history.undo()
        self.assertIsNone(self.history.bulk(("a", "c"), "complete", done=False))
        self.assertTrue(self.history.can_redo)
        self.history.bulk(("a", "c"), "shift", days=1)
        self.assertFalse(self.history.can_redo)

    def test_history_retains_at_least_fifty_batches(self):
        self.history = EventCommandHistory(self.store, limit=1)
        for _ in range(53):
            self.history.bulk(("a", "b"), "shift", days=1)
        self.assertEqual(self.history.undo_count, 50)
        for _ in range(50):
            self.history.undo()
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 10))

    def test_invalid_batch_payloads_fail_without_writes(self):
        with patch.object(self.store, "save") as save:
            for ids, operation, options in (((), "delete", {}), (("a",), "unknown", {}), (("a",), "shift", {"days": 1.5})):
                with self.assertRaises(ValueError):
                    self.history.bulk(ids, operation, **options)
            entry = self.history.preview_bulk(("a",), "delete").entries[0]
            with self.assertRaises(ValueError):
                self.history.commit_bulk(BatchPreview((entry, entry), "重复"))
            save.assert_not_called()

    def test_transaction_preparation_failure_cannot_publish_partial_memory(self):
        before = self.fields()
        with patch.object(self.store, "save") as save:
            with self.assertRaises(TypeError):
                self.store.persist_event_states([("a", None, None, None)])
            self.store.persist_event_states([])
            save.assert_not_called()
        self.assertEqual(self.fields(), before)

    def test_habit2_backup_restore_restart_and_no_new_schema(self):
        item = RoutineItem("habit", "习惯", frequency="weekly", weekdays=[0, 2, 4], interval=2,
                           start_on="2026-08-03", habit_done=["2026-08-03"], workdays_only=False)
        self.store.routines = [item]
        self.history.bulk(("a", "c"), "shift", days=1)
        backup = Path(self.temporary.name) / "test.dcalbackup"
        create_backup(self.path.read_bytes(), backup)
        expected = self.fields()
        self.history.bulk(("a", "c"), "delete")
        restore_store(self.store, backup)
        self.assertEqual(self.fields(), expected)
        self.assertEqual(asdict(self.store.routines[0]), asdict(item))
        self.assert_persisted()
        loaded = Store(self.path)
        self.assertEqual(loaded.routines[0].habit_done, ["2026-08-03"])
        self.assertFalse(EventCommandHistory(loaded).can_undo)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("selection", payload)
        self.assertNotIn("history", payload)

    def workspace(self):
        app = BulkActionsMixin()
        app.store = self.store
        app.view_mode, app.global_display_mode = "global", "timeline"
        app.shown_year, app.shown_month = 2026, 8
        app._global_category_filter_ids, app._global_include_uncategorized = None, True
        app._search_reveal_event_id = None
        app.timeline_selection = TimelineSelection()
        app.bulk_selection = EventSelection()
        app.timeline_model = build_month_timeline(self.store, 2026, 8)
        app.bulk_selection.reconcile(app._bulk_visible_order(), app._bulk_context())
        app.bulk_selection.ids = {"a", "c"}
        app.clear_bulk_selection = Mock(side_effect=app.bulk_selection.clear)
        return app

    def test_bulk_action_revalidates_deleted_and_changed_filter(self):
        app = self.workspace()
        self.assertEqual(set(app._bulk_ids_for_action()), {"a", "c"})
        self.store.events = [e for e in self.store.events if e.id != "c"]
        with self.assertRaises(ValueError):
            app._bulk_ids_for_action()
        self.assertFalse(app.bulk_selection.ids)
        app = self.workspace()
        app._global_category_filter_ids = {"art"}
        with self.assertRaises(ValueError):
            app._bulk_ids_for_action()
        self.assertFalse(app.bulk_selection.ids)

    def test_bulk_action_refuses_silent_partial_selection(self):
        app = self.workspace()
        app.bulk_selection.ids.add("not-visible")
        with self.assertRaises(ValueError):
            app._bulk_ids_for_action()
        app = self.workspace()
        with self.assertRaises(ValueError):
            app._bulk_ids_for_action(expected=("a", "b"))

    def test_category_undo_refuses_deleted_category_instead_of_partial_restore(self):
        self.history.bulk(("a", "c"), "category", category_id="work")
        before = self.fields()
        self.store.categories = [c for c in self.store.categories if c.id != "art"]
        with self.assertRaises(CommandConflictError):
            self.history.undo()
        self.assertEqual(self.fields(), before)

    def test_later_notifications_preserved_by_unrelated_batch_undo(self):
        self.history.bulk(("a", "c"), "category", category_id="work")
        self.store.notified.update(("a:later", "c:later", "unselected:keep"))
        self.store.save()
        self.history.undo()
        self.assertEqual(self.store.notified, {"a:later", "c:later", "unselected:keep"})
        self.assert_persisted()

    def test_fifty_batch_undo_does_not_change_unrelated_settings_or_habits(self):
        self.store.routines = [RoutineItem("habit", "保留", habit_done=["2026-08-03"])]
        self.history.bulk(("a", "c"), "delete")
        self.store.settings["theme"] = "paper"
        self.store.save()
        self.history.undo()
        self.assertEqual(self.store.settings["theme"], "paper")
        self.assertEqual(self.store.routines[0].habit_done, ["2026-08-03"])

    def test_500_events_bulk_10_50_100_performance_and_write_counts(self):
        self.store.events = [Event(str(i), f"事项{i}", f"2026-08-{i%28+1:02}T10:00", duration_days=i%3+1,
                                   event_type="ddl" if i%4==0 else "general") for i in range(500)]
        self.store.save()
        for count in (10, 50, 100):
            ids = [str(i) for i in range(count)]
            start = perf_counter()
            with patch.object(self.store, "save", wraps=self.store.save) as save:
                self.history.bulk(ids, "shift", days=1)
                self.history.undo()
                self.history.redo()
                self.assertEqual(save.call_count, 3)
            print(f"500 events / {count} selected: shift+undo+redo {perf_counter()-start:.3f}s")
            self.assertEqual(len(self.store.events), 500)
            self.history.undo()


class BulkUiTests(unittest.TestCase):
    def fake(self):
        app = BulkActionsMixin()
        app.bulk_selection = EventSelection()
        app._bulk_ids_for_action = Mock(return_value=("a", "b"))
        app._bulk_message = Mock(return_value=True)
        app._execute_bulk = Mock(return_value=True)
        return app

    def test_delete_requires_two_confirmations_and_revalidation(self):
        app = self.fake()
        self.assertTrue(app.bulk_delete())
        self.assertEqual(app._bulk_message.call_count, 2)
        self.assertEqual(app._bulk_ids_for_action.call_count, 2)
        app._execute_bulk.assert_called_once_with("delete", expected=("a", "b"))

    def test_cancel_either_confirmation_never_executes(self):
        for answers in ((False,), (True, False)):
            app = self.fake()
            app._bulk_message.side_effect = answers
            self.assertFalse(app.bulk_delete())
            app._execute_bulk.assert_not_called()
            self.assertEqual(app._bulk_message.call_count, len(answers))

    def test_deleted_or_hidden_selection_during_confirmation_aborts(self):
        app = self.fake()
        app._bulk_ids_for_action.side_effect = [("a", "b"), ValueError("已不可见")]
        self.assertFalse(app.bulk_delete())
        app._execute_bulk.assert_not_called()

    def test_modifiers_consume_selection_never_start_drag_or_mutate(self):
        app = BulkActionsMixin()
        app.global_display_mode = "timeline"
        app.timeline_model = SimpleNamespace(items=tuple(SimpleNamespace(id=i) for i in ("a", "b", "c")),
            item_by_id=lambda identifier: SimpleNamespace(id=identifier))
        app.timeline_selection = TimelineSelection()
        app.bulk_selection = EventSelection()
        app._sync_bulk_selection = Mock()
        app._cancel_flow_drag = Mock()
        app._update_global_detail = Mock()
        app._refresh_bulk_visuals = Mock()
        event = SimpleNamespace(state=4, widget=Mock())
        self.assertTrue(app._bulk_press(event, "a"))
        self.assertTrue(app._bulk_press(event, "b"))
        self.assertEqual(app.bulk_selection.ids, {"a", "b"})
        self.assertTrue(app._bulk_press(SimpleNamespace(state=1, widget=Mock()), "c"))
        self.assertEqual(app.bulk_selection.ids, {"b", "c"})
        self.assertFalse(app._bulk_press(SimpleNamespace(state=0), "a"))
        self.assertEqual(app.bulk_selection.ids, {"a"})

    def test_modifier_double_click_does_not_open_editor(self):
        app = self.fake()
        app.select_timeline_item = Mock()
        app.open_timeline_item = Mock()
        app._bulk_press = Mock()
        app.global_display_mode = "timeline"
        for state in (1, 4, 5):
            self.assertEqual(app._bulk_open_item(SimpleNamespace(state=state), "a"), "break")
        app.open_timeline_item.assert_not_called()
        self.assertEqual(app._bulk_press.call_count, 3)
        app.global_display_mode = "flow"
        app._bulk_open_item(SimpleNamespace(state=4), "a")
        self.assertEqual(app._bulk_press.call_count, 3)

    def test_flow_hidden_overflow_is_not_bulk_visible(self):
        app = self.fake()
        app.timeline_model = SimpleNamespace(items=tuple(SimpleNamespace(id=i) for i in ("a", "b", "c")))
        app.global_display_mode = "flow"
        app._schedule_hits = [SimpleNamespace(event_id="a"), SimpleNamespace(event_id="a"), SimpleNamespace(event_id="b")]
        self.assertEqual(app._bulk_visible_order(), ("a", "b"))

    def test_esc_prioritizes_modal_and_drag_before_multi_selection(self):
        from app import CalendarApp
        app = SimpleNamespace(_active_modal_window=Mock(return_value=object()),
            _schedule_drag=object(), _cancel_schedule_drag=Mock(),
            bulk_selection=SimpleNamespace(ids={"a", "b"}), clear_bulk_selection=Mock())
        CalendarApp._handle_escape(app)
        app._cancel_schedule_drag.assert_not_called()
        app.clear_bulk_selection.assert_not_called()
        app._active_modal_window.return_value = None
        CalendarApp._handle_escape(app)
        app._cancel_schedule_drag.assert_called_once()
        app.clear_bulk_selection.assert_not_called()
        app._schedule_drag = None
        CalendarApp._handle_escape(app)
        app.clear_bulk_selection.assert_called_once()

    def test_execute_one_command_one_refresh_and_selection_clear(self):
        app = self.fake()
        app.event_commands = Mock()
        app.clear_bulk_selection = Mock()
        app._run_event_command = lambda callback: bool(callback())
        self.assertTrue(BulkActionsMixin._execute_bulk(app, "complete", done=True))
        app.event_commands.bulk.assert_called_once_with(("a", "b"), "complete", done=True)
        app.clear_bulk_selection.assert_called_once()

    def test_failed_command_keeps_selection_for_retry(self):
        app = self.fake()
        app._run_event_command = Mock(return_value=False)
        app.clear_bulk_selection = Mock()
        self.assertFalse(BulkActionsMixin._execute_bulk(app, "delete"))
        app.clear_bulk_selection.assert_not_called()

    def test_message_owner_is_active_date_dialog_not_hidden_root(self):
        app = self.fake()
        owner = object()
        app._active_modal_window = Mock(return_value=owner)
        app._run_owned_bulk_modal = lambda _owner, callback: callback()
        with patch("bulk_ui.messagebox.showerror") as message:
            BulkActionsMixin._bulk_message(app, "错误")
            self.assertIs(message.call_args.kwargs["parent"], owner)


if __name__ == "__main__":
    unittest.main()
