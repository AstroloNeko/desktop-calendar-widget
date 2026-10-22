import ctypes
from ctypes import wintypes
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import CalendarApp, ThemeButton, Tooltip
from global_header import GlobalHeader, HEADER_HEIGHT, header_layout
from ui_draw import draw_compact_view_icon
import win_integration as native


class GlobalHeaderTests(unittest.TestCase):
    def test_responsive_controls_fit_without_overlap(self):
        for width in (700, 712, 720, 800, 899, 900, 1240, 1800):
            bounds = header_layout(width)
            with self.subTest(width=width):
                controls = sorted((box for key, box in bounds.items() if key not in ("title", "drag")))
                for x, y, w, h in controls:
                    self.assertGreaterEqual(x, 0)
                    self.assertLessEqual(x + w, width)
                    self.assertGreaterEqual(w, 30)
                    self.assertEqual(h, 32)
                    self.assertLessEqual(y + h, HEADER_HEIGHT)
                for left, right in zip(controls, controls[1:]):
                    self.assertLess(left[0] + left[2], right[0])
                self.assertGreaterEqual(bounds["drag"][2], 35)
                self.assertLess(bounds["title"][0] + bounds["title"][2], bounds["previous"][0])

    def test_header_order_and_equal_navigation(self):
        for width in (712, 1240):
            bounds = header_layout(width)
            keys = ("previous", "today", "following", "create", "habits", "ddl", "pin", "more", "compact", "minimize")
            self.assertEqual(sorted(keys, key=lambda key: bounds[key][0]), list(keys))
            self.assertEqual(bounds["previous"][2:], bounds["today"][2:])
            self.assertEqual(bounds["following"][2:], bounds["today"][2:])

    def test_layout_scales_proportionally_at_all_supported_dpis(self):
        from dpi_utils import scale_px
        bounds = header_layout(712)
        for dpi in (96, 120, 144, 168, 192):
            for key, (x, y, width, height) in bounds.items():
                with self.subTest(dpi=dpi, key=key):
                    self.assertLessEqual(scale_px(x + width, dpi), scale_px(712, dpi))
                    self.assertLessEqual(scale_px(y + height, dpi), scale_px(HEADER_HEIGHT, dpi))

    def test_compact_icon_is_two_vector_rectangles(self):
        with patch("ui_draw.rounded_rectangle") as rectangle:
            draw_compact_view_icon(object(), 16, 16, foreground="outline", background="surface")
        self.assertEqual(rectangle.call_count, 2)
        first, second = rectangle.call_args_list
        self.assertEqual(first.args[1:5], (7, 8, 21, 19))
        self.assertEqual(second.args[1:5], (15, 15, 25, 24))
        self.assertEqual(second.kwargs["fill"], "surface")

    def test_compact_icon_uses_existing_button_state_surfaces(self):
        source = inspect.getsource(ThemeButton.draw)
        self.assertIn('self.icon == "compact"', source)
        self.assertIn("background=background", source)
        self.assertIn('self.state == "pressed"', source)

    def test_buttons_are_not_drag_targets(self):
        source = inspect.getsource(GlobalHeader)
        self.assertIn("for area in (self, self.month, self.subtitle)", source)
        self.assertNotIn('button.bind("<B1-Motion>"', source)

    def test_drag_is_blocked_by_modal_or_maximized_window(self):
        app = SimpleNamespace(_active_modal_window=lambda: object(), state=lambda: "normal", _start_global_window_move=Mock())
        fake = SimpleNamespace(app=app, hide_tooltips=Mock())
        GlobalHeader._start_drag(fake, "event")
        app._start_global_window_move.assert_not_called()
        app._active_modal_window = lambda: None
        app.state = lambda: "zoomed"
        GlobalHeader._start_drag(fake, "event")
        app._start_global_window_move.assert_not_called()
        app.state = lambda: "normal"
        GlobalHeader._start_drag(fake, "event")
        app._start_global_window_move.assert_called_once_with("event")

    def test_minimize_saves_geometry_hides_tips_and_iconifies_without_rebuild(self):
        actions = []
        fake = SimpleNamespace(view_mode="global", _active_modal_window=lambda: None,
                               _global_window_move=None,
                               global_header=SimpleNamespace(hide_tooltips=lambda: actions.append("tips")),
                               _global_tooltip=SimpleNamespace(hide=lambda: actions.append("canvas-tip")),
                               _save_window_settings=lambda: actions.append("save"),
                               iconify=lambda: actions.append("iconify"))
        CalendarApp.minimize_global(fake)
        self.assertEqual(actions, ["tips", "canvas-tip", "save", "iconify"])
        fake.view_mode = "compact"
        CalendarApp.minimize_global(fake)
        self.assertEqual(len(actions), 4)
        fake.view_mode = "global"
        fake._active_modal_window = lambda: object()
        CalendarApp.minimize_global(fake)
        self.assertEqual(len(actions), 4)

    def test_tooltip_reschedule_cancels_previous_timer(self):
        fake = SimpleNamespace(hide=Mock(), show=Mock(), widget=SimpleNamespace(after=Mock(return_value="job")))
        Tooltip._schedule(fake)
        fake.hide.assert_called_once()
        fake.widget.after.assert_called_once_with(550, fake.show)

    def test_tooltip_has_no_grab_focus_or_topmost(self):
        source = inspect.getsource(Tooltip)
        for forbidden in ("grab_set", "focus_force", 'attributes("-topmost"'):
            self.assertNotIn(forbidden, source)
        self.assertIn("transient(owner)", source)
        self.assertIn("clamp_to_work_area", source)
        self.assertIn('"<Unmap>"', source)
        self.assertIn('"<Destroy>"', source)


