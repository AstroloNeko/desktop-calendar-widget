"""Opt-in isolated source and frozen-EXE upgrade regression; no network.

Run source GUI: python tools/check_update_compatibility.py
Add --published-zip release/DesktopCalendar-win64.zip --qa-dir build/update-schema-qa/dist/DesktopCalendar
to exercise a published EXE and an unpublished QA payload. All calendar files
and installations are synthetic temporary copies, never the real AppData.
Only test-owned processes with a verified temporary executable path are stopped.
"""
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp, UpdateProgressDialog
from calendar_core import Event, EventCategory, RoutineItem, Store
from data_compat import DataCompatibilityError
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from updater import safe_extract
from update_manager import launch_updater
from update_state import reserve_installation


def fixture(path, schema=8):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": schema, "events": [asdict(Event("original", "Synthetic DDL", "2026-10-23T10:00", event_type="ddl"))],
        "categories": [asdict(EventCategory("work", "Synthetic category", "#6687F2"))],
        "routines": [asdict(RoutineItem("habit", "Synthetic habit", frequency="weekly", weekdays=[0, 2, 4],
            start_on="2026-10-19", workdays_only=False, habit_done=["2026-10-19"]))]}
    if schema == 7:
        for key in ("frequency", "weekdays", "interval", "start_on", "end_on", "workdays_only", "month_day", "year_month"):
            payload["routines"][0].pop(key, None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def source_gui(root):
    path = root / "source" / "calendar_data.json"
    fixture(path)
    errors = []
    with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
        app = CalendarApp(Store(path))
        app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
        try:
            pump(app, .2)
            print("Native source GUI DPI:", dpi_for_window(app), flush=True)
            for theme in ("modern", "aero", "paper", "frutiger"):
                app.set_theme(theme)
                for view in ("compact", "global"):
                    (app.enter_global_view if view == "global" else app.return_to_compact_view)()
                    pump(app)
                    app.update_busy = True
                    app.update_dialog = UpdateProgressDialog(app, "Synthetic update")
                    dialog = app.update_dialog
                    pump(app)
                    before = path.read_bytes()
                    with patch.object(app, "_save_window_settings", side_effect=DataCompatibilityError("Synthetic incompatible schema")), \
                         patch("app.launch_updater") as launch, patch("app.owned_messagebox") as message:
                        app._finish_update_download(root/"not-used.zip", None)
                        launch.assert_not_called()
                        assert message.call_count == 1
                        assert path.read_bytes() == before
                    pump(app)
                    assert not dialog.winfo_exists() and app.update_dialog is None
                    assert not app.update_busy and app.grab_current() is None
                    # Once the event loop resumes, normal delayed geometry
                    # persistence may update settings. It must not modify the
                    # calendar records; command-boundary bytes were checked
                    # above before processing unrelated timer work.
                    original, current = json.loads(before), json.loads(path.read_bytes())
                    for key in ("version", "events", "categories", "routines", "date_states", "notified"):
                        assert original.get(key) == current.get(key), key
                    app.open_editor()
                    pump(app)
                    assert app.editor_window.winfo_ismapped() and app.grab_current() is app.editor_window
                    app.editor_window.close()
                    pump(app)
                    assert app.grab_current() is None
                print(theme, "Compact/Global update failure cleanup, editor owner/grab: PASS", flush=True)
            assert not errors, errors
        finally:
            cleanup(app)


# Private function caches: QA prototypes must not overwrite the application's
# cached ctypes.windll signatures (notably its EnumWindows callback type).
U, K = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
U.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
U.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
U.IsWindowVisible.argtypes = [wintypes.HWND]
U.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
U.GetDpiForWindow.argtypes = [wintypes.HWND]
U.GetDpiForWindow.restype = wintypes.UINT
K.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
K.OpenProcess.restype = wintypes.HANDLE
K.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
K.CloseHandle.argtypes = [wintypes.HANDLE]
K.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
K.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def process_path(pid):
    handle = K.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        return Path(buffer.value) if K.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
    finally:
        K.CloseHandle(handle)


def windows(executable):
    result = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _param):
        if U.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            U.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if process_path(pid.value) == executable.resolve():
                result.append((int(hwnd), pid.value))
        return True
    U.EnumWindows(collect, 0)
    return result


