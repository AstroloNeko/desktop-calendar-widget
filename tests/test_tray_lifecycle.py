import ctypes
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import CalendarApp
from app_lifecycle import ApplicationLifecycleMixin, LifecycleState, drain_commands
from data_compat import DataCompatibilityError
from tray_icon import TrayIcon, WindowsTrayAPI, NOTIFYICONDATAW
from view_mode import WindowGeometry


class FakeApp(ApplicationLifecycleMixin):
    def __init__(self):
        self._init_lifecycle()
        self._state = "normal"
        self.view_mode = "compact"
        self.tray_icon = SimpleNamespace(is_available=True, stop=Mock(return_value=True))
        self.tray_icon.probe_available = lambda: self.tray_icon.is_available
        self.tray_actions = queue.Queue()
        self.update_busy = False
        self.update_dialog = None
        self.desktop_session_active = True
        self._active_modal_window = Mock(return_value=None)
        self._active_overlays = Mock(return_value=[])
        self.grab_current = Mock(return_value=None)
        self._global_window_move = None
        self._global_normal_geometry = WindowGeometry(1000, 700, -800, 20)
        self._capture_window_geometry = Mock(return_value=WindowGeometry(372, 548, -500, 30))
        self._apply_saved_geometry = Mock()
        self._cancel_schedule_drag = Mock()
        self._clear_feedback = Mock()
        self._save_window_settings = Mock()
        self._start_tray_icon = Mock()
        self._activate_desktop_session = Mock()
        self.apply_window_mode = Mock()
        self.enter_global_view = Mock(side_effect=lambda: setattr(self, "view_mode", "global"))
        self._set_initial_geometry = Mock()
        self.search_window = None
        self.search_service = Mock()
        self.instance_guard = Mock()
        self._cancel_lifecycle_callbacks = Mock()
        self._owned_data_dialog = lambda action: action()
        self.destroy = Mock()
        self.after = Mock(return_value="job")
        self.after_cancel = Mock()
        self.lift = Mock()
        self.focus_set = Mock()
        self.report_callback_exception = Mock()

    def state(self, value=None):
        if value:
            self._state = value
        return self._state

    def withdraw(self):
        self._state = "withdrawn"

    def deiconify(self):
        self._state = "normal"

    def winfo_exists(self):
        return True


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.foreground = patch("app_lifecycle.raise_for_interaction").start()
        self.addCleanup(patch.stopall)

    def test_hide_does_not_save_destroy_or_change_history(self):
        app = FakeApp()
        history = app.event_commands = object()
        self.assertTrue(app.hide_to_tray())
        self.assertTrue(app.lifecycle.hidden)
        self.assertEqual(app.state(), "withdrawn")
        self.assertFalse(app.desktop_session_active)
        self.assertIs(app.event_commands, history)
        app._save_window_settings.assert_not_called()
        app.destroy.assert_not_called()

    def test_hidden_compact_global_restore_same_geometry_and_view(self):
        for view in ("compact", "global"):
            app = FakeApp()
            app.view_mode = view
            app.hide_to_tray()
            geometry = app.lifecycle.snapshot
            self.assertTrue(app.restore_from_tray())
            self.assertEqual(app.view_mode, view)
            self.assertFalse(app.lifecycle.hidden)
            self.assertEqual(app.state(), "normal")
            self.assertEqual(app.lifecycle.snapshot, geometry)
            app._apply_saved_geometry.assert_not_called()  # Tk kept the same HWND/geometry.
            app.enter_global_view.assert_not_called()

    def test_twenty_hide_show_cycles_do_not_write_or_rebuild(self):
        app = FakeApp()
        for _ in range(20):
            self.assertTrue(app.hide_to_tray())
            self.assertTrue(app.restore_from_tray())
        app._save_window_settings.assert_not_called()
        app.enter_global_view.assert_not_called()

    def test_explicit_global_switch(self):
        app = FakeApp()
        app.hide_to_tray()
        app._handle_tray_action("global")
        self.assertEqual(app.view_mode, "global")

    def test_restore_preserves_intentional_global_maximize_only(self):
        app = FakeApp()
        app.view_mode, app._state = "global", "zoomed"
        app.hide_to_tray()
        self.assertEqual(app.lifecycle.snapshot, app._global_normal_geometry)
        app.restore_from_tray()
        self.assertEqual(app.state(), "zoomed")

    def test_unavailable_tray_never_hides_only_window(self):
        app = FakeApp()
        app.tray_icon.is_available = False
        with patch("app_lifecycle.messagebox.showwarning"):
            self.assertFalse(app.hide_to_tray())
        self.assertFalse(app.lifecycle.hidden)
        self.assertEqual(app.state(), "normal")

    def test_lost_tray_automatically_restores_hidden_window(self):
        app = FakeApp()
        app.hide_to_tray()
        app.tray_icon.is_available = False
        app._poll_tray_actions()
        self.assertFalse(app.lifecycle.hidden)
        self.assertEqual(app.state(), "normal")

    def test_live_thread_but_silent_shell_icon_loss_restores_at_health_probe(self):
        app = FakeApp()
        app.hide_to_tray()
        app.lifecycle.next_tray_probe = 0
        app.tray_icon.probe_available = lambda: False
        app._poll_tray_actions()
        self.assertFalse(app.lifecycle.hidden)

    def test_pre_hide_shell_probe_failure_keeps_window_visible(self):
        app = FakeApp()
        app.tray_icon.probe_available = lambda: False
        with patch("app_lifecycle.messagebox.showwarning"):
            self.assertFalse(app.hide_to_tray())
        self.assertEqual(app.state(), "normal")

    def test_modal_blocks_hide_switch_and_exit_without_destroying_grab(self):
        app = FakeApp()
        modal = Mock()
        app._active_modal_window.return_value = modal
        self.assertFalse(app.hide_to_tray())
        self.assertFalse(app.restore_from_tray(global_view=True))
        self.assertFalse(app.on_close())
        app.enter_global_view.assert_not_called()
        app.destroy.assert_not_called()
        modal.focus_set.assert_called()

    def test_native_data_dialog_and_update_block_lifecycle(self):
        for kind in ("data", "update", "grab"):
            app = FakeApp()
            if kind == "data":
                app._lifecycle_operation_depth = 1
            elif kind == "update":
                app.update_busy = True
            else:
                app.grab_current.side_effect = KeyError("native Tcl-only grab")
            self.assertFalse(app.hide_to_tray())
            self.assertFalse(app.on_close())
            app.destroy.assert_not_called()

    def test_failure_recovery_keeps_window_accessible(self):
        app = FakeApp()
        app.hide_to_tray()
        app._capture_window_geometry.return_value = WindowGeometry(100, 100, 0, 0)
        app._apply_saved_geometry.side_effect = OSError("bad geometry")
        with self.assertLogs("app_lifecycle", level="ERROR"):
            self.assertTrue(app.restore_from_tray())
        app._set_initial_geometry.assert_called_once()
        self.assertFalse(app.lifecycle.hidden)
        self.assertFalse(app.lifecycle.restoring)

    def test_close_is_idempotent_stops_tray_search_callbacks_and_instance(self):
        app = FakeApp()
        tray, search = app.tray_icon, app.search_service
        app._ui_actions.put(Mock())
        self.assertTrue(app.on_close())
        self.assertFalse(app.on_close())
        app._save_window_settings.assert_called_once()
        tray.stop.assert_called_once()
        search.close.assert_called_once()
        app._cancel_lifecycle_callbacks.assert_called_once()
        app.instance_guard.close.assert_called_once()
        app.destroy.assert_called_once()
        self.assertTrue(app._ui_actions.empty())

    def test_save_failure_keeps_application_running(self):
        app = FakeApp()
        app.hide_to_tray()
        app._save_window_settings.side_effect = OSError("disk full")
        with patch("app_lifecycle.messagebox.showerror"):
            self.assertFalse(app.on_close())
        self.assertFalse(app.lifecycle.exiting)
        self.assertFalse(app.lifecycle.hidden)
        app.destroy.assert_not_called()
        app.tray_icon.stop.assert_not_called()

    def test_schema_rejection_on_exit_does_not_write_or_trap_process(self):
        app = FakeApp()
        app._save_window_settings.side_effect = DataCompatibilityError("newer data")
        with patch("app_lifecycle.messagebox.showerror"):
            self.assertTrue(app.on_close())
        app.destroy.assert_called_once()

    def test_update_handoff_skips_second_save_and_busy_guards(self):
        app = FakeApp()
        app._update_handoff = app.update_busy = True
        self.assertTrue(app.on_close())
        app._save_window_settings.assert_not_called()
        app.destroy.assert_called_once()

    def test_worker_dispatch_never_calls_tk_and_drops_late_callbacks(self):
        app = FakeApp()
        callback = Mock()
        worker = threading.Thread(target=lambda: app._call_main(callback))
        worker.start(); worker.join()
        app.after.assert_not_called()
        callback.assert_not_called()
        app._poll_tray_actions()
        callback.assert_called_once()
        app.on_close()
        app._call_main(callback)
        app._handle_tray_action("show")
        self.assertTrue(app._ui_actions.empty())
        self.assertEqual(callback.call_count, 1)

    def test_queue_coalesces_repeated_show_and_exit_wins(self):
        commands = queue.Queue()
        for _ in range(30):
            commands.put("show")
        self.assertEqual(drain_commands(commands), ["show"])
        for action in ("show", "hide", "show", "exit", "global"):
            commands.put(action)
        self.assertEqual(drain_commands(commands), ["exit"])

    def test_callback_failure_does_not_kill_command_pump(self):
        app = FakeApp()
        app._ui_actions.put(Mock(side_effect=RuntimeError("worker result failed")))
        app._poll_tray_actions()
        app.report_callback_exception.assert_called_once()
        app.after.assert_called_once_with(250, app._poll_tray_actions)

    def test_existing_close_policy_is_preserved_but_configurable_later(self):
        app = FakeApp()
        app._on_window_close()
        self.assertTrue(app.lifecycle.hidden)
        app.restore_from_tray()
        app.lifecycle.close_hides = False
        app._on_window_close()
        self.assertTrue(app.lifecycle.stopped)

    def test_global_minimize_is_still_taskbar_iconify(self):
        app = SimpleNamespace(view_mode="global", _active_modal_window=lambda: None,
            _global_window_move=None, global_header=Mock(), _global_tooltip=None,
            _save_window_settings=Mock(), iconify=Mock(), withdraw=Mock())
        CalendarApp.minimize_global(app)
        app.iconify.assert_called_once()
        app.withdraw.assert_not_called()

    def test_hidden_compact_exit_preserves_saved_geometry(self):
        app = FakeApp()
        app.lifecycle.hidden = True
        app._state = "withdrawn"
        app.global_display_mode, app.agenda_open, app.window_mode, app.theme_name = "flow", True, "desktop", "modern"
        app.store = SimpleNamespace(settings={"compact_geometry": {"original": True}}, save=Mock())
        CalendarApp._save_window_settings(app)
        self.assertEqual(app.store.settings["compact_geometry"], {"original": True})
        app._capture_window_geometry.assert_not_called()
        app.store.save.assert_called_once()

    def test_hidden_exit_persists_last_visible_snapshot_not_withdrawn_dimensions(self):
        for view in ("compact", "global"):
            app = FakeApp()
            app.view_mode = view
            app.hide_to_tray()
            snapshot = app.lifecycle.snapshot
            app._capture_window_geometry.side_effect = AssertionError("must not measure hidden window")
            app.global_display_mode, app.agenda_open, app.window_mode, app.theme_name = "flow", True, "desktop", "modern"
            app.store = SimpleNamespace(settings={}, save=Mock())
            CalendarApp._save_window_settings(app)
            self.assertEqual(app.store.settings[view + "_geometry"], snapshot.as_dict())


