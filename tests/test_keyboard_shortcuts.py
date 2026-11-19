from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from calendar_core import Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from global_hotkey import GLOBAL_DEFAULTS, GlobalHotkeys, saved_global_shortcuts
from keyboard_shortcuts import LOCAL_SETTING, parse_local, saved_local_shortcuts, save_shortcuts, validate_shortcuts
from test_tray_lifecycle import FakeApp


class KeyboardShortcutTests(TestCase):
    def test_old_global_preference_is_preserved_even_if_it_uses_ai_default(self):
        self.assertEqual(saved_global_shortcuts({"global_view_hotkey": "alt+ctrl+j"}),
                         {"global": "Ctrl+Alt+J", "assistant": ""})
        self.assertEqual(saved_global_shortcuts({}), {"global": "Ctrl+Alt+D", "assistant": "Ctrl+Alt+J"})

    def test_local_aliases_and_duplicate_validation(self):
        self.assertEqual(parse_local(" ctrl + shift + k , pgup "),
                         {"Ctrl+Shift+K": "<Control-Shift-KeyPress-K>", "PgUp": "<KeyPress-Prior>"})
        with self.assertRaisesRegex(ValueError, "同时用于"):
            validate_shortcuts(GLOBAL_DEFAULTS, {"search": GLOBAL_DEFAULTS["assistant"]})
        with self.assertRaises(ValueError):
            parse_local("Ctrl+V", editor=True)
        self.assertEqual(saved_local_shortcuts({LOCAL_SETTING: {"search": "", "today": 10}})["search"], "")
        self.assertEqual(saved_local_shortcuts({LOCAL_SETTING: {"today": 10}})["today"], "Ctrl+T, Home, T")

    def test_local_only_preferences_saved_with_tray_unavailable(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.json"
            app = SimpleNamespace(store=Store(path), tray_icon=None, _apply_calendar_shortcuts=Mock())
            save_shortcuts(app, GLOBAL_DEFAULTS, {"search": "Ctrl+Shift+K"})
            app._apply_calendar_shortcuts.assert_called_once()
            self.assertEqual(saved_local_shortcuts(Store(path).settings)["search"], "Ctrl+Shift+K")

    def test_sqlite_remembers_global_and_local_changes_on_reopen(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.sqlite3"
            first = Store(path, repository_factory=SqliteCalendarRepository)
            app = SimpleNamespace(store=first, tray_icon=Mock(is_available=True, hotkey_error=None),
                                  _apply_calendar_shortcuts=Mock())
            try:
                save_shortcuts(app, {**GLOBAL_DEFAULTS, "assistant": "Alt+F8"}, {"search": "Ctrl+Shift+K"})
            finally:
                first.repository.close()
            second = Store(path, repository_factory=SqliteCalendarRepository)
            try:
                self.assertEqual(saved_global_shortcuts(second.settings)["assistant"], "Alt+F8")
                self.assertEqual(saved_local_shortcuts(second.settings)["search"], "Ctrl+Shift+K")
            finally:
                second.repository.close()

    def test_failed_save_rolls_back_both_global_and_local_preferences(self):
        store = Mock(settings={"global_view_hotkey": "Ctrl+Alt+D"})
        store.save.side_effect = OSError("disk full")
        tray = Mock(is_available=True, hotkey_error=None)
        app = SimpleNamespace(store=store, tray_icon=tray, _apply_calendar_shortcuts=Mock())
        with self.assertRaises(OSError):
            save_shortcuts(app, {**GLOBAL_DEFAULTS, "assistant": "Alt+F8"}, {"search": ""})
        self.assertEqual(store.settings, {"global_view_hotkey": "Ctrl+Alt+D"})
        self.assertEqual([call.args[0] for call in tray.configure_hotkey.call_args_list], ["prepare", "rollback"])
        app._apply_calendar_shortcuts.assert_not_called()

    def test_assistant_wakes_current_view_and_respects_modal(self):
        app = FakeApp()
        app._open_assistant = Mock()
        with patch("app_lifecycle.raise_for_interaction"):
            self.assertTrue(app.hide_to_tray())
            app._handle_tray_action("assistant")
            self.assertFalse(app.lifecycle.hidden)
            app.enter_global_view.assert_not_called()
            app._open_assistant.assert_called_once()
            editor = Mock()
            app._active_modal_window.return_value = editor
            app._handle_tray_action("assistant")
            editor.focus_set.assert_called_once()
            app._open_assistant.assert_called_once()


class NativeRegistrationTests(TestCase):
    def setUp(self):
        self.user = Mock()
        self.native = GlobalHotkeys(self.user, 42)
        self.assertIsNone(self.native.start(GLOBAL_DEFAULTS))

    def test_swap_keeps_os_reservations_and_dispatches_after_commit(self):
        old_id, spec = self.native.active["global"]
        packed = (spec.key << 16) | spec.modifiers
        self.native.prepare({"global": GLOBAL_DEFAULTS["assistant"], "assistant": GLOBAL_DEFAULTS["global"]})
        self.assertEqual(self.native.action(old_id, packed), "global")
        self.native.commit()
        self.assertEqual(self.native.action(old_id, packed), "assistant")
        self.assertEqual(self.user.RegisterHotKey.call_count, 2)
        self.user.UnregisterHotKey.assert_not_called()

    def test_second_registration_failure_releases_first_candidate_only(self):
        old = self.native.active.copy()
        self.user.RegisterHotKey.side_effect = [True, False]
        with self.assertRaisesRegex(ValueError, "唤起 AI 助理"):
            self.native.prepare({"global": "Alt+F8", "assistant": "Alt+F9"})
        self.assertEqual(self.native.active, old)
        self.assertIsNone(self.native.pending)
        removed = self.user.UnregisterHotKey.call_args.args[1]
        self.assertNotIn(removed, {entry[0] for entry in old.values()})

    def test_startup_failure_of_first_action_does_not_disable_the_other(self):
        self.native.close()
        self.user.RegisterHotKey.side_effect = [False, True]
        self.assertIn("打开全局视图", self.native.start(GLOBAL_DEFAULTS))
        self.assertEqual(set(self.native.active), {"assistant"})