class FramelessNativeTests(unittest.TestCase):
    def setup_native(self, styles):
        self.hwnd = 0x200123456789
        self.user = SimpleNamespace(GetWindowLongPtrW=Mock(side_effect=lambda _hwnd, index: styles[index]),
                                    GetWindowLongW=Mock(), SetWindowLongPtrW=Mock(), SetWindowLongW=Mock(),
                                    SetWindowPos=Mock(return_value=True), GetClientRect=Mock(), GetWindowRect=Mock())
        def client(_hwnd, pointer):
            bounds = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            bounds.left, bounds.top = 0, 0
            bounds.right, bounds.bottom = (1920, 1200) if self.user.GetClientRect.call_count == 1 else (1922, 1248)
            return True
        def outer(_hwnd, pointer):
            bounds = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            bounds.left, bounds.top, bounds.right, bounds.bottom = 100, 50, 2046, 1322
            return True
        self.user.GetClientRect.side_effect = client
        self.user.GetWindowRect.side_effect = outer
        return patch.object(ctypes, "windll", SimpleNamespace(user32=self.user), create=True)

    def test_caption_removed_but_sizing_minimize_and_system_menu_retained(self):
        original = 0x10CF0000
        with self.setup_native({-16: original, -20: 0x88}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            native.make_frameless_app_window(object())
        calls = self.user.SetWindowLongPtrW.call_args_list
        self.assertEqual(calls[0].args, (self.hwnd, -16, original & ~0x00C00000))
        self.assertEqual(calls[1].args, (self.hwnd, -20, 0x40008))  # retain TOPMOST preference
        self.assertEqual(self.user.SetWindowPos.call_args_list[0].args, (self.hwnd, None, 0, 0, 0, 0, 0x37))
        self.assertEqual(self.user.SetWindowPos.call_args_list[1].args, (self.hwnd, None, 0, 0, 1944, 1224, 0x16))

    def test_caption_removal_does_not_resize_if_client_size_is_unchanged(self):
        with self.setup_native({-16: 0x10CF0000, -20: 0x40000}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            self.user.GetClientRect.side_effect = lambda _h, _p: True
            native.make_frameless_app_window(object())
        self.user.SetWindowPos.assert_called_once_with(self.hwnd, None, 0, 0, 0, 0, 0x37)

    def test_caption_size_read_failure_is_not_silently_ignored(self):
        with self.setup_native({-16: 0x10CF0000, -20: 0x40000}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            self.user.GetClientRect.side_effect = lambda _h, _p: False
            with self.assertRaises(OSError):
                native.make_frameless_app_window(object())
        self.user.SetWindowLongPtrW.assert_not_called()

    def test_native_handles_are_pointer_wide(self):
        with self.setup_native({-16: 0xCF0000, -20: 0}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            native.make_frameless_app_window(object())
        self.assertEqual(self.user.GetWindowLongPtrW.argtypes, [wintypes.HWND, ctypes.c_int])
        self.assertEqual(self.user.SetWindowLongPtrW.argtypes, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t])
        self.assertEqual(self.user.GetWindowLongPtrW.restype, ctypes.c_ssize_t)
        self.assertEqual(self.user.SetWindowPos.argtypes[:2], [wintypes.HWND, wintypes.HWND])

    def test_restoration_is_idempotent_and_never_activates_or_rebuilds(self):
        with self.setup_native({-16: 0x160F0008, -20: 0x40000}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            for _ in range(20):
                native.make_frameless_app_window(object())
        self.user.SetWindowLongPtrW.assert_not_called()
        self.user.SetWindowPos.assert_not_called()

    def test_get_ancestor_accepts_large_hwnd(self):
        user = SimpleNamespace(GetAncestor=Mock(return_value=0x200123456789))
        widget = SimpleNamespace(update_idletasks=Mock(), winfo_id=lambda: 0x200123456700)
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user), create=True), patch.object(native, "IS_WINDOWS", True):
            self.assertEqual(native.window_handle(widget), 0x200123456789)
        self.assertEqual(user.GetAncestor.argtypes, [wintypes.HWND, wintypes.UINT])
        self.assertEqual(user.GetAncestor.restype, wintypes.HWND)

    def test_non_windows_is_noop(self):
        with patch.object(native, "IS_WINDOWS", False), patch.object(native, "window_handle") as handle:
            native.make_frameless_app_window(object())
        handle.assert_not_called()

    def test_mapped_handle_lookup_can_avoid_idle_modal_reentrancy(self):
        user = SimpleNamespace(GetAncestor=Mock(return_value=0x200123456789))
        widget = SimpleNamespace(update_idletasks=Mock(), winfo_id=lambda: 0x200123456700)
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user), create=True), patch.object(native, "IS_WINDOWS", True):
            native.window_handle(widget, refresh=False)
        widget.update_idletasks.assert_not_called()