class NativeTrayTests(unittest.TestCase):
    def tray(self):
        events = []
        tray = TrayIcon("test", Path("missing.ico"), events.append)
        tray._api = SimpleNamespace(user=Mock(), shell=Mock())
        tray._nid = NOTIFYICONDATAW()
        tray._thread = Mock()
        tray._thread.is_alive.return_value = True
        tray._hwnd = 2**44 + 17
        return tray, events

    def test_menu_is_small_and_matches_required_actions(self):
        self.assertEqual([label for _id, label, _action in TrayIcon.MENU if label],
                         ["显示桌面月历", "打开全局视图", "隐藏到托盘", "退出桌面月历"])
        self.assertEqual(set(TrayIcon.COMMANDS.values()), {"show", "global", "hide", "exit"})

    def test_handle_is_not_sufficient_until_shell_registration_succeeds(self):
        tray, events = self.tray()
        self.assertFalse(tray.is_available)
        tray._api.shell.Shell_NotifyIconW.return_value = False
        self.assertFalse(tray._register_icon())
        self.assertFalse(tray.is_available)
        self.assertIn("tray_unavailable", events)

    def test_registration_version_failure_removes_icon(self):
        tray, _events = self.tray()
        tray._api.shell.Shell_NotifyIconW.side_effect = [True, False, True]
        self.assertFalse(tray._register_icon())
        self.assertEqual(tray._api.shell.Shell_NotifyIconW.call_args_list[-1].args[0], tray.NIM_DELETE)

    def test_shell_probe_detects_icon_loss_even_with_live_hwnd(self):
        tray, events = self.tray()
        tray._available.set()
        tray._api.shell.Shell_NotifyIconGetRect.return_value = 0
        self.assertTrue(tray.probe_available())
        tray._api.shell.Shell_NotifyIconGetRect.return_value = -1
        self.assertFalse(tray.probe_available())
        self.assertFalse(tray.is_available)
        self.assertEqual(events, ["tray_unavailable"])

    def test_explorer_message_reregisters_same_icon_and_callback_version(self):
        tray, _events = self.tray()
        tray._taskbar_created = 0xC001
        tray._api.shell.Shell_NotifyIconW.return_value = True
        tray._handle_message(tray._hwnd, tray._taskbar_created, 0, 0)
        self.assertTrue(tray.is_available)
        self.assertEqual([call.args[0] for call in tray._api.shell.Shell_NotifyIconW.call_args_list], [0, 4])

    def test_double_click_and_menu_callbacks_only_emit_commands(self):
        tray, events = self.tray()
        tray._handle_message(tray._hwnd, tray.CALLBACK_MESSAGE, 0, (1 << 16) | tray.WM_LBUTTONDBLCLK)
        tray._handle_message(tray._hwnd, tray.WM_COMMAND, 1002, 0)
        tray._handle_message(tray._hwnd, tray.WM_COMMAND, 9999, 0)
        self.assertEqual(events, ["show", "global"])
        tray._stopping.set()
        tray._emit("exit")
        self.assertEqual(events, ["show", "global"])

    def test_right_menu_destroys_native_resources_and_routes_selected_action(self):
        tray, events = self.tray()
        tray._api.user.TrackPopupMenu.return_value = 1003
        tray._show_menu(tray._hwnd)
        self.assertEqual(events, ["hide"])
        tray._api.user.DestroyMenu.assert_called_once()

    def test_native_api_handles_are_pointer_wide(self):
        api = WindowsTrayAPI()
        self.assertEqual(ctypes.sizeof(api.user.CreateWindowExW.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(api.user.DefWindowProcW.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(api.user.DestroyIcon.argtypes[0]), ctypes.sizeof(ctypes.c_void_p))

    def test_native_failures_cleanup_and_make_readiness_available(self):
        for result in (0, -1):
            api = SimpleNamespace(user=Mock(), shell=Mock(), kernel=Mock())
            api.kernel.GetModuleHandleW.return_value = 2**44 + 3
            api.user.CreateWindowExW.return_value = 2**44 + 1
            api.user.LoadImageW.return_value = 2**44 + 2
            api.user.RegisterWindowMessageW.return_value = 0xC001
            api.user.GetSystemMetrics.return_value = 32
            api.user.GetMessageW.return_value = result
            events = []
            tray = TrayIcon("test", Path("icon.ico"), events.append)
            with patch("tray_icon.WindowsTrayAPI", return_value=api):
                tray._run()
            self.assertTrue(tray._ready.is_set())
            self.assertFalse(tray.is_available)
            api.user.DestroyIcon.assert_called_once()
            api.user.DestroyWindow.assert_called_once()
            api.user.UnregisterClassW.assert_called_once()
            self.assertIn("tray_unavailable", events)

    def test_no_new_tk_root_or_dependency(self):
        text = Path(__file__).resolve().parents[1].joinpath("tray_icon.py").read_text(encoding="utf-8")
        self.assertNotIn("import tkinter", text)
        self.assertNotIn("import pystray", text)
