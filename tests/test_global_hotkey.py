import ctypes
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from calendar_core import Store
from global_hotkey import (DEFAULT_HOTKEY, SETTING_KEY, HotkeyBinding,
                           parse_shortcut, saved_shortcut, save_shortcut)
from tray_icon import TrayIcon, WindowsTrayAPI
from test_tray_lifecycle import FakeApp


class ShortcutTests(unittest.TestCase):
    def test_default_and_old_settings(self):
        self.assertEqual(saved_shortcut({}), DEFAULT_HOTKEY)
        self.assertEqual(saved_shortcut({SETTING_KEY: 123}), DEFAULT_HOTKEY)
        self.assertEqual(saved_shortcut({SETTING_KEY: 'bad'}), DEFAULT_HOTKEY)
        self.assertEqual(saved_shortcut({SETTING_KEY: ''}), '')

    def test_normalized_modifier_order_letters_digits_function_keys(self):
        for source, expected, mods, key in [(' alt + ctrl + d ', 'Ctrl+Alt+D', 3, 68),
                                          ('ctrl+shift+9', 'Ctrl+Shift+9', 6, 57),
                                          ('Alt+F24', 'Alt+F24', 1, 135)]:
            value = parse_shortcut(source)
            self.assertEqual((value.text, value.modifiers, value.key), (expected, mods, key))

    def test_unsafe_invalid_or_duplicate_combinations_rejected(self):
        for value in ('D', 'Shift+D', 'Win+D', 'Ctrl+Ctrl+D', 'Ctrl++D', 'Alt+F4',
                      'Ctrl+Alt+F4', 'Ctrl+F25', 'Ctrl+中', 'Ctrl+Alt', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_shortcut(value)

    def test_blank_explicitly_disables(self):
        self.assertIsNone(parse_shortcut('  '))


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.user = Mock()
        self.user.RegisterHotKey.return_value = True
        self.binding = HotkeyBinding(self.user, 2**44 + 123)
        self.binding.prepare(DEFAULT_HOTKEY)
        self.binding.commit()
        self.original = self.binding.active

    def test_registration_uses_pointer_wide_hwnd_and_no_repeat(self):
        self.user.RegisterHotKey.assert_called_once_with(2**44 + 123, self.original[0], 0x4003, 68)
        api = WindowsTrayAPI()
        self.assertEqual(ctypes.sizeof(api.user.RegisterHotKey.argtypes[0]), ctypes.sizeof(ctypes.c_void_p))

    def test_conflict_preserves_old_binding(self):
        self.user.RegisterHotKey.return_value = False
        with self.assertRaises(ValueError):
            self.binding.prepare('Ctrl+Alt+E')
        self.assertEqual(self.binding.active, self.original)
        self.user.UnregisterHotKey.assert_not_called()

    def test_prepare_reserves_old_until_commit(self):
        self.binding.prepare('Ctrl+Alt+E')
        self.user.UnregisterHotKey.assert_not_called()
        self.assertEqual(self.binding.active, self.original)
        self.binding.commit()
        self.user.UnregisterHotKey.assert_called_once_with(self.binding.hwnd, self.original[0])
        self.assertEqual(self.binding.active[1].text, 'Ctrl+Alt+E')

    def test_failed_save_can_release_only_candidate(self):
        self.binding.prepare('Ctrl+Alt+E')
        candidate = self.binding.pending
        self.binding.rollback()
        self.user.UnregisterHotKey.assert_called_once_with(self.binding.hwnd, candidate[0])
        self.assertEqual(self.binding.active, self.original)

    def test_unchanged_configuration_does_not_register_twice(self):
        self.binding.prepare(DEFAULT_HOTKEY)
        self.binding.commit()
        self.assertEqual(self.user.RegisterHotKey.call_count, 1)
        self.user.UnregisterHotKey.assert_not_called()

    def test_disable_and_exit_release_registration(self):
        self.binding.prepare('')
        self.binding.commit()
        self.assertIsNone(self.binding.active)
        self.binding.close()
        self.user.UnregisterHotKey.assert_called_once()

    def test_exit_releases_active_and_pending_once(self):
        self.binding.prepare('Alt+F8')
        self.binding.close()
        self.binding.close()
        self.assertEqual(self.user.UnregisterHotKey.call_count, 2)

    def test_stale_or_wrong_fingerprint_is_ignored(self):
        self.assertTrue(self.binding.matches(self.original[0], (68 << 16) | 3))
        self.assertFalse(self.binding.matches(self.original[0], (69 << 16) | 3))
        self.assertFalse(self.binding.matches(999, (68 << 16) | 3))


class PersistenceTests(unittest.TestCase):
    def app(self, folder):
        return SimpleNamespace(store=Store(Path(folder) / 'data.json'),
                               tray_icon=SimpleNamespace(is_available=True, configure_hotkey=Mock()))

    def test_save_restart_and_backup_includes_shortcut_without_schema_change(self):
        import json
        from data_backup import create_backup, read_backup, restore_store
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(folder)
            save_shortcut(app, 'alt+ctrl+e')
            loaded = Store(app.store.data_file)
            self.assertEqual(saved_shortcut(loaded.settings), 'Ctrl+Alt+E')
            self.assertEqual([call.args[0] for call in app.tray_icon.configure_hotkey.call_args_list], ['prepare', 'commit'])
            self.assertEqual(loaded.events, [])
            backup = create_backup(loaded.data_file.read_bytes(), Path(folder) / 'shortcut.dcalbackup')
            manifest, data = read_backup(backup)
            self.assertEqual(json.loads(data)['settings'][SETTING_KEY], 'Ctrl+Alt+E')
            loaded.settings[SETTING_KEY] = ''
            loaded.save()
            restore_store(loaded, backup)
            self.assertEqual(saved_shortcut(loaded.settings), 'Ctrl+Alt+E')

    def test_storage_failure_keeps_original_and_rolls_back_reservation(self):
        for previous in (None, 'Alt+F8'):
            with tempfile.TemporaryDirectory() as folder:
                app = self.app(folder)
                if previous:
                    app.store.settings[SETTING_KEY] = previous
                app.store.save = Mock(side_effect=OSError('disk unavailable'))
                with self.assertRaises(OSError):
                    save_shortcut(app, 'Ctrl+Alt+E')
                self.assertEqual(app.store.settings.get(SETTING_KEY), previous)
                if previous is None:
                    self.assertNotIn(SETTING_KEY, app.store.settings)
                self.assertEqual([c.args[0] for c in app.tray_icon.configure_hotkey.call_args_list], ['prepare', 'rollback'])

    def test_conflict_or_unavailable_service_never_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(folder)
            app.store.save = Mock()
            app.tray_icon.configure_hotkey.side_effect = ValueError('occupied')
            with self.assertRaises(ValueError):
                save_shortcut(app, 'Ctrl+Alt+E')
            self.assertNotIn(SETTING_KEY, app.store.settings)
            app.tray_icon.is_available = False
            with self.assertRaises(ValueError):
                save_shortcut(app, '')
            app.store.save.assert_not_called()

    def test_service_loss_after_save_does_not_claim_active_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.app(folder)
            app.tray_icon.configure_hotkey.side_effect = [None, ValueError('stopped')]
            with self.assertRaisesRegex(ValueError, '已保存.*重新启动'):
                save_shortcut(app, 'Alt+F8')
            self.assertEqual(saved_shortcut(Store(app.store.data_file).settings), 'Alt+F8')


class MessageTests(unittest.TestCase):
    def tray(self):
        tray = TrayIcon('QA', Path('missing.ico'), Mock())
        tray._api = SimpleNamespace(user=Mock())
        tray._hotkey_binding = HotkeyBinding(tray._api.user, 5)
        tray._hotkey_binding.prepare(DEFAULT_HOTKEY)
        tray._hotkey_binding.commit()
        return tray

    def test_native_message_only_enqueues_global_command_not_tk(self):
        tray = self.tray()
        identifier = tray._hotkey_binding.active[0]
        tray._handle_message(5, tray.WM_HOTKEY, identifier, (68 << 16) | 3)
        tray.on_action.assert_called_once_with('global')
        tray._handle_message(5, tray.WM_HOTKEY, 123, (68 << 16) | 3)
        self.assertEqual(tray.on_action.call_count, 1)
        tray._stopping.set()
        tray._handle_message(5, tray.WM_HOTKEY, identifier, (68 << 16) | 3)
        self.assertEqual(tray.on_action.call_count, 1)

    def test_cancelled_rpc_cannot_apply_late(self):
        tray = self.tray()
        request = dict(lock=threading.Lock(), cancelled=True, operation='prepare', value='Alt+F8')
        tray._hotkey_requests.put(request)
        tray._configure_hotkey_requests()
        self.assertIsNone(tray._hotkey_binding.pending)

    def test_rpc_failure_is_reported_and_finishes(self):
        tray = self.tray()
        tray._api.user.RegisterHotKey.return_value = False
        request = dict(lock=threading.Lock(), cancelled=False, operation='prepare', value='Alt+F8', done=threading.Event(), error=None)
        tray._hotkey_requests.put(request)
        tray._handle_message(5, tray.HOTKEY_CONFIG, 0, 0)
        self.assertTrue(request['done'].is_set())
        self.assertIn('占用', request['error'])

    def test_rpc_timeout_cancels_request_before_late_delivery(self):
        tray = self.tray()
        tray._available.set()
        tray._hwnd = 5
        tray._thread = Mock()
        with patch.object(threading.Event, 'wait', return_value=False):
            with self.assertRaisesRegex(ValueError, '超时'):
                tray.configure_hotkey('prepare', 'Alt+F8')
        tray._configure_hotkey_requests()
        self.assertIsNone(tray._hotkey_binding.pending)

    def test_rpc_post_failure_cancels_request(self):
        tray = self.tray()
        tray._available.set()
        tray._hwnd = 5
        tray._thread = Mock()
        tray._api.user.PostMessageW.return_value = False
        with self.assertRaisesRegex(ValueError, '提交'):
            tray.configure_hotkey('prepare', 'Alt+F8')
        tray._configure_hotkey_requests()
        self.assertIsNone(tray._hotkey_binding.pending)

    def test_repeated_hotkey_restores_global_and_not_toggle_hidden(self):
        app = FakeApp()
        with patch('app_lifecycle.raise_for_interaction'):
            app.hide_to_tray()
            for _ in range(20):
                app._handle_tray_action('global')
            self.assertFalse(app.lifecycle.hidden)
            self.assertEqual(app.view_mode, 'global')
            app.enter_global_view.assert_called_once()

    def test_modal_is_focused_without_switching_or_stealing_grab(self):
        app = FakeApp()
        editor = Mock()
        app._active_modal_window.return_value = editor
        with patch('app_lifecycle.raise_for_interaction'):
            app._handle_tray_action('global')
        editor.focus_set.assert_called_once()
        app.enter_global_view.assert_not_called()
        app.grab_current.assert_not_called()

    def test_exit_ignores_pending_hotkey(self):
        app = FakeApp()
        app.lifecycle.exiting = True
        app._handle_tray_action('global')
        app.enter_global_view.assert_not_called()
