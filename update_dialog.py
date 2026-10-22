"""Presentation-only release notes dialog. Remote content is never executed."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from pathlib import Path

from dpi_utils import WorkArea
from update_manager import UpdateInfo
from release_notes import NoteBlock, inline_text as _inline_text, release_note_blocks, load_history
from release_notes_ui import NotesDocument


def dialog_geometry(area: WorkArea, dpi: int, parent_rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Bound device-pixel size and position to the owner's monitor work area."""
    scale = max(96, dpi) / 96
    # Reserve room for native frame/caption chrome too, on small monitors.
    margin = min(round(24 * scale), max(0, (min(area.width, area.height) - 1) // 2))
    width = min(round(540 * scale), max(1, area.width - 2 * margin))
    height = min(round(520 * scale), max(1, area.height - 2 * margin))
    px, py, pw, ph = parent_rect
    x = min(max(px + (pw - width) // 2, area.left + margin), area.right - width - margin)
    y = min(max(py + (ph - height) // 2, area.top + margin), area.bottom - height - margin)
    return width, height, x, y


class UpdateAvailableDialog(tk.Toplevel):
    """Shared modal ownership; fixed actions outside a readonly scrollable Text."""

    def __init__(self, master, info: UpdateInfo, current_version: str, font_family: str,
                 *, history: bool = False) -> None:
        super().__init__(master)
        self.withdraw()
        self.master_app = master
        self.accepted = False
        self._closing = False
        theme = master.theme
        px = master.dpi.px
        self.title("版本与更新日志" if history else "发现桌面月历更新")
        self.configure(bg=theme.panel_background)
        area = master.dpi.work_area()
        width, height, x, y = dialog_geometry(
            area, master.dpi.dpi,
            (master.winfo_rootx(), master.winfo_rooty(), master.winfo_width(), master.winfo_height()),
        )
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, px(320)), min(height, px(260)))
        self.maxsize(max(width, area.width - px(48)), max(height, area.height - px(48)))

        # Pack fixed chrome FIRST so growing notes cannot displace the actions.
        self.actions = tk.Frame(self, bg=theme.panel_background)
        self.actions.pack(side="bottom", fill="x", padx=px(16), pady=px(12))
        self.later_button = tk.Button(
            self.actions, text="关闭" if history else "稍后再说", command=self.close,
            bg=theme.control_background, fg=theme.control_text,
            activebackground=theme.control_hover, activeforeground=theme.control_text,
            relief="flat", font=(font_family, 9), padx=px(14), pady=px(7),
        )
        self.update_button = tk.Button(
            self.actions, text="立即更新", command=lambda: self.close(True),
            bg=theme.accent, fg=theme.text_on_accent,
            activebackground=theme.accent_hover, activeforeground=theme.text_on_accent,
            relief="flat", font=(font_family, 9, "bold"), padx=px(14), pady=px(7),
        )
        if not history:
            self.update_button.pack(side="right")
        self.later_button.pack(side="right", padx=(0, px(8)))
        header = tk.Frame(self, bg=theme.panel_background)
        header.pack(side="top", fill="x", padx=px(16), pady=(px(14), px(10)))
        tk.Label(header, text="桌面月历 Desktop Calendar" if history else "发现桌面月历新版本", anchor="w",
                 bg=theme.panel_background, fg=theme.text_primary,
                 font=(font_family, 13, "bold")).pack(fill="x")
        tk.Label(header, text=f"当前安装版本 v{current_version}" if history else f"当前 v{current_version}  →  {info.tag}", anchor="w",
                 bg=theme.panel_background, fg=theme.text_secondary,
                 font=(font_family, 9)).pack(fill="x", pady=(px(4), 0))
        if not history:
            tk.Label(header, text="来源：GitHub 目标版本 Release Notes", anchor="w",
                     bg=theme.panel_background, fg=theme.text_muted,
                     font=(font_family, 8)).pack(fill="x", pady=(px(4), 0))
        # The full release title and body both live in the scrollable document.
        self.document = NotesDocument(self, theme, px, font_family)
        self.document.pack(fill="both", expand=True, padx=px(16))
        self.notes = self.document.text
        self.scrollbar = self.document.scrollbar
        self.document.show(info.notes, info.name if info.name != info.tag else "")
        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        master.present_modal(self, master)
        self.grab_set()
        self.later_button.focus_set()

    def _wheel(self, event):
        delta = int(event.delta)
        if delta:
            units = max(1, abs(delta) // 120)
            self.notes.yview_scroll(-units if delta > 0 else units, "units")
        return "break"

    def close(self, accepted: bool = False) -> None:
        if self._closing:
            return
        self._closing = True
        self.accepted = accepted
        if self.grab_current() is self:
            self.grab_release()
        self.master_app._leave_modal(self)
        self.destroy()
        # The app's registered-overlay destroy hook restores the saved band.


class ReleaseHistoryDialog(UpdateAvailableDialog):
    """Offline history with the existing app's modal lifecycle and Text renderer."""

    def __init__(self, master, current_version: str, path: Path, font_family: str):
        self.entries = load_history(path, current_version)
        current = next(entry for entry in self.entries if entry.version == current_version)
        info = UpdateInfo(f"v{current.version}", current.version, current.title, current.notes, "", "", "")
        super().__init__(master, info, current_version, font_family, history=True)
        px, theme = master.dpi.px, master.theme
        selector = tk.Frame(self, bg=theme.panel_background)
        selector.pack(fill="x", padx=px(16), pady=(0, px(8)), before=self.document)
        tk.Label(selector, text="查看版本", bg=theme.panel_background,
                 fg=theme.text_secondary, font=(font_family, 9)).pack(side="left")
        self.version_selector = ttk.Combobox(selector, state="readonly",
                                            values=[entry.title for entry in self.entries])
        self.version_selector.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
        self.version_selector.current(self.entries.index(current))
        self.version_selector.bind("<<ComboboxSelected>>", self._select_version)
        tk.Label(selector, text="来源：本地随附 CHANGELOG · 离线可用", anchor="w",
                 bg=theme.panel_background, fg=theme.text_muted,
                 font=(font_family, 8)).pack(side="bottom", fill="x", pady=(px(5), 0))

    def _select_version(self, _event=None):
        entry = self.entries[self.version_selector.current()]
        self.document.show(entry.notes, entry.title)
