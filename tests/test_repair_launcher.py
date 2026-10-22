import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import repair_launcher


class RepairLauncherTests(unittest.TestCase):
    def test_repair_window_reserves_space_for_controls_at_common_dpis(self) -> None:
        for dpi in (96, 120, 144, 168, 192):
            with self.subTest(dpi=dpi):
                width, height = repair_launcher.repair_window_size(dpi, 500, 260)
                self.assertGreaterEqual(width, round(410 * dpi / 96))
                self.assertGreaterEqual(height, round(220 * dpi / 96))
                self.assertGreaterEqual(width, 500 + round(24 * dpi / 96))
                self.assertGreaterEqual(height, 260 + round(24 * dpi / 96))

    def test_can_update_or_reinstall_but_not_downgrade(self) -> None:
        self.assertEqual(repair_launcher.repair_action("0.8.3", "0.8.2"), "更新")
        self.assertEqual(repair_launcher.repair_action("0.8.2", "0.8.2"), "重新安装")
        with self.assertRaises(repair_launcher.RepairError):
            repair_launcher.repair_action("0.8.1", "0.8.2")

    def test_only_targets_portable_installation_beside_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            launcher = directory / "DesktopCalendarRepair.exe"
            with self.assertRaises(repair_launcher.RepairError):
                repair_launcher.installation_directory(launcher)
            (directory / "DesktopCalendar.exe").write_bytes(b"broken")
            self.assertEqual(repair_launcher.installation_directory(launcher), directory)
            (directory / "DesktopCalendar.exe").unlink()
            (directory / "_internal").mkdir()
            self.assertEqual(repair_launcher.installation_directory(launcher), directory)
            self.assertEqual(repair_launcher.validate_installation_directory(directory), directory)

    def test_stages_new_updater_without_touching_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "release.zip"
            user_data = directory / "calendar_data.json"
            user_data.write_text('{"events": ["keep"]}', encoding="utf-8")
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("DesktopCalendar/DesktopCalendar.exe", b"MZmain")
                package.writestr("DesktopCalendar/DesktopCalendarUpdater.exe", b"MZupdater")
                package.writestr("DesktopCalendar/_internal/base_library.zip", b"runtime")
            staged = repair_launcher.stage_verified_updater(archive)
            try:
                self.assertEqual(staged.read_bytes(), b"MZupdater")
                self.assertEqual(user_data.read_text(encoding="utf-8"), '{"events": ["keep"]}')
            finally:
                shutil.rmtree(staged.parent)

    def test_rejects_incomplete_or_invalid_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "release.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("DesktopCalendar/DesktopCalendar.exe", b"MZmain")
            with self.assertRaises(repair_launcher.RepairError):
                repair_launcher.stage_verified_updater(archive)
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("DesktopCalendar/DesktopCalendar.exe", b"MZmain")
                package.writestr("DesktopCalendar/DesktopCalendarUpdater.exe", b"not executable")
                package.writestr("DesktopCalendar/_internal/base_library.zip", b"runtime")
            with self.assertRaises(repair_launcher.RepairError):
                repair_launcher.stage_verified_updater(archive)

    def test_running_main_program_blocks_overwrite(self) -> None:
        with patch.object(repair_launcher, "main_app_running", return_value=True), \
             patch.object(repair_launcher.subprocess, "Popen") as popen:
            with self.assertRaises(repair_launcher.RepairError):
                repair_launcher.launch_repair_updater(
                    Path("C:/Temp/DesktopCalendarUpdater.exe"),
                    Path("C:/Temp/release.zip"),
                    Path("C:/Calendar"),
                )
            popen.assert_not_called()

    def test_launch_passes_install_directory_and_parent_pid(self) -> None:
        with patch.object(repair_launcher, "main_app_running", return_value=False), \
             patch.object(repair_launcher.subprocess, "Popen") as popen:
            repair_launcher.launch_repair_updater(
                Path("C:/Temp/DesktopCalendarUpdater.exe"),
                Path("C:/Temp/release.zip"),
                Path("C:/Calendar"),
            )
        arguments = popen.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--install-dir") + 1], str(Path("C:/Calendar")))
        self.assertEqual(arguments[arguments.index("--executable") + 1], "DesktopCalendar.exe")
        self.assertIn("--pid", arguments)

    def test_release_build_packages_standalone_repair_executable(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn('"repair_launcher.py"', script)
        self.assertIn('"--name", "DesktopCalendarRepair"', script)
        self.assertIn('repair-dist\\DesktopCalendarRepair.exe', script)
        self.assertIn('"DesktopCalendarRepair.exe.sha256"', script)


if __name__ == "__main__":
    unittest.main()
