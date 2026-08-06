import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
