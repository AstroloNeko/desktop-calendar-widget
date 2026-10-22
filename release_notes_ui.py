"""Shared readonly Text/Tag presentation. Creates no root and reads no data."""
import tkinter as tk
from tkinter import ttk

from release_notes import inline_text, release_note_blocks


class NotesDocument(tk.Frame):
    def __init__(self, master, theme, px, font_family="Microsoft YaHei UI"):
        super().__init__(master, bg=theme.panel_background)
        self.text = tk.Text(
            self, wrap="word", height=1, width=1, undo=False,
            bg=theme.input_background, fg=theme.text_primary,
            selectbackground=theme.accent, selectforeground=theme.text_on_accent,
            relief="flat", borderwidth=0, highlightthickness=px(1),
            highlightbackground=theme.input_border, highlightcolor=theme.input_focus,
            padx=px(12), pady=px(10), font=(font_family, 9),
            spacing1=px(2), spacing3=px(3), cursor="arrow",
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        for level, size in ((1, 12), (2, 11), (3, 10)):
            self.text.tag_configure(f"h{level}", font=(font_family, size, "bold"),
                                    spacing1=px(9), spacing3=px(5))
        self.text.tag_configure("code", font=("Consolas", 9))
        self.text.bind("<MouseWheel>", self._wheel)

    def show(self, notes, title=""):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if title:
            self.text.insert("end", inline_text(title) + "\n\n", "h1")
        for block in release_note_blocks(notes):
            self.text.insert("end", block.text + "\n", block.style)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _wheel(self, event):
        delta = int(event.delta)
        if delta:
            self.text.yview_scroll((-1 if delta > 0 else 1) * max(1, abs(delta) // 120), "units")
        return "break"
