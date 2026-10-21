import inspect
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from app import CalendarApp
from event_search import SearchResult
from global_search_ui import GlobalSearchMixin, SearchDialog


class GlobalSearchUITests(unittest.TestCase):
    def test_repeated_shortcut_reuses_existing_window(self):
        window = Mock()
        fake = SimpleNamespace(search_window=window)
        self.assertEqual(GlobalSearchMixin.open_search(fake), "break")
        window.present.assert_called_once()

    def test_editor_modal_is_not_stolen_by_search(self):
        owner = Mock()
        fake = SimpleNamespace(search_window=None, _active_modal_window=lambda: owner)
        GlobalSearchMixin.open_search(fake)
        owner.focus_set.assert_called_once()

    def test_palette_owns_global_undo_redo_while_open(self):
        fake = SimpleNamespace(search_window=Mock())
        event = SimpleNamespace(widget=Mock())
        self.assertEqual(CalendarApp._history_shortcut(fake, event, redo=False), "break")
        self.assertEqual(CalendarApp._history_shortcut(fake, event, redo=True), "break")
        event.widget.winfo_class.assert_not_called()

    def test_commands_dispatch_existing_application_methods(self):
        methods = ("open_new_event", "go_today", "open_ddl_list", "open_category_manager", "enter_global_view", "set_global_display_mode", "select_day")
        fake = SimpleNamespace(**{method: Mock() for method in methods})
        for key, method in (("new", "open_new_event"), ("today", "go_today"), ("ddl", "open_ddl_list"), ("categories", "open_category_manager")):
            GlobalSearchMixin._activate_search_result(fake, SearchResult("command", key, "", ""))
            getattr(fake, method).assert_called_once()
        for key in ("flow", "timeline"):
            GlobalSearchMixin._activate_search_result(fake, SearchResult("command", key, "", ""))
            fake.set_global_display_mode.assert_called_with(key)
        target = date(2027, 2, 5)
        GlobalSearchMixin._activate_search_result(fake, SearchResult("date", "goto", "", "", target_date=target))
        fake.select_day.assert_called_once_with(target)

    def test_result_opens_existing_editor_after_navigating_and_temporarily_reveals(self):
        event = object()
        fake = SimpleNamespace(
            store=SimpleNamespace(event_by_id=lambda _id: event, event_start_date=lambda _event: date(2027, 2, 5)),
            view_mode="global", select_day=Mock(), _render_global_timeline=Mock(),
            _scroll_search_target_into_view=Mock(), open_editor=Mock(),
            _global_category_filter_ids=set(), _global_include_uncategorized=False,
        )
        event = SimpleNamespace(id="target")
        GlobalSearchMixin._activate_search_result(fake, SearchResult("event", "target", "", ""))
        fake.select_day.assert_called_once_with(date(2027, 2, 5))
        self.assertEqual(fake._search_reveal_event_id, "target")
        self.assertEqual(fake._global_category_filter_ids, set())
        self.assertFalse(fake._global_include_uncategorized)
        fake._render_global_timeline.assert_called_once_with(refresh_sidebar=False, select_event_id="target")
        fake.open_editor.assert_called_once_with(event)

    def test_deleted_search_result_is_not_opened(self):
        fake = SimpleNamespace(store=SimpleNamespace(event_by_id=lambda _: None), _invalidate_search=Mock())
        GlobalSearchMixin._activate_search_result(fake, SearchResult("event", "missing", "", ""))
        fake._invalidate_search.assert_called_once()

    def test_index_invalidation_updates_visible_palette(self):
        fake = SimpleNamespace(search_service=Mock(), search_window=Mock())
        GlobalSearchMixin._invalidate_search(fake)
        fake.search_service.invalidate.assert_called_once()
        fake.search_window.request.assert_called_once()

    def test_search_does_not_generate_event_commands_or_change_saved_filter(self):
        source = inspect.getsource(SearchDialog) + inspect.getsource(GlobalSearchMixin)
        self.assertNotIn("event_commands.", source)
        self.assertNotIn("store.save", source)
        self.assertNotIn("_global_category_filter_ids =", source)

    def test_search_visuals_use_theme_and_worker_never_touches_tk(self):
        import event_search
        self.assertNotRegex(inspect.getsource(SearchDialog), r"#[0-9a-fA-F]{6}")
        self.assertNotIn("import tkinter", inspect.getsource(event_search))

    def test_mutations_invalidate_search_not_theme_or_render(self):
        self.assertIn("_invalidate_search", inspect.getsource(CalendarApp._refresh_event_change))
        self.assertIn("_invalidate_search", inspect.getsource(CalendarApp.category_data_changed))
        self.assertNotIn("_invalidate_search", inspect.getsource(CalendarApp.render))
