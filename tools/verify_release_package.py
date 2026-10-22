"""Validate and extract a fresh release without executing any archived code."""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import json
from pathlib import Path
import zipfile

from PyInstaller.archive.readers import CArchiveReader


def manifest(exe):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = (
        ("LoadLibraryExW", [w.LPCWSTR, w.HANDLE, w.DWORD], w.HMODULE),
        ("FindResourceW", [w.HMODULE, ctypes.c_void_p, ctypes.c_void_p], w.HANDLE),
        ("SizeofResource", [w.HMODULE, w.HANDLE], w.DWORD),
        ("LoadResource", [w.HMODULE, w.HANDLE], w.HANDLE),
        ("LockResource", [w.HANDLE], ctypes.c_void_p),
        ("FreeLibrary", [w.HMODULE], w.BOOL),
    )
    for name, args, result in declarations:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    module = kernel.LoadLibraryExW(str(exe), None, 0x22)
    assert module, exe
    try:
        resource = kernel.FindResourceW(module, 1, 24)
        assert resource
        data = kernel.LoadResource(module, resource)
        text = ctypes.string_at(kernel.LockResource(data), kernel.SizeofResource(module, resource)).decode("utf-8")
        assert "PerMonitorV2,PerMonitor" in text.replace(" ", "")
    finally:
        kernel.FreeLibrary(module)


def checksum(file):
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    assert file.with_name(file.name + ".sha256").read_text().strip() == f"{digest}  {file.name}"
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--extract-dir", required=True, type=Path)
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = root / "release/DesktopCalendar-win64.zip"
    digest = checksum(archive)
    target = options.extract_dir.resolve()
    assert target.is_relative_to(root / "build") and target != root / "build"
    target.mkdir(exist_ok=False)  # Never overwrite a previous verification.
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        names = package.namelist()
        for name in names:
            path = Path(name)
            assert (target / path).resolve().is_relative_to(target), name
            assert not set(path.parts) & {"tests", "tools", "__pycache__", ".git", "backups"}, name
            assert not path.name.startswith(("test_", "qa_")), name
            assert "calendar_data.json" not in name.lower(), name
            assert not name.lower().endswith((".log", ".dcalbackup", ".spec")), name
        package.extractall(target)
    app = target / "DesktopCalendar"
    for name in ("DesktopCalendar.exe", "DesktopCalendarUpdater.exe", "DesktopCalendarRepair.exe"):
        executable = app / name
        manifest(executable)
        pyz = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
        assert options.version in pyz.extract("version").co_consts, name
        assert not any(name.startswith(("tests", "tools")) for name in pyz.toc)
        if name == "DesktopCalendar.exe":
            for module in ("habit_schedule", "habit_ui", "bulk_ui", "event_selection", "data_backup",
                           "event_commands", "ui_theme", "dpi_utils", "win_integration"):
                assert module in pyz.toc, module
            constants = pyz.extract("ui_theme").co_consts
            assert all(theme in constants for theme in ("modern", "aero", "paper", "frutiger"))
    for asset in (root / "assets").rglob("*"):
        if asset.is_file():
            assert (app / "_internal/assets" / asset.relative_to(root / "assets")).read_bytes() == asset.read_bytes()
    repair = root / "release/DesktopCalendarRepair.exe"
    repair_digest = checksum(repair)
    assert repair.read_bytes() == (app / repair.name).read_bytes()
    result = dict(version=options.version, zip=str(archive), sha256=digest,
                  repair_sha256=repair_digest, entries=len(names), extracted=str(app), manifest="PerMonitorV2")
    (target / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
