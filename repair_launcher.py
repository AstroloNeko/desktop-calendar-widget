"""Standalone recovery entry point for an installation whose main app cannot start."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from update_manager import GITHUB_REPOSITORY, UpdateInfo, check_for_update, download_update, version_tuple
from version import __version__


APP_EXE = "DesktopCalendar.exe"
UPDATER_EXE = "DesktopCalendarUpdater.exe"
APP_MUTEX = "Local\\DesktopCalendarWidgetV2"  # Matches win_integration.SingleInstance.
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases/latest"


class RepairError(RuntimeError):
    pass


def repair_action(latest_version: str, installed_version: str = __version__) -> str:
    """The current release may be reinstalled, but never downgrade to an older one."""
    latest = version_tuple(latest_version)
    installed = version_tuple(installed_version)
    if latest < installed:
        raise RepairError("GitHub 上的正式版本比此修复工具更旧，已取消降级。")
    return "更新" if latest > installed else "重新安装"


def validate_installation_directory(directory: Path) -> Path:
    directory = directory.resolve()
    if not (directory / APP_EXE).is_file() and not (directory / "_internal").is_dir():
        raise RepairError("所选文件夹不是桌面月历安装文件夹。")
    return directory


def installation_directory(launcher: Path) -> Path:
    """Prefer the portable installation next to this recovery executable."""
    return validate_installation_directory(launcher.resolve().parent)


def main_app_running() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_mutex(0x00100000, False, APP_MUTEX)  # SYNCHRONIZE
    if not handle:
        return False
    close_handle(handle)
    return True


def stage_verified_updater(archive: Path) -> Path:
    """Take the new updater from the checksum-verified release, not the broken install."""
    app_member = f"DesktopCalendar/{APP_EXE}"
    updater_member = f"DesktopCalendar/{UPDATER_EXE}"
    try:
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            if app_member not in names or updater_member not in names:
                raise RepairError("发布包缺少主程序或更新器。")
            if not any(name.startswith("DesktopCalendar/_internal/") for name in names):
                raise RepairError("发布包缺少必要运行文件。")
            if package.testzip() is not None:
                raise RepairError("发布包损坏，已取消安装。")
            member = package.getinfo(updater_member)
            if member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RepairError("发布包中的更新器无效。")
            staging = Path(tempfile.mkdtemp(prefix="DesktopCalendarRepair-"))
            target = staging / UPDATER_EXE
            try:
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                with target.open("rb") as executable:
                    signature = executable.read(2)
                if signature != b"MZ":
                    raise RepairError("发布包中的更新器不是 Windows 可执行文件。")
                return target
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise RepairError(f"无法读取已下载的发布包：{exc}") from exc


def launch_repair_updater(updater: Path, archive: Path, install_dir: Path) -> None:
    if main_app_running():
        raise RepairError("月历仍在运行。请先从通知区域退出月历，然后重试。")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [
            str(updater),
            "--archive", str(archive),
            "--install-dir", str(install_dir),
            "--pid", str(os.getpid()),
            "--executable", APP_EXE,
        ],
        cwd=str(updater.parent),
        close_fds=True,
        creationflags=flags,
    )


class RepairWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("桌面月历 · 修复与更新")
        self.geometry("410x220")
        self.resizable(False, False)
        self.install_dir: Path | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.status = tk.StringVar(value="主程序无法打开时，可在这里检查更新或重新安装。")
        self.progress = tk.IntVar(value=0)
        self.button = ttk.Button(self, text="检查并修复", command=self.start_check)
        ttk.Label(self, text="桌面月历修复与更新", font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(20, 9))
        ttk.Label(self, textvariable=self.status, wraplength=360, justify="center").pack(pady=5)
        ttk.Progressbar(self, variable=self.progress, maximum=100, length=340).pack(pady=10)
        self.button.pack(pady=5)
        ttk.Button(self, text="打开 GitHub 下载页", command=lambda: webbrowser.open(RELEASES_URL)).pack()
        self.after(80, self._poll_messages)

    def _busy(self, value: bool) -> None:
        self.button.configure(state="disabled" if value else "normal")

    def _run_worker(self, work: Callable[[], None]) -> None:
        def guarded() -> None:
            try:
                work()
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=guarded, name="calendar-repair", daemon=True).start()

    def start_check(self) -> None:
        try:
            if not getattr(sys, "frozen", False):
                raise RepairError("源码运行时不能覆盖安装；请使用发布包中的修复程序。")
            try:
                self.install_dir = installation_directory(Path(sys.executable))
            except RepairError:
                selected = filedialog.askdirectory(parent=self, title="选择桌面月历安装文件夹")
                if not selected:
                    return
                self.install_dir = validate_installation_directory(Path(selected))
            if main_app_running():
                raise RepairError("月历仍在运行。请先从通知区域退出月历，然后重试。")
        except RepairError as exc:
            messagebox.showerror("无法开始修复", str(exc), parent=self)
            return
        self._busy(True)
        self.status.set("正在查询 GitHub 正式版本…")
        self._run_worker(lambda: self.messages.put(("release", check_for_update())))

    def _start_download(self, info: UpdateInfo) -> None:
        self.progress.set(0)
        self.status.set(f"正在下载并校验 {info.tag}…")

        def download() -> None:
            archive = download_update(info, lambda value: self.messages.put(("progress", value)))
            updater = stage_verified_updater(archive)
            self.messages.put(("ready", (updater, archive)))

        self._run_worker(download)

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "progress":
                    self.progress.set(int(payload))
                elif kind == "release":
                    info = payload
                    try:
                        action = repair_action(info.version)
                    except RepairError as exc:
                        self._show_error(str(exc))
                        continue
                    if messagebox.askyesno(
                        "确认修复",
                        f"找到正式版本 {info.tag}。是否{action}程序文件？\n\n"
                        "个人事项、习惯和设置不会被删除。",
                        parent=self,
                    ):
                        self._start_download(info)
                    else:
                        self.status.set("已取消；没有修改程序或个人数据。")
                        self._busy(False)
                elif kind == "ready":
                    updater, archive = payload
                    try:
                        if self.install_dir is None:
                            raise RepairError("尚未确认月历安装位置，请重新开始修复。")
                        launch_repair_updater(updater, archive, self.install_dir)
                    except (OSError, RepairError) as exc:
                        shutil.rmtree(updater.parent, ignore_errors=True)
                        self._show_error(str(exc))
                    else:
                        self.status.set("正在启动更新器，月历会在修复后自动打开。")
                        self.destroy()
                elif kind == "error":
                    self._show_error(str(payload))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll_messages)

    def _show_error(self, message: str) -> None:
        self.status.set("修复未完成；个人数据没有修改。")
        self._busy(False)
        messagebox.showerror("修复失败", message, parent=self)


if __name__ == "__main__":
    RepairWindow().mainloop()
