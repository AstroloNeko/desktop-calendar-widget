from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from data_backup import automatic_backup
from update_state import reserve_installation


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "桌面月历更新失败", 0x10)


def wait_for_process(pid: int, timeout_seconds: int = 90) -> None:
    if os.name != "nt":
        time.sleep(2)
        return
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(SYNCHRONIZE, False, pid)
    if not handle:
        error = kernel32.GetLastError()
        if error not in (0, 87):  # ERROR_INVALID_PARAMETER: process already gone.
            raise OSError(error, "无法确认旧进程已退出，已取消更新。")
        return
    try:
        result = wait(wintypes.HANDLE(handle), timeout_seconds * 1000)
        if result == 0x00000102:  # WAIT_TIMEOUT
            raise TimeoutError("月历或修复工具尚未退出，已取消覆盖运行中的程序。")
        if result != 0:
            raise OSError("无法可靠等待旧进程退出，已取消更新。")
    finally:
        close_handle(wintypes.HANDLE(handle))


def ensure_no_calendar_instance() -> None:
    """The parent may be a repair tool; another calendar must not be ignored."""
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_mutex.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = open_mutex(0x00100000, False, "Local\\DesktopCalendarWidgetV2")
    if handle:
        close(handle)
        raise RuntimeError("另一个月历实例仍在运行，更新尚未完成。请退出该实例后重试；未覆盖程序或日历数据。")
    error = kernel32.GetLastError()
    if error not in (0, 2):  # ERROR_FILE_NOT_FOUND: no named instance.
        raise OSError(error, "无法确认其他月历实例状态，已取消更新。")


def safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError("更新包包含不安全的文件路径。") from exc
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("更新包不能包含符号链接。")
        package.extractall(destination)


def payload_root(extracted: Path) -> Path:
    children = [item for item in extracted.iterdir() if item.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extracted


def copy_payload(source: Path, destination: Path, executable_name: str) -> None:
    if not (source / executable_name).is_file():
        raise RuntimeError(f"更新包中没有 {executable_name}。")
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for _attempt in range(12):
            try:
                shutil.copy2(item, target)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(0.5)
        if last_error:
            raise last_error


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--executable", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extracted = Path(tempfile.mkdtemp(prefix="DesktopCalendarExtract-"))
    try:
        wait_for_process(args.pid)
        ensure_no_calendar_instance()
        with reserve_installation():
            safe_extract(args.archive, extracted)
            source = payload_root(extracted)
            data_file = Path(os.getenv("APPDATA", str(Path.home()))) / "DesktopCalendar" / "calendar_data.json"
            automatic_backup(data_file, reason="before-update", force=True)
            copy_payload(source, args.install_dir, args.executable)
        executable = args.install_dir / args.executable
        subprocess.Popen([str(executable)], cwd=str(args.install_dir), close_fds=True)
        args.archive.unlink(missing_ok=True)
        return 0
    except Exception as exc:
        show_error(str(exc))
        return 1
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
