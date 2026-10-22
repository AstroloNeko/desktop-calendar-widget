import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import CalendarApp
from dpi_utils import WorkArea
from update_dialog import UpdateAvailableDialog, dialog_geometry, release_note_blocks
from update_manager import UpdateInfo, parse_release, RELEASE_ASSET_NAME, CHECKSUM_ASSET_NAME


def update_info(notes="说明"):
    return UpdateInfo("v99.0.0", "99.0.0", "更新标题", notes, "", "", "")


class ReleaseNotesTests(unittest.TestCase):
    def text(self, notes):
        return "\n".join(block.text for block in release_note_blocks(notes))

    def test_empty_notes(self):
        for notes in ("", " \r\n\t"):
            self.assertIn("没有附加说明", self.text(notes))

    def test_lengths_and_final_paragraph_are_never_truncated(self):
        for length in (50, 420, 500, 2000, 20000):
            with self.subTest(length=length):
                notes = "中En123🙂" * length + "\n\n最后一段 FINAL END 🐱"
                self.assertEqual(self.text(notes), notes)

    def test_three_heading_levels(self):
        blocks = release_note_blocks("# 第一章\n## 二级\n### 三级")
        self.assertEqual([b.style for b in blocks], ["h1", "h2", "h3"])
        self.assertEqual([b.text for b in blocks], ["第一章", "二级", "三级"])

    def test_bold_lists_inline_code_and_blank_lines(self):
        notes = "**重要** 与 __加粗__\n\n- 项一\n* 项二\n1. first\n2. second\n`Ctrl+K`"
        self.assertEqual(self.text(notes), "重要 与 加粗\n\n• 项一\n• 项二\n1. first\n2. second\nCtrl+K")

    def test_line_endings_normalized(self):
        self.assertEqual(self.text("一\r\n\r\n二\r三"), "一\n\n二\n三")

    def test_code_preserves_literal_markers(self):
        self.assertEqual(self.text("`**literal**`\n```text\n## not heading\n```"), "**literal**\n## not heading")

    def test_untrusted_html_and_links_are_only_text(self):
        value = '<script>alert(1)</script>\n[link](https://example.test)'
        self.assertEqual(self.text(value), '<script>alert(1)</script>\nlink (https://example.test)')

    def test_release_parser_retains_full_source_body(self):
        notes = "测试" * 3000 + "\n最后一段"
        info = parse_release({"tag_name": "v99.0.0", "body": notes, "assets": [
            {"name": name, "browser_download_url": "https://example.test/" + name}
            for name in (RELEASE_ASSET_NAME, CHECKSUM_ASSET_NAME)
        ]})
        self.assertEqual(info.notes, notes)
        self.assertTrue(self.text(info.notes).endswith("最后一段"))


class DialogGeometryTests(unittest.TestCase):
    def test_scaling_and_bounds(self):
        for dpi in (96, 120, 144, 168, 192):
            for area in (WorkArea(0, 0, 3840, 2120), WorkArea(-1280, -80, 0, 640), WorkArea(0, 0, 800, 560)):
                with self.subTest(dpi=dpi, area=area):
                    w, h, x, y = dialog_geometry(area, dpi, (10000, -10000, 744, 1100))
                    self.assertGreater(w, 0)
                    self.assertGreater(h, 0)
                    self.assertGreaterEqual(x, area.left)
                    self.assertGreaterEqual(y, area.top)
                    self.assertLessEqual(x + w, area.right)
                    self.assertLessEqual(y + h, area.bottom)

    def test_size_not_dependent_on_notes(self):
        self.assertEqual(dialog_geometry(WorkArea(0, 0, 1920, 1040), 96, (0, 0, 500, 500))[:2], (540, 520))


class UpdatePromptRoutingTests(unittest.TestCase):
    def owner(self):
        return SimpleNamespace(_close_update_dialog=Mock(), update_busy=False,
                               update_dialog=None, wait_window=Mock(),
                               _start_update_download=Mock(), after=Mock(), apply_window_mode=Mock())

    def test_accept_uses_existing_download_flow_with_original_info(self):
        app = self.owner()
        info = update_info("最后一段" * 1000)
        dialog = SimpleNamespace(accepted=True)
        with patch("app.UpdateAvailableDialog", return_value=dialog) as constructor:
            CalendarApp._finish_update_check(app, info, None)
        self.assertIs(constructor.call_args.args[1], info)
        app.wait_window.assert_called_once_with(dialog)
        app._start_update_download.assert_called_once_with(info)
        self.assertFalse(app.update_busy)
        self.assertIsNone(app.update_dialog)

    def test_cancel_does_not_start_download(self):
        app = self.owner()
        with patch("app.UpdateAvailableDialog", return_value=SimpleNamespace(accepted=False)):
            CalendarApp._finish_update_check(app, update_info(), None)
        app._start_update_download.assert_not_called()
        app.after.assert_called_once_with(100, app.apply_window_mode)

    def test_prompt_failure_clears_busy_reference(self):
        app = self.owner()
        with patch("app.UpdateAvailableDialog", side_effect=RuntimeError("cannot create")):
            with self.assertRaises(RuntimeError):
                CalendarApp._finish_update_check(app, update_info(), None)
        self.assertFalse(app.update_busy)
        self.assertIsNone(app.update_dialog)

    def test_close_releases_only_own_grab_and_leaves_shared_modal_stack(self):
        for owns_grab in (True, False):
            owner = SimpleNamespace(_leave_modal=Mock())
            dialog = SimpleNamespace(_closing=False, master_app=owner, grab_release=Mock(), destroy=Mock())
            dialog.grab_current = lambda: dialog if owns_grab else object()
            UpdateAvailableDialog.close(dialog, True)
            self.assertTrue(dialog.accepted)
            self.assertEqual(dialog.grab_release.call_count, int(owns_grab))
            owner._leave_modal.assert_called_once_with(dialog)
            dialog.destroy.assert_called_once()
            UpdateAvailableDialog.close(dialog, False)
            dialog.destroy.assert_called_once()

    def test_wheel_scrolls_readonly_document(self):
        notes = SimpleNamespace(yview_scroll=Mock())
        for delta, units in ((120, -1), (-120, 1), (240, -2), (30, -1), (-30, 1)):
            self.assertEqual(UpdateAvailableDialog._wheel(SimpleNamespace(notes=notes), SimpleNamespace(delta=delta)), "break")
            notes.yview_scroll.assert_called_with(units, "units")


if __name__ == "__main__":
    unittest.main()
