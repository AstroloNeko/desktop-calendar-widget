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
        app = SimpleNamespace(_active_modal_window=lambda: object(), state=lambda: "normal", _start_drag=Mock())
        fake = SimpleNamespace(app=app, hide_tooltips=Mock())
        GlobalHeader._start_drag(fake, "event")
        app._start_drag.assert_not_called()
        app._active_modal_window = lambda: None
        app.state = lambda: "zoomed"
        GlobalHeader._start_drag(fake, "event")
        app._start_drag.assert_not_called()
        app.state = lambda: "normal"
        GlobalHeader._start_drag(fake, "event")
        app._start_drag.assert_called_once_with("event")

    def test_minimize_saves_geometry_hides_tips_and_iconifies_without_rebuild(self):
        actions = []
        fake = SimpleNamespace(view_mode="global", _active_modal_window=lambda: None,
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
                                    SetWindowPos=Mock())
        return patch.object(ctypes, "windll", SimpleNamespace(user32=self.user), create=True)

    def test_caption_removed_but_sizing_minimize_and_system_menu_retained(self):
        original = 0x10CF0000
        with self.setup_native({-16: original, -20: 0x88}), patch.object(native, "IS_WINDOWS", True), patch.object(native, "window_handle", return_value=self.hwnd):
            native.make_frameless_app_window(object())
        calls = self.user.SetWindowLongPtrW.call_args_list
        self.assertEqual(calls[0].args, (self.hwnd, -16, original & ~0x00C00000))
        self.assertEqual(calls[1].args, (self.hwnd, -20, 0x40008))  # retain TOPMOST preference
        self.assertEqual(self.user.SetWindowPos.call_args.args, (self.hwnd, None, 0, 0, 0, 0, 0x37))

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


if __name__ == "__main__":
    unittest.main()
