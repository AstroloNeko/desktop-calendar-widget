import unittest
import ctypes
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dpi_utils


class DpiMathTests(unittest.TestCase):
    def tearDown(self) -> None:
        dpi_utils.set_active_dpi(dpi_utils.BASE_DPI)

    def test_common_windows_scales(self) -> None:
        expectations = {
            96: (1.0, 372, 1),
            120: (1.25, 465, 1),
            144: (1.5, 558, 2),
            168: (1.75, 651, 2),
            192: (2.0, 744, 2),
        }
        for dpi, (factor, width, line) in expectations.items():
            self.assertEqual(dpi_utils.scale_factor(dpi), factor)
            self.assertEqual(dpi_utils.scale_px(372, dpi), width)
            self.assertEqual(dpi_utils.scale_line_width(1, dpi), line)

    def test_offsets_round_symmetrically(self) -> None:
        for dpi in (96, 120, 144, 168, 192):
            for value in (1, 3, 7, 14, 17):
                self.assertEqual(dpi_utils.scale_px(-value, dpi), -dpi_utils.scale_px(value, dpi))

    def test_geometry_scales_size_but_keeps_device_position(self) -> None:
        self.assertEqual(dpi_utils.scaled_geometry(372, 548, -120, 80, 192), "744x1096-120+80")

    def test_scale_round_trip_is_stable_for_layout_values(self) -> None:
        for dpi in (96, 120, 144, 168, 192):
            for value in (1, 4, 25, 46, 372, 548):
                device = dpi_utils.scale_px(value, dpi)
                self.assertLessEqual(abs(dpi_utils.unscale_px(device, dpi) - value), 1)


class DpiBuildConfigurationTests(unittest.TestCase):
    def test_manifest_declares_per_monitor_v2(self) -> None:
        project = Path(__file__).resolve().parents[1]
        manifest = project / "windows_per_monitor_v2.manifest"
        ET.parse(manifest)
        contents = manifest.read_text(encoding="utf-8")
        self.assertIn("PerMonitorV2, PerMonitor", contents)
        self.assertIn("true/pm", contents)

    def test_release_build_embeds_manifest_in_both_executables(self) -> None:
        project = Path(__file__).resolve().parents[1]
        build_script = (project / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn('windows_per_monitor_v2.manifest', build_script)
        self.assertEqual(build_script.count('"--manifest", $manifestPath'), 2)

    def test_release_build_splats_complete_python_argument_arrays(self) -> None:
        project = Path(__file__).resolve().parents[1]
        build_script = (project / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn('& $pythonExe @Arguments', build_script)
        self.assertIn('"-m", "PyInstaller"', build_script)
        self.assertNotIn('& $pythonCommand @pythonPrefix', build_script)


@unittest.skipUnless(dpi_utils.IS_WINDOWS, "Win32 monitor API is Windows-only")
class Win32MonitorHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user32 = ctypes.windll.user32
        self.widget = SimpleNamespace(winfo_screenwidth=lambda: 2880, winfo_screenheight=lambda: 1620)

    def test_monitor_signatures_preserve_small_and_pointer_sized_handles(self) -> None:
        for monitor_handle in (0x10001, 0x100000000):
            with self.subTest(handle=monitor_handle):
                seen = []

                def monitor_info(handle, info_pointer):
                    seen.append(handle.value)
                    info = ctypes.cast(info_pointer, ctypes.POINTER(dpi_utils.MonitorInfo)).contents
                    info.rcWork.left, info.rcWork.top = -1920, 0
                    info.rcWork.right, info.rcWork.bottom = 0, 1080
                    return 1

                with patch.object(dpi_utils, "window_handle", return_value=0x100000000), \
                     patch.object(self.user32, "MonitorFromWindow", return_value=monitor_handle) as from_window, \
                     patch.object(self.user32, "GetMonitorInfoW", side_effect=monitor_info) as get_info:
                    self.assertEqual(dpi_utils.work_area_for_window(self.widget), dpi_utils.WorkArea(-1920, 0, 0, 1080))
                self.assertEqual(seen, [monitor_handle])
                self.assertEqual(from_window.argtypes[0], dpi_utils.wintypes.HWND)
                self.assertEqual(from_window.restype, dpi_utils.wintypes.HANDLE)
                self.assertEqual(get_info.argtypes[0], dpi_utils.wintypes.HANDLE)
                self.assertEqual(get_info.restype, dpi_utils.wintypes.BOOL)

    def test_invalid_large_monitor_handle_falls_back_without_overflow(self) -> None:
        with patch.object(dpi_utils, "window_handle", return_value=0x10001), \
             patch.object(self.user32, "MonitorFromWindow", return_value=0x100000000):
            self.assertEqual(
                dpi_utils.work_area_for_window(self.widget),
                dpi_utils.WorkArea(0, 0, 2880, 1620),
            )

    def test_rect_path_preserves_negative_coordinates_and_large_monitor_handle(self) -> None:
        seen = []

        def from_rect(rect_pointer, flag):
            rect = ctypes.cast(rect_pointer, ctypes.POINTER(dpi_utils.wintypes.RECT)).contents
            seen.append((rect.left, rect.top, rect.right, rect.bottom))
            return 0x100000000

        def monitor_info(handle, info_pointer):
            self.assertEqual(handle.value, 0x100000000)
            info = ctypes.cast(info_pointer, ctypes.POINTER(dpi_utils.MonitorInfo)).contents
            info.rcWork.left, info.rcWork.top = -1920, -200
            info.rcWork.right, info.rcWork.bottom = 0, 880
            return 1

        with patch.object(self.user32, "MonitorFromRect", side_effect=from_rect) as monitor_from_rect, \
             patch.object(self.user32, "GetMonitorInfoW", side_effect=monitor_info):
            area = dpi_utils.work_area_for_rect(-1800, -100, 500, 400)
        self.assertEqual(seen, [(-1800, -100, -1300, 300)])
        self.assertEqual(area, dpi_utils.WorkArea(-1920, -200, 0, 880))
        self.assertEqual(monitor_from_rect.argtypes[0], ctypes.POINTER(dpi_utils.wintypes.RECT))

    def test_invalid_large_rect_monitor_handle_does_not_overflow(self) -> None:
        with patch.object(self.user32, "MonitorFromRect", return_value=0x100000000):
            self.assertEqual(
                dpi_utils.work_area_for_rect(100, 200, 372, 548),
                dpi_utils.WorkArea(100, 200, 472, 748),
            )

    def test_implausible_monitor_work_area_uses_safe_fallback(self) -> None:
        def monitor_info(_handle, info_pointer):
            info = ctypes.cast(info_pointer, ctypes.POINTER(dpi_utils.MonitorInfo)).contents
            info.rcWork.left, info.rcWork.top = -2_000_000, 0
            info.rcWork.right, info.rcWork.bottom = 0, 1080
            return 1

        with patch.object(dpi_utils, "window_handle", return_value=0x10001), \
             patch.object(self.user32, "MonitorFromWindow", return_value=0x10001), \
             patch.object(self.user32, "GetMonitorInfoW", side_effect=monitor_info):
            self.assertEqual(
                dpi_utils.work_area_for_window(self.widget),
                dpi_utils.WorkArea(0, 0, 2880, 1620),
            )


if __name__ == "__main__":
    unittest.main()
