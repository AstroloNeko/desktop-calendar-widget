import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from app import CalendarApp
from calendar_core import Event, Store
from event_commands import EventCommandHistory


class EventCommandUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "calendar.json")
        self.history = EventCommandHistory(self.store)
        self.ui = SimpleNamespace(
            store=self.store, event_commands=self.history,
            selected=date(2026, 8, 7), shown_year=2026, shown_month=8,
            view_mode="compact", editor_window=None, category_editor=None, routine_editor=None,
            day_detail_window=None, ddl_list_window=None,
            day_cells=[], theme=SimpleNamespace(event_done="#aaaaaa"),
            _refresh_event_change=Mock(), render=Mock(), render_agenda=Mock(),
            _render_global_timeline=Mock(),
        )
        self.ui._run_event_command = MethodType(CalendarApp._run_event_command, self.ui)
        self.ui.commit_event_preview = MethodType(CalendarApp.commit_event_preview, self.ui)
        self.ui._active_modal_window = lambda: self.ui.editor_window or self.ui.day_detail_window

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, **fields):
        event = Event("task", "事项", "2026-08-07T18:00", **fields)
        self.store.upsert(event)
        return event

    def test_all_item_mutation_entry_points_use_reversible_commands(self):
        event = self.seed()
        CalendarApp.upsert_event(self.ui, replace(event, title="编辑"))
        CalendarApp.toggle_done(self.ui, event)
        self.assertTrue(self.store.events[0].done)
        # A stale card reference still resolves current state by stable ID.
        CalendarApp.toggle_done(self.ui, event)
        self.assertFalse(self.store.events[0].done)
        CalendarApp.defer_to_tomorrow(self.ui, event)
        self.assertEqual(self.store.events[0].due_date, date(2026, 8, 8))
        CalendarApp.delete_event(self.ui, event.id)
        self.assertEqual(self.history.undo_count, 5)
        self.history.undo()
        self.assertEqual(self.store.events[0].id, event.id)
        self.assertEqual(self.ui._refresh_event_change.call_count, 5)

    def test_notification_actions_use_the_same_history(self):
        event = self.seed(event_type="ddl")
        popup = SimpleNamespace(destroy=Mock())
        CalendarApp.snooze_event(self.ui, event, popup)
        self.assertIsNotNone(self.store.events[0].snooze_until)
        CalendarApp.complete_from_notification(self.ui, event, popup)
        self.assertTrue(self.store.events[0].done)
        CalendarApp.undo_event(self.ui)
        self.assertFalse(self.store.events[0].done)
        self.assertIsNotNone(self.store.events[0].snooze_until)
        self.assertEqual((self.history.undo_count, self.history.redo_count), (1, 1))

    def test_text_widgets_do_not_trigger_global_history(self):
        self.ui.undo_event, self.ui.redo_event = Mock(), Mock()
        for widget_class in ("Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"):
            for redo in (False, True):
                event = SimpleNamespace(widget=SimpleNamespace(winfo_class=lambda: widget_class))
                self.assertIsNone(CalendarApp._history_shortcut(self.ui, event, redo=redo))
        self.ui.undo_event.assert_not_called()
        self.ui.redo_event.assert_not_called()

    def test_nontext_shortcuts_dispatch_and_editor_blocks_background_mutations(self):
        self.ui.undo_event, self.ui.redo_event = Mock(), Mock()
        event = SimpleNamespace(widget=SimpleNamespace(winfo_class=lambda: "Canvas"))
        self.assertEqual(CalendarApp._history_shortcut(self.ui, event, redo=False), "break")
        self.assertEqual(CalendarApp._history_shortcut(self.ui, event, redo=True), "break")
        self.ui.undo_event.assert_called_once()
        self.ui.redo_event.assert_called_once()
        self.ui.editor_window = SimpleNamespace(winfo_exists=lambda: True)
        CalendarApp._history_shortcut(self.ui, event, redo=False)
        self.ui.undo_event.assert_called_once()

    def test_shortcuts_are_registered_for_root_and_detail_windows(self):
        ui = Mock()
        CalendarApp._bind_shortcuts(ui)
        sequences = {call.args[0] for call in ui.bind_all.call_args_list}
        self.assertTrue({"<Control-z>", "<Control-y>", "<Control-Shift-Z>"} <= sequences)

    def test_failed_save_uses_modal_owner_without_refresh_or_history(self):
        event = self.seed()
        owner = SimpleNamespace(winfo_exists=lambda: True)
        self.ui.editor_window = owner
        before = self.store.data_file.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk full")), \
             patch("app.owned_messagebox") as message:
            self.assertFalse(CalendarApp.upsert_event(self.ui, replace(event, notes="修改")))
        self.assertIs(message.call_args.args[0], owner)
        self.ui._refresh_event_change.assert_not_called()
        self.assertEqual(self.history.undo_count, 0)
        self.assertEqual(self.store.data_file.read_bytes(), before)

    def test_compact_change_only_redraws_affected_cells_and_sections(self):
        event = self.seed()
        change = self.history.update(event.id, done=True)
        affected = SimpleNamespace(day=date(2026, 8, 7), draw=Mock())
        unrelated = SimpleNamespace(day=date(2026, 8, 9), draw=Mock())
        self.ui.day_cells = [affected, unrelated]
        self.ui.day_detail_window = SimpleNamespace(winfo_exists=lambda: True, day=date(2026, 8, 9), refresh=Mock())
        CalendarApp._refresh_event_change(self.ui, change)
        affected.draw.assert_called_once()
        unrelated.draw.assert_not_called()
        self.ui.render.assert_not_called()
        self.ui.render_agenda.assert_called_once_with(refresh_items=True, refresh_ddl=False)
        self.ui.day_detail_window.refresh.assert_not_called()
        self.assertEqual(self.history.undo_count, 1)

    def test_failed_detail_operation_owns_error_dialog_and_retains_grab_owner(self):
        event = self.seed()
        detail = SimpleNamespace(winfo_exists=lambda: True)
        self.ui.day_detail_window = detail
        with patch.object(Path, "replace", side_effect=OSError("disk full")), \
             patch("app.owned_messagebox") as message:
            self.assertFalse(CalendarApp.toggle_done(self.ui, event))
        self.assertIs(message.call_args.args[0], detail)
        self.assertFalse(self.store.events[0].done)
        self.assertEqual(self.history.undo_count, 0)

    def test_ddl_change_refreshes_open_views_without_extra_history(self):
        event = self.seed(event_type="ddl")
        change = self.history.update(event.id, done=True)
        self.ui.day_detail_window = SimpleNamespace(winfo_exists=lambda: True, day=date(2026, 8, 7), refresh=Mock())
        self.ui.ddl_list_window = SimpleNamespace(winfo_exists=lambda: True, refresh=Mock())
        CalendarApp._refresh_event_change(self.ui, change)
        self.ui.ddl_list_window.refresh.assert_called_once()
        self.ui.day_detail_window.refresh.assert_called_once()
        self.ui.render_agenda.assert_called_once_with(refresh_items=True, refresh_ddl=True)
        self.assertEqual(self.history.undo_count, 1)

    def test_unrelated_date_update_keeps_daily_list(self):
        event = self.seed()
        change = self.history.update(event.id, notes="改备注")
        self.ui.selected = date(2026, 8, 20)
        CalendarApp._refresh_event_change(self.ui, change)
        self.ui.render_agenda.assert_called_once_with(refresh_items=False, refresh_ddl=False)

    def test_global_refresh_keeps_sidebar_and_ignores_other_months(self):
        event = self.seed()
        change = self.history.update(event.id, notes="改备注")
        self.ui.view_mode = "global"
        CalendarApp._refresh_event_change(self.ui, change)
        self.ui._render_global_timeline.assert_called_once_with(refresh_sidebar=False)
        self.ui._render_global_timeline.reset_mock()
        self.ui.shown_month = 9
        CalendarApp._refresh_event_change(self.ui, change)
        self.ui._render_global_timeline.assert_not_called()

    def test_category_catalog_edit_clears_history_but_does_not_save_twice(self):
        self.history.upsert(Event("task", "事项", "2026-08-07T18:00"))
        self.ui.category_manager = None
        self.ui._prune_global_category_filter = Mock()
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            CalendarApp.category_data_changed(self.ui)
            save.assert_not_called()
        self.assertFalse(self.history.can_undo or self.history.can_redo)


if __name__ == "__main__":
    unittest.main()
