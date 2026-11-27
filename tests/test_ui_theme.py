import unittest
from unittest.mock import Mock, patch

from desktop_preferences import ACTIVE_THEME_NAMES, ARCHIVED_THEME_FALLBACKS, DEFAULT_THEME_NAME, normalize_stored_theme_name
from desktop_calendar.ui.ui_theme import ACTIVE_THEMES, THEMES, get_theme, normalize_theme_name


class ThemeRegistryTests(unittest.TestCase):
    def test_only_modern_and_slate_use_rounded_widget_shells(self):
        self.assertEqual({t.name for t in ACTIVE_THEMES.values() if t.rounded_widget_shells}, {"modern", "slate"})

    def test_only_slate_opts_out_of_the_deadline_ring(self):
        self.assertEqual(get_theme("slate").ddl_marker_style, "dot")
        self.assertEqual(get_theme("modern").ddl_marker_style, "ring")

    def test_active_registry_leaves_archived_definitions_available(self):
        self.assertEqual(tuple(ACTIVE_THEMES), ("modern", "slate"))
        self.assertEqual(set(THEMES) - set(ACTIVE_THEMES), set(ARCHIVED_THEME_FALLBACKS))
        self.assertEqual(DEFAULT_THEME_NAME, "modern")
        self.assertEqual({name: THEMES[name].display_name for name in ARCHIVED_THEME_FALLBACKS},
                         {"aero": "Windows 7 Glass", "paper": "Paper", "frutiger": "Frutiger Aero"})

    def test_legacy_theme_aliases_and_invalid_fallback(self):
        for saved, canonical in (("aero", "aero"), ("win7_aero", "aero"),
                                 ("paper", "paper"), ("frutiger", "frutiger")):
            with self.subTest(saved=saved):
                self.assertEqual(normalize_stored_theme_name(saved), canonical)
                self.assertIs(get_theme(saved), get_theme("modern"))
        self.assertIs(get_theme("gemini_reviewing"), get_theme("slate"))
        for invalid in ("unknown", None, []):
            self.assertEqual(normalize_theme_name(invalid), DEFAULT_THEME_NAME)

    def test_main_menu_lists_and_dispatches_only_active_themes(self):
        from app import CalendarApp

        app = Mock(theme_name="modern", theme=get_theme("modern"), view_mode="compact", window_mode="desktop")
        app.store.settings = {}
        app.attributes.return_value = 1.0
        app.menu_button.winfo_rooty.return_value = 0
        app.menu_button.winfo_height.return_value = 24
        menu, theme_menu, opacity_menu, data_menu = (Mock() for _ in range(4))
        with patch("app.themed_popup_menu", side_effect=(menu, theme_menu, opacity_menu, data_menu)), \
                patch("app.tk.StringVar"), patch("app.tk.DoubleVar"), patch("app.tk.BooleanVar"), \
                patch("app.is_autostart_enabled", return_value=False):
            CalendarApp.show_main_menu(app)
        options = [entry.kwargs for entry in theme_menu.add_radiobutton.call_args_list]
        self.assertEqual([(entry["value"], entry["label"]) for entry in options],
                         [("modern", "Modern"), ("slate", "Slate")])
        for entry in options:
            entry["command"]()
            app.set_theme.assert_called_with(entry["value"])

    def test_theme_selection_persists_each_active_choice(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app import CalendarApp
        from calendar_core import Store
        from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository

        with TemporaryDirectory() as directory:
            path = Path(directory) / "theme.sqlite3"
            store = Store(path, repository_factory=SqliteCalendarRepository)
            try:
                store.save_preferences({"unrelated": "preserved"})
                app = Mock(theme_name="modern", quick_placeholder_active=False, store=store)
                app._save_preferences = lambda updates: CalendarApp._save_preferences(app, updates)
                with patch("app.activate_theme") as activate:
                    for name in reversed(ACTIVE_THEME_NAMES):
                        CalendarApp.set_theme(app, name)
                        self.assertEqual(app.theme_name, name)
                        activate.assert_called_with(ACTIVE_THEMES[name])
                        reopened = Store(path, repository_factory=SqliteCalendarRepository)
                        try:
                            self.assertEqual(reopened.settings["theme"], name)
                            self.assertEqual(reopened.settings["unrelated"], "preserved")
                        finally:
                            reopened.repository.close()
            finally:
                store.repository.close()


if __name__ == "__main__":
    unittest.main()
