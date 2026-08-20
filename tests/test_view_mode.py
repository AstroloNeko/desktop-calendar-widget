import unittest

from dpi_utils import WorkArea, scale_px
from view_mode import (
    WindowGeometry,
    fit_geometry_to_work_area,
    initial_global_geometry,
    normalize_view_mode,
)


class ViewModeTests(unittest.TestCase):
    def test_default_and_invalid_modes_are_compact(self) -> None:
        self.assertEqual(normalize_view_mode(None), "compact")
        self.assertEqual(normalize_view_mode("large"), "compact")
        self.assertEqual(normalize_view_mode("global"), "global")

    def test_geometry_round_trip_is_stable(self) -> None:
        geometry = WindowGeometry(1100, 700, -1200, 80)
        self.assertEqual(WindowGeometry.from_mapping(geometry.as_dict()), geometry)

    def test_invalid_geometry_has_safe_fallback(self) -> None:
        self.assertIsNone(WindowGeometry.from_mapping(None))
        self.assertIsNone(WindowGeometry.from_mapping({"width": "bad", "height": 600, "x": 0, "y": 0}))
        self.assertIsNone(WindowGeometry.from_mapping({"width": 0, "height": 600, "x": 0, "y": 0}))

    def test_initial_global_geometry_uses_current_monitor_and_dpi(self) -> None:
        for dpi in (96, 120, 144, 168, 192):
            with self.subTest(dpi=dpi):
                area = WorkArea(-3840, 0, 0, 2160)
                geometry = initial_global_geometry(area, dpi)
                device_width = scale_px(geometry.width, dpi)
                device_height = scale_px(geometry.height, dpi)
                self.assertGreaterEqual(geometry.x, area.left)
                self.assertLessEqual(geometry.x + device_width, area.right)
                self.assertGreaterEqual(geometry.y, area.top)
                self.assertLessEqual(geometry.y + device_height, area.bottom)
                self.assertGreater(device_width, area.width * 0.75)
                self.assertLess(device_width, area.width * 0.95)

    def test_fit_geometry_clamps_size_and_position(self) -> None:
        area = WorkArea(100, 50, 1100, 750)
        fitted = fit_geometry_to_work_area(WindowGeometry(2000, 1600, -9000, 9000), area, 96)
        self.assertGreaterEqual(fitted.x, area.left)
        self.assertGreaterEqual(fitted.y, area.top)
        self.assertLessEqual(fitted.x + fitted.width, area.right)
        self.assertLessEqual(fitted.y + fitted.height, area.bottom)


if __name__ == "__main__":
    unittest.main()
