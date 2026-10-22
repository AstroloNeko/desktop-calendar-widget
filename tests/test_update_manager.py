import hashlib
import ctypes
from ctypes import wintypes
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from update_manager import (
    CHECKSUM_ASSET_NAME,
    RELEASE_ASSET_NAME,
    UpdateError,
    is_newer_version,
    parse_release,
    verify_archive,
    version_tuple,
)
from updater import payload_root, safe_extract, wait_for_process


class UpdateManagerTests(unittest.TestCase):
    def test_version_comparison(self):
        self.assertEqual(version_tuple("v1.2.3"), (1, 2, 3))
        self.assertTrue(is_newer_version("v0.1.1", "0.1.0"))
        self.assertFalse(is_newer_version("v0.1.0", "0.1.0"))

    def test_release_requires_archive_and_checksum(self):
        base = {"tag_name": "v0.2.0", "assets": []}
        with self.assertRaises(UpdateError):
            parse_release(base)

        payload = {
            "tag_name": "v0.2.0",
            "html_url": "https://example.test/release",
            "assets": [
                {"name": RELEASE_ASSET_NAME, "browser_download_url": "https://example.test/app.zip", "size": 12},
                {"name": CHECKSUM_ASSET_NAME, "browser_download_url": "https://example.test/app.sha256"},
            ],
        }
        info = parse_release(payload)
        self.assertEqual(info.version, "0.2.0")
        self.assertEqual(info.asset_size, 12)

    def test_archive_sha256_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "update.zip"
            archive.write_bytes(b"trusted update")
            expected = hashlib.sha256(b"trusted update").hexdigest()
            verify_archive(archive, expected)
            with self.assertRaises(UpdateError):
                verify_archive(archive, "0" * 64)

    def test_safe_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", "bad")
            with self.assertRaises(RuntimeError):
                safe_extract(archive, root / "extract")

    def test_payload_root_accepts_single_top_level_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "DesktopCalendar"
            payload.mkdir()
            self.assertEqual(payload_root(root), payload)

    @unittest.skipUnless(os.name == "nt", "Win32 handle test")
    def test_updater_wait_accepts_pointer_sized_process_handle(self):
        kernel32 = ctypes.windll.kernel32
        with patch.object(kernel32, "OpenProcess", return_value=0x100000000) as open_process, \
             patch.object(kernel32, "WaitForSingleObject", return_value=0) as wait, \
             patch.object(kernel32, "CloseHandle", return_value=1) as close:
            wait_for_process(1234, timeout_seconds=2)
        self.assertEqual(open_process.restype, wintypes.HANDLE)
        self.assertEqual(wait.call_args.args[0].value, 0x100000000)
        self.assertEqual(wait.call_args.args[1], 2000)
        self.assertEqual(close.call_args.args[0].value, 0x100000000)

    @unittest.skipUnless(os.name == "nt", "Win32 wait test")
    def test_updater_refuses_to_copy_while_parent_still_running(self):
        kernel32 = ctypes.windll.kernel32
        with patch.object(kernel32, "OpenProcess", return_value=0x10001), \
             patch.object(kernel32, "WaitForSingleObject", return_value=0x00000102), \
             patch.object(kernel32, "CloseHandle") as close:
            with self.assertRaises(TimeoutError):
                wait_for_process(1234, timeout_seconds=1)
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
