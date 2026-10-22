"""Standalone recovery entry point for an installation whose main app cannot start."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from update_manager import GITHUB_REPOSITORY, UpdateInfo, check_for_update, download_update, version_tuple
from version import __version__
from update_payload import PayloadError, stage_verified_updater as _stage_verified_updater
from dpi_utils import dpi_for_window, work_area_for_window
from release_notes_ui import NotesDocument
from update_dialog import dialog_geometry
from ui_theme import get_theme


APP_EXE = "DesktopCalendar.exe"
UPDATER_EXE = "DesktopCalendarUpdater.exe"
APP_MUTEX = "Local\\DesktopCalendarWidgetV2"  # Matches win_integration.SingleInstance.
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases/latest"


class RepairError(RuntimeError):
    pass


def repair_window_size(dpi: float, requested_width: int, requested_height: int) -> tuple[int, int]:
    """Leave enough room for native controls at the current Windows scaling."""
    scale = max(1.0, dpi / 96.0)
    margin = round(24 * scale)
    return (
        max(round(410 * scale), requested_width + margin),
        max(round(220 * scale), requested_height + margin),
    )


def repair_action(latest_version: str, installed_version: str = __version__) -> str:
    """The current release may be reinstalled, but never downgrade to an older one."""
    latest = version_tuple(latest_version)
    installed = version_tuple(installed_version)
    if latest < installed:
        raise RepairError("GitHub 上的正式版本比此修复工具更旧，已取消降级。")
    return "更新" if latest > installed else "重新安装"


def installed_version(directory: Path) -> str | None:
    """Read program-only build metadata; never open calendar data or execute EXEs."""
    try:
        value = (directory / "app-version.txt").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if re.fullmatch(r"\d+\.\d+\.\d+", value) else None


def repair_description(info: UpdateInfo, local: str | None) -> tuple[str, str, str]:
    # Preserve the existing no-downgrade guard against this tool's own version,
    # and additionally protect a newer installation when metadata is available.
    repair_action(info.version, __version__)
    action = repair_action(info.version, local) if local else "修复 / 更新"
    if local is None:
        heading = f"修复版本 {info.tag}（已安装版本未知）"
    elif version_tuple(info.version) > version_tuple(local):
        heading = f"发现新版本 {info.tag}"
    else:
        heading = f"当前已是最新版本 · 修复版本 {info.tag}"
    summary = f"本地 v{local} → {info.tag}" if local else f"已安装版本无法确认 · 修复工具 v{__version__} → 目标 {info.tag}"
    return heading, summary, action


class RepairNotesDialog(tk.Toplevel):
    """Independent confirmation: no main app, calendar Store or network calls."""

    def __init__(self, master, info: UpdateInfo, local: str | None):
        heading, summary, action = repair_description(info, local)
        super().__init__(master)
        self.withdraw()
        self.accepted = False
        self._closing = False
        self.title("确认修复 / 更新")
        dpi = dpi_for_window(master)
        px = lambda value: round(value * dpi / 96)
        area = work_area_for_window(master)
        w, h, x, y = dialog_geometry(area, dpi, (master.winfo_rootx(), master.winfo_rooty(),
                                                master.winfo_width(), master.winfo_height()))
        self.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self.minsize(min(w, px(320)), min(h, px(260)))
        self.maxsize(max(w, area.width - px(48)), max(h, area.height - px(48)))
        theme = get_theme("modern")
        self.configure(bg=theme.panel_background)
        actions = ttk.Frame(self, padding=px(12))
        actions.pack(side="bottom", fill="x")
        self.confirm_button = ttk.Button(actions, text=f"确认{action}", command=lambda: self.close(True))
        self.confirm_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="稍后再说", command=self.close)
        self.cancel_button.pack(side="right", padx=px(8))
        header = ttk.Frame(self, padding=px(12))
        header.pack(side="top", fill="x")
        ttk.Label(header, text=heading, wraplength=w - px(48), font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(header, text=summary, wraplength=w - px(48)).pack(anchor="w", pady=px(4))
        ttk.Label(header, text="来源：GitHub 目标版本 Release Notes · 确认后才下载", wraplength=w - px(48)).pack(anchor="w")
        self.document = NotesDocument(self, theme, px)
        self.document.pack(fill="both", expand=True, padx=px(12))
        self.document.show(info.notes, info.name if info.name != info.tag else "")
        self.notes = self.document.text
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.deiconify()
        self.lift()
        self.grab_set()
        self.cancel_button.focus_set()

    def close(self, accepted=False):
        if self._closing:
            return
        self._closing = True
        self.accepted = accepted
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()


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
    try:
        return _stage_verified_updater(archive)
    except PayloadError as exc:
        raise RepairError(str(exc)) from exc


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
        self.resizable(False, False)
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        dp = lambda value: round(value * scale)
        self.install_dir: Path | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.status = tk.StringVar(value="主程序无法打开时，可在这里检查更新或重新安装。")
        self.progress = tk.IntVar(value=0)
        content = ttk.Frame(self, padding=(dp(18), dp(12)))
        content.pack(fill="both", expand=True)
        self.button = ttk.Button(content, text="检查并修复", command=self.start_check)
        ttk.Label(content, text="桌面月历修复与更新", font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(dp(8), dp(9)))
        ttk.Label(content, textvariable=self.status, wraplength=dp(360), justify="center").pack(pady=dp(5))
        ttk.Progressbar(content, variable=self.progress, maximum=100, length=dp(340)).pack(pady=dp(10))
        self.button.pack(pady=dp(5))
        self.download_button = ttk.Button(content, text="打开 GitHub 下载页", command=lambda: webbrowser.open(RELEASES_URL))
        self.download_button.pack()
        self.update_idletasks()
        width, height = repair_window_size(self.winfo_fpixels("1i"), self.winfo_reqwidth(), self.winfo_reqheight())
        self.geometry(f"{width}x{height}")
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

    def _confirm_release(self, info: UpdateInfo) -> bool:
        local = installed_version(self.install_dir) if self.install_dir else None
        dialog = RepairNotesDialog(self, info, local)
        self.wait_window(dialog)
        return dialog.accepted

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "progress":
                    self.progress.set(int(payload))
                elif kind == "release":
                    info = payload
                    try:
                        confirmed = self._confirm_release(info)
                    except RepairError as exc:
                        self._show_error(str(exc))
                        continue
                    if confirmed:
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
