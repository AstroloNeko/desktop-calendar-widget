import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from update_manager import (
    CHECKSUM_ASSET_NAME,
    RELEASE_ASSET_NAME,
    UpdateError,
    is_newer_version,
    parse_release,
    verify_archive,
    version_tuple,
)
from updater import payload_root, safe_extract


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


if __name__ == "__main__":
    unittest.main()
