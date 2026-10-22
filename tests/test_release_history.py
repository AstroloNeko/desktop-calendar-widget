import ast
from pathlib import Path
import queue
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import CalendarApp
import release_notes
import repair_launcher as repair
from update_dialog import ReleaseHistoryDialog
from update_manager import UpdateInfo
from version import __version__


def info(version="99.0.0", notes="目标日志"):
    return UpdateInfo("v" + version, version, "目标版本", notes, "", "", "")


class HistoryTests(unittest.TestCase):
    def test_version_sections_do_not_include_adjacent_release(self):
        entries = release_notes.parse_history("# Log\n## v0.10.0 — date\n### 新增\nNEW\n## v0.9.3\nOLD")
        self.assertEqual([e.version for e in entries], ["0.10.0", "0.9.3"])
        self.assertIn("NEW", entries[0].notes)
        self.assertNotIn("OLD", entries[0].notes)

    def test_no_truncation_and_markdown_shared(self):
        notes = "## heading\n\n- **中文 English 123🙂**\n`Ctrl+K`\n" * 300 + "LAST PARAGRAPH"
        entry = release_notes.parse_history("## v0.9.3\n" + notes)[0]
        text = "\n".join(b.text for b in release_notes.release_note_blocks(entry.notes))
        self.assertTrue(text.endswith("LAST PARAGRAPH"))
        self.assertNotIn("**", text)
        self.assertIn("• 中文", text)

    def test_fenced_fake_version_is_not_release(self):
        entries = release_notes.parse_history("## v0.9.3\n```\n## v9.9.9\n```\nend")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].notes.endswith("end"))

    def test_missing_or_invalid_file_gives_current_version_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            for content in (None, b"\xff", b"# empty"):
                if content is not None:
                    path.write_bytes(content)
                entries = release_notes.load_history(path, "0.9.3")
                self.assertEqual(entries[0].version, "0.9.3")
                self.assertIn("没有此版本", entries[0].notes)

    def test_current_version_not_present_does_not_show_wrong_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text("## v0.9.2\nOLD", encoding="utf-8")
            entries = release_notes.load_history(path, "0.9.3")
            self.assertEqual(entries[0].version, "0.9.3")
            self.assertNotIn("OLD", entries[0].notes)
            self.assertEqual(entries[1].notes, "OLD")

    def test_real_changelog_has_installed_and_historical_versions(self):
        entries = release_notes.load_history(Path(__file__).resolve().parents[1]/"CHANGELOG.md", __version__)
        self.assertGreater(len(entries), 5)
        self.assertTrue(any(e.version == __version__ for e in entries))
        self.assertTrue(any(e.version == "0.9.1" for e in entries))

    def test_parser_has_no_tk_app_store_or_network_dependency(self):
        tree = ast.parse(Path(release_notes.__file__).read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imports & {"app", "tkinter", "calendar_core", "update_manager"})

    def test_app_opens_offline_history(self):
        owner = object()
        with patch("app.ReleaseHistoryDialog") as dialog:
            CalendarApp.open_release_history(owner)
        self.assertIs(dialog.call_args.args[0], owner)
        self.assertEqual(dialog.call_args.args[1], __version__)
        self.assertEqual(dialog.call_args.args[2].name, "CHANGELOG.md")

    def test_select_history_replaces_only_document(self):
        entries = release_notes.parse_history("## v0.9.3\nNEW\n## v0.9.2\nOLD")
        owner = SimpleNamespace(entries=entries, version_selector=SimpleNamespace(current=lambda: 1), document=Mock())
        ReleaseHistoryDialog._select_version(owner)
        owner.document.show.assert_called_once_with("OLD", "v0.9.2")

    def test_build_bundles_changelog_and_installed_version(self):
        script = (Path(__file__).resolve().parents[1]/"build_release.ps1").read_text(encoding="utf-8")
        self.assertIn('"--add-data", "$projectDir\\CHANGELOG.md;."', script)
        self.assertIn('"app-version.txt"', script)