class NativeMoveTests(unittest.TestCase):
    def setUp(self):
        self.hwnd = 0x200123456789
        self.client = self.hwnd+1
        self.widget = SimpleNamespace(winfo_id=lambda: self.client)
        self.user = SimpleNamespace(**{name: Mock() for name in (
            "GetAsyncKeyState", "GetWindowThreadProcessId", "SetWinEventHook",
            "ReleaseCapture", "PostMessageW", "UnhookWinEvent", "GetWindowRect", "SetWindowPos",
        )})
        self.user.GetAsyncKeyState.return_value = -32768
        self.user.SetWinEventHook.return_value = self.hwnd+2
        self.user.PostMessageW.return_value = True
        def process(_hwnd, pointer):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 123
            return 456
        self.user.GetWindowThreadProcessId.side_effect = process
        self.windll = patch.object(ctypes, "windll", SimpleNamespace(user32=self.user), create=True)
        self.windll.start()
        self.addCleanup(self.windll.stop)
        self.windows = patch.object(native, "IS_WINDOWS", True)
        self.windows.start()
        self.addCleanup(self.windows.stop)
        self.handle = patch.object(native, "window_handle", return_value=self.hwnd)
        self.handle.start()
        self.addCleanup(self.handle.stop)

    def test_move_is_posted_and_hook_is_scoped_to_own_process_and_thread(self):
        move = native.NativeWindowMove(self.widget)
        self.assertTrue(move.start())
        self.user.PostMessageW.assert_called_once_with(self.hwnd, 0x112, 0xF012, 0)
        self.assertEqual(self.user.SetWinEventHook.call_args.args[4:], (123, 456, 0))
        self.assertEqual(self.user.PostMessageW.argtypes, [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t])
        self.assertNotIn("SendMessage", inspect.getsource(native.NativeWindowMove.start))

    def test_released_press_does_not_enter_keyboard_move_mode(self):
        self.user.GetAsyncKeyState.return_value = 0
        self.assertFalse(native.NativeWindowMove(self.widget).start())
        self.user.SetWinEventHook.assert_not_called()
        self.user.PostMessageW.assert_not_called()

    def test_end_event_returns_consumed_release_to_root_without_calling_tk(self):
        move = native.NativeWindowMove(self.widget)
        move.start()
        move._event(None, 0xA, self.hwnd, 0, 0, 456, 0)
        self.assertTrue(move.started)
        move._event(None, 0xB, self.hwnd+100, 0, 0, 456, 0)
        self.assertFalse(move.ended)
        move._event(None, 0xB, self.hwnd, 0, 0, 456, 0)
        self.assertTrue(move.ended)
        self.assertIsNone(move.hook)
        self.user.PostMessageW.assert_called_with(self.client, 0x202, 0, 0)
        count = self.user.PostMessageW.call_count
        move._event(None, 0xB, self.hwnd, 0, 0, 456, 0)
        self.assertEqual(count, self.user.PostMessageW.call_count)
        self.user.UnhookWinEvent.assert_called_once()
        source = inspect.getsource(native.NativeWindowMove._event)
        for unsafe in (".after", ".event_generate", ".update", ".geometry"):
            self.assertNotIn(unsafe, source)

    def test_post_failure_releases_hook(self):
        self.user.PostMessageW.return_value = False
        move = native.NativeWindowMove(self.widget)
        with self.assertRaises(OSError):
            move.start()
        self.assertIsNone(move.hook)
        self.user.UnhookWinEvent.assert_called_once()

    def test_cancel_closes_hook_and_native_move_loop_without_tk_calls(self):
        move = native.NativeWindowMove(self.widget)
        move.start()
        move.started = True
        move.close()
        self.user.PostMessageW.assert_called_with(self.hwnd, 0x1F, 0, 0)
        self.assertIsNone(move.hook)

    def test_release_without_press_cannot_click_through_to_a_toolbar_button(self):
        fake = SimpleNamespace(_armed=False, winfo_width=lambda: 60, winfo_height=lambda: 30,
                               _set_state=Mock(), command=Mock())
        event = SimpleNamespace(x=20, y=15)
        ThemeButton._release(fake, event)
        fake.command.assert_not_called()
        ThemeButton._press(fake)
        ThemeButton._release(fake, event)
        fake.command.assert_called_once()
        ThemeButton._release(fake, event)
        fake.command.assert_called_once()

    def test_hook_failure_does_not_release_capture_or_post_move(self):
        self.user.SetWinEventHook.return_value = None
        with self.assertRaises(OSError):
            native.NativeWindowMove(self.widget).start()
        self.user.ReleaseCapture.assert_not_called()
        self.user.PostMessageW.assert_not_called()

    def test_invalid_thread_cannot_accidentally_install_a_global_desktop_hook(self):
        self.user.GetWindowThreadProcessId.side_effect = None
        self.user.GetWindowThreadProcessId.return_value = 0
        with self.assertRaises(OSError):
            native.NativeWindowMove(self.widget).start()
        self.user.SetWinEventHook.assert_not_called()

    def test_native_finish_clamps_outer_rectangle_not_client_size(self):
        def bounds(_hwnd, pointer):
            result = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            result.left, result.top, result.right, result.bottom = -2100, 50, -500, 1150
            return True
        self.user.GetWindowRect.side_effect = bounds
        with patch.object(native, "clamp_to_work_area", return_value=(-1916, 50)) as clamp:
            native.clamp_native_window(self.widget)
        clamp.assert_called_once_with(-2100, 50, 1600, 1100)
        self.user.SetWindowPos.assert_called_once_with(self.hwnd, None, -1916, 50, 0, 0, 0x215)

    def test_already_in_bounds_does_not_move_or_reframe(self):
        def bounds(_hwnd, pointer):
            result = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            result.left, result.top, result.right, result.bottom = -1500, 50, -500, 650
            return True
        self.user.GetWindowRect.side_effect = bounds
        with patch.object(native, "clamp_to_work_area", return_value=(-1500, 50)):
            native.clamp_native_window(self.widget)
        self.user.SetWindowPos.assert_not_called()

    def test_window_drag_end_saves_once_and_never_creates_tk_geometry_write(self):
        fake = SimpleNamespace(view_mode="global", dpi=SimpleNamespace(current_window_dpi=lambda: 192, dpi=192),
                               _save_window_settings=Mock(), after=Mock(), apply_window_mode=Mock(), geometry=Mock())
        with patch("app.IS_WINDOWS", True), patch("app.clamp_native_window") as clamp:
            CalendarApp._end_drag(fake)
        clamp.assert_called_once_with(fake)
        fake.geometry.assert_not_called()
        fake._save_window_settings.assert_called_once()

    def test_native_end_is_consumed_once_by_normal_tk_handler(self):
        session = SimpleNamespace(ended=True, close=Mock())
        fake = SimpleNamespace(_global_window_move=session, winfo_exists=lambda: True,
                               view_mode="global", state=lambda: "normal", _end_drag=Mock())
        fake._finish_global_window_move = lambda: CalendarApp._finish_global_window_move(fake)
        CalendarApp._on_global_window_move_end(fake)
        CalendarApp._on_global_window_move_end(fake)
        fake._end_drag.assert_called_once()
        session.close.assert_called_once()
        self.assertIsNone(fake._global_window_move)

    def test_geometry_and_dpi_saves_wait_until_native_move_ends(self):
        fake = SimpleNamespace(_global_window_move=object(), _dpi_rebuilding=False,
                               view_mode="global", winfo_exists=lambda: True, state=lambda: "normal", store=Mock())
        CalendarApp._save_global_geometry(fake)
        CalendarApp._check_dpi_change(fake)
        fake.store.save.assert_not_called()

    def test_late_configure_does_not_save_identical_window_geometry_twice(self):
        from view_mode import WindowGeometry
        geometry = WindowGeometry(1000, 600, 80, 50)
        fake = SimpleNamespace(_global_window_move=None, view_mode="global", winfo_exists=lambda: True,
                               state=lambda: "normal", _capture_window_geometry=lambda: geometry,
                               store=SimpleNamespace(settings={"global_geometry": geometry.as_dict()}, save=Mock()))
        CalendarApp._save_global_geometry(fake)
        fake.store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
