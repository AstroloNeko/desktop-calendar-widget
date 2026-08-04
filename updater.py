from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "桌面月历更新失败", 0x10)


def wait_for_process(pid: int, timeout_seconds: int = 90) -> None:
    if os.name != "nt":
        time.sleep(2)
        return
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_seconds * 1000)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


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
        safe_extract(args.archive, extracted)
        source = payload_root(extracted)
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