def wait_until(predicate, label, timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("Timed out: " + label)


def stop_owned(executable, root, *owned_pids):
    executable = executable.resolve()
    assert executable.is_relative_to(root.resolve()), "not a synthetic test installation"
    pids = set(owned_pids) | {pid for _hwnd, pid in windows(executable)}
    for pid in pids:
        actual = process_path(pid)
        if actual is None:
            continue  # Already exited; never guess another PID.
        assert actual == executable
        handle = K.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
        if handle:
            try:
                assert K.TerminateProcess(handle, 0), "could not stop owned QA process"
                assert K.WaitForSingleObject(handle, 10000) == 0, "owned QA process did not exit"
            finally:
                K.CloseHandle(handle)


def frozen_checks(root, published_zip, qa_dir, legacy_dir=None):
    from PyInstaller.archive.readers import CArchiveReader
    safe_extract(published_zip, root/"published")
    published = root/"published"/"DesktopCalendar"/"DesktopCalendar.exe"
    pyz = CArchiveReader(str(published)).open_embedded_archive("PYZ.pyz")
    assert pyz.extract("version").co_consts[0] == "0.9.2"
    environment = dict(os.environ, APPDATA=str(root/"roaming"))
    data = root/"roaming"/"DesktopCalendar"/"calendar_data.json"
    fixture(data, 7)
    owned_pids = []
    legacy = None
    try:
        if legacy_dir:
            legacy = root/"legacy091"/"DesktopCalendar.exe"
            legacy.parent.mkdir()
            shutil.copy2(legacy_dir/"DesktopCalendar.exe", legacy)
            shutil.copytree(legacy_dir/"_internal", legacy.parent/"_internal")
            old_pyz = CArchiveReader(str(legacy)).open_embedded_archive("PYZ.pyz")
            assert old_pyz.extract("version").co_consts[0] == "0.9.1"
            for schema in (7, 8):
                fixture(data, schema)
                before = data.read_bytes()
                old = subprocess.Popen([str(legacy)], cwd=legacy.parent, env=environment)
                try:
                    wait_until(lambda: windows(legacy) or old.poll() is not None, "legacy EXE startup")
                    if schema == 8:
                        wait_until(lambda: (data.parent/"calendar.log").exists() or old.poll() is not None,
                                   "legacy Schema8 error recording")
                        assert data.read_bytes() == before
                    else:
                        wait_until(lambda: json.loads(data.read_bytes()).get("settings", {}).get("crisp_text_migrated_v1"),
                                   "legacy Schema7 normal initialization")
                        assert json.loads(data.read_bytes())["version"] == 7
                finally:
                    stop_owned(legacy, root, old.pid)
                    old.wait(10)
            print("Installed v0.9.1 EXE copy: Schema7 reader and Schema8 failure reproduction; future-data bytes unchanged: PASS", flush=True)
            fixture(data, 7)
        for attempt in range(2):
            process = subprocess.Popen([str(published)], cwd=published.parent, env=environment)
            owned_pids.append(process.pid)
            visible = wait_until(lambda: windows(published), "published v0.9.2 visible window")
            if attempt == 0:
                print("Published EXE native window DPI:", U.GetDpiForWindow(visible[0][0]), flush=True)
            wait_until(lambda: json.loads(data.read_bytes()).get("version") == 8, "schema7 migration")
            current = json.loads(data.read_bytes())
            assert current["events"][0]["id"] == "original"
            assert current["routines"][0]["habit_done"] == ["2026-10-19"]
            stop_owned(published, root, process.pid)
            process.wait(10)
        print("Published v0.9.2 EXE: schema7->8, original IDs/history, second startup: PASS", flush=True)
        # The new gate must reject before GUI initialization writes anything.
        fixed = qa_dir.resolve()/"DesktopCalendar.exe"
        fixture(data, 9)
        before = data.read_bytes()
        process = subprocess.Popen([str(fixed)], cwd=fixed.parent, env=environment)
        # QA output is not under temp root. Only stop the process we directly
        # created; do not use the temporary-install helper for this directory.
        try:
            wait_until(lambda: windows(fixed), "fixed EXE incompatible-schema error dialog")
            assert process.poll() is None and data.read_bytes() == before
        finally:
            process.terminate()
            process.wait(10)
        print("QA EXE: Schema9 startup refused; original data bytes unchanged: PASS", flush=True)
        # New startup explains the installation state. Even the unmodified old
        # binary is excluded through its original SingleInstance mutex name.
        with reserve_installation():
            for executable in (fixed, legacy):
                if executable is None:
                    continue
                fixture(data, 8)
                before = data.read_bytes()
                blocked = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment)
                try:
                    wait_until(lambda: windows(executable), "startup blocked during installation")
                    assert data.read_bytes() == before
                finally:
                    blocked.terminate()
                    blocked.wait(10)
        print("Actual old/new EXEs during installation: entry excluded, data unchanged: PASS", flush=True)
        # Preserve Habit2 weekly rules during a real old-process wait, install
        # and restart. Current release is never replaced; only this temp copy.
        fixture(data, 8)
        # If supplied, upgrade the actual old binary copy, not just a
        # new-release reinstall. Its source directory remains untouched.
        parent_executable = legacy or published
        install = parent_executable.parent
        archive = root/"qa-update.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for file in qa_dir.rglob("*"):
                if file.is_file():
                    package.write(file, str(Path("DesktopCalendar")/file.relative_to(qa_dir)))
        parent = subprocess.Popen([str(parent_executable)], cwd=install, env=environment)
        owned_pids.append(parent.pid)
        wait_until(lambda: windows(parent_executable), "parent app for updater wait")
        old_hash = hashlib.sha256(parent_executable.read_bytes()).hexdigest()
        starts = []
        popen = subprocess.Popen
        def start(*args, **kwargs):
            kwargs["env"] = environment
            child = popen(*args, **kwargs)
            starts.append(child)
            return child
        with patch("update_manager.running_as_packaged_app", return_value=True), \
             patch("update_manager.sys.executable", str(parent_executable)), patch("update_manager.os.getpid", return_value=parent.pid), \
             patch("update_manager.subprocess.Popen", side_effect=start):
            launch_updater(archive)
        worker = starts[0]
        staged = Path(worker.args[0])
        try:
            # The updater cannot copy until this exact parent has exited.
            deadline = time.monotonic()+2
            while time.monotonic()<deadline:
                assert parent.poll() is None
                assert hashlib.sha256(parent_executable.read_bytes()).hexdigest() == old_hash
                time.sleep(.05)
            stop_owned(parent_executable, root, parent.pid)
            parent.wait(10)
            worker.wait(60)
            assert worker.returncode == 0, "updater reported failure"
            restarted = wait_until(lambda: windows(parent_executable), "restarted newly installed EXE")
            owned_pids.extend(pid for _hwnd, pid in restarted)
            assert hashlib.sha256(parent_executable.read_bytes()).digest() == hashlib.sha256(fixed.read_bytes()).digest()
            current = json.loads(data.read_bytes())
            assert current["version"] == 8 and current["events"][0]["id"] == "original"
            assert current["routines"][0]["frequency"] == "weekly"
            assert current["routines"][0]["habit_done"] == ["2026-10-19"]
            print("Real QA updater: wait parent, checkpoint, copy, exact-path restart, Habit2 preserved: PASS", flush=True)
        finally:
            stop_owned(parent_executable, root, *owned_pids)
            if worker.poll() is None:
                # Dismiss only this QA updater's own error dialog, if any.
                for hwnd, _pid in windows(staged):
                    U.PostMessageW(hwnd, 0x0010, 0, 0)
                worker.wait(10)
    finally:
        # Recorded PIDs can belong to either install; validate each path before
        # deciding which test-owned process to stop.
        for executable in (published, legacy):
            if executable is not None:
                matching = [pid for pid in owned_pids if process_path(pid) == executable.resolve()]
                stop_owned(executable, root, *matching)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-zip", type=Path)
    parser.add_argument("--qa-dir", type=Path)
    parser.add_argument("--exe-only", action="store_true")
    parser.add_argument("--legacy-dir", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="calendar-update-compat-") as temporary:
        root = Path(temporary)
        if not args.exe_only:
            source_gui(root)
        if args.published_zip and args.qa_dir:
            frozen_checks(root, args.published_zip, args.qa_dir, args.legacy_dir)


if __name__ == "__main__":
    main()
