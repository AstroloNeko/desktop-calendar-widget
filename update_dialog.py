"""Presentation-only release notes dialog. Remote content is never executed."""
from __future__ import annotations

from dataclasses import dataclass
import re
import tkinter as tk
from tkinter import ttk

from dpi_utils import WorkArea
from update_manager import UpdateInfo


@dataclass(frozen=True)
class NoteBlock:
    text: str
    style: str = "body"


def _inline_text(text: str) -> str:
    # Preserve inline code literally; render other common Markdown as plain text.
    parts = re.split(r"(`+[^`\n]*`+)", text)
    for index, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`"):
            parts[index] = part.strip("`")
        else:
            part = re.sub(r"!?\[([^\]]*)\]\(([^)]*)\)", r"\1 (\2)", part)
            part = re.sub(r"(\*\*|__)(.+?)\1", r"\2", part)
            parts[index] = re.sub(r"\\([\\`*_{}\[\]()#+.!>-])", r"\1", part)
    return "".join(parts)


def release_note_blocks(notes: str) -> tuple[NoteBlock, ...]:
    """No length limit: retain every paragraph, including the final one.

    This deliberately small Markdown-to-readable-text converter is not HTML or
    a browser renderer. Unknown syntax remains inert text, never a command/link.
    """
    if not notes.strip():
        return (NoteBlock("这个版本没有附加说明。"),)
    blocks = []
    fenced = False
    for line in notes.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced:
            blocks.append(NoteBlock(line, "code"))
            continue
        heading = re.match(r"^\s{0,3}(#{1,3})\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            blocks.append(NoteBlock(_inline_text(heading[2]), f"h{len(heading[1])}"))
            continue
        bullet = re.match(r"^(\s*)[-+*]\s+(.+)$", line)
        if bullet:
            line = bullet[1] + "• " + bullet[2]
        blocks.append(NoteBlock(_inline_text(line)))
    return tuple(blocks)


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

    def __init__(self, master, info: UpdateInfo, current_version: str, font_family: str) -> None:
        super().__init__(master)
        self.withdraw()
        self.master_app = master
        self.accepted = False
        self._closing = False
        theme = master.theme
        px = master.dpi.px
        self.title("发现桌面月历更新")
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
            self.actions, text="稍后再说", command=self.close,
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
        self.update_button.pack(side="right")
        self.later_button.pack(side="right", padx=(0, px(8)))
        header = tk.Frame(self, bg=theme.panel_background)
        header.pack(side="top", fill="x", padx=px(16), pady=(px(14), px(10)))
        tk.Label(header, text="发现桌面月历新版本", anchor="w",
                 bg=theme.panel_background, fg=theme.text_primary,
                 font=(font_family, 13, "bold")).pack(fill="x")
        tk.Label(header, text=f"当前 v{current_version}  →  {info.tag}", anchor="w",
                 bg=theme.panel_background, fg=theme.text_secondary,
                 font=(font_family, 9)).pack(fill="x", pady=(px(4), 0))
        # The full release title and body both live in the scrollable document.
        document = tk.Frame(self, bg=theme.panel_background)
        document.pack(fill="both", expand=True, padx=px(16))
        self.notes = tk.Text(
            document, wrap="word", height=1, width=1, undo=False,
            bg=theme.input_background, fg=theme.text_primary,
            selectbackground=theme.accent, selectforeground=theme.text_on_accent,
            relief="flat", borderwidth=0, highlightthickness=px(1),
            highlightbackground=theme.input_border, highlightcolor=theme.input_focus,
            padx=px(12), pady=px(10), font=(font_family, 9),
            spacing1=px(2), spacing3=px(3), cursor="arrow",
        )
        self.scrollbar = ttk.Scrollbar(document, orient="vertical", command=self.notes.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.notes.pack(side="left", fill="both", expand=True)
        self.notes.configure(yscrollcommand=self.scrollbar.set)
        for level, size in ((1, 12), (2, 11), (3, 10)):
            self.notes.tag_configure(f"h{level}", font=(font_family, size, "bold"),
                                     spacing1=px(9), spacing3=px(5))
        self.notes.tag_configure("code", font=("Consolas", 9))
        if info.name and info.name != info.tag:
            self.notes.insert("end", _inline_text(info.name) + "\n\n", "h1")
        for block in release_note_blocks(info.notes):
            self.notes.insert("end", block.text + "\n", block.style)
        self.notes.configure(state="disabled")
        self.notes.bind("<MouseWheel>", self._wheel)
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