class RepairNotesTests(unittest.TestCase):
    def test_version_metadata_unknown_invalid_or_missing_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.assertIsNone(repair.installed_version(path))
            for value in ("bad", "../0.9.3", "0.9.3 trailing", ""):
                (path/"app-version.txt").write_text(value)
                self.assertIsNone(repair.installed_version(path))
            (path/"app-version.txt").write_text("0.9.3\r\n")
            self.assertEqual(repair.installed_version(path), "0.9.3")

    def test_new_equal_and_unknown_version_descriptions(self):
        target = info()
        heading, summary, action = repair.repair_description(target, "0.9.1")
        self.assertIn("发现新版本", heading)
        self.assertIn("0.9.1", summary)
        self.assertEqual(action, "更新")
        heading, _, action = repair.repair_description(target, target.version)
        self.assertIn("当前已是最新版本", heading)
        self.assertIn("修复版本", heading)
        self.assertNotIn("发现新版本", heading)
        self.assertEqual(action, "重新安装")
        heading, _, _ = repair.repair_description(target, None)
        self.assertIn("未知", heading)
        self.assertNotIn("最新版本", heading)

    def test_numeric_comparison_and_no_downgrade(self):
        with patch.object(repair, "__version__", "0.9.3"):
            self.assertIn("发现新版本", repair.repair_description(info("0.10.0"), "0.9.3")[0])
            with self.assertRaises(repair.RepairError):
                repair.repair_description(info("0.9.3"), "0.10.0")
            with self.assertRaises(repair.RepairError):
                repair.repair_description(info("0.9.2"), None)

    def owner(self):
        return SimpleNamespace(messages=queue.Queue(), _confirm_release=Mock(), _show_error=Mock(),
                               _start_download=Mock(), status=Mock(), _busy=Mock(),
                               winfo_exists=lambda: True, after=Mock(), _poll_messages=Mock())

    def test_download_only_after_explicit_acceptance(self):
        for accepted in (False, True):
            owner = self.owner()
            target = info(notes="TARGET END " * 1000)
            owner.messages.put(("release", target))
            owner._confirm_release.return_value = accepted
            repair.RepairWindow._poll_messages(owner)
            owner._confirm_release.assert_called_once_with(target)
            self.assertEqual(owner._start_download.call_count, int(accepted))
            if accepted:
                owner._start_download.assert_called_once_with(target)
            else:
                owner._busy.assert_called_once_with(False)

    def test_network_failure_never_starts_download(self):
        owner = self.owner()
        owner.messages.put(("error", "offline"))
        repair.RepairWindow._poll_messages(owner)
        owner._confirm_release.assert_not_called()
        owner._start_download.assert_not_called()
        owner._show_error.assert_called_once_with("offline")

    def test_downgrade_error_does_not_download(self):
        owner = self.owner()
        owner.messages.put(("release", info()))
        owner._confirm_release.side_effect = repair.RepairError("downgrade")
        repair.RepairWindow._poll_messages(owner)
        owner._start_download.assert_not_called()
        owner._show_error.assert_called_once_with("downgrade")

    def test_close_owns_grab_and_is_idempotent(self):
        dialog = SimpleNamespace(_closing=False, destroy=Mock(), grab_release=Mock())
        dialog.grab_current = lambda: dialog
        repair.RepairNotesDialog.close(dialog)
        repair.RepairNotesDialog.close(dialog, True)
        self.assertFalse(dialog.accepted)
        dialog.grab_release.assert_called_once()
        dialog.destroy.assert_called_once()

    def test_confirmation_passes_exact_target_and_never_loads_main_app(self):
        target = info(notes="TARGET")
        owner = SimpleNamespace(install_dir=Path("missing"), wait_window=Mock())
        with patch.object(repair, "RepairNotesDialog", return_value=SimpleNamespace(accepted=True)) as constructor:
            self.assertTrue(repair.RepairWindow._confirm_release(owner, target))
        self.assertIs(constructor.call_args.args[1], target)
        self.assertNotIn("from app import", Path(repair.__file__).read_text(encoding="utf-8"))
