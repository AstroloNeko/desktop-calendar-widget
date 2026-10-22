"""Grouped, virtualized command-palette list. Search ranking is not touched."""

import tkinter as tk
from tkinter import font as tkfont

from interaction_visuals import elide, match_spans, palette_rows
from ui_draw import blend, rounded_rectangle


class PaletteList(tk.Canvas):
    def __init__(self, parent, app, family):
        super().__init__(parent, bg=app.theme.panel_background, bd=0, highlightthickness=0, takefocus=True)
        self.app = app
        self.title_font = tkfont.Font(self, family=family, size=9, weight="bold")
        self.meta_font = tkfont.Font(self, family=family, size=8)
        self.results, self.rows, self.types = (), (), {}
        self.index, self.hover, self.pending, self.query = 0, None, True, ""
        self._draw_job = None
        self.bind("<Configure>", lambda _event: self.reflow())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _event: self._set_hover(None))
        self.bind("<MouseWheel>", self._wheel)
        self.configure(yscrollcommand=self._scrolled)

    def _scrolled(self, first, last):
        if hasattr(self, "scrollbar"):
            self.scrollbar.set(first, last)
        self._schedule_draw()

    def _schedule_draw(self):
        if self._draw_job is None:
            self._draw_job = self.after(16, self._draw)

    def reflow(self):
        self.rows, height = palette_rows(self.results, self.app.dpi.px)
        self.configure(scrollregion=(0, 0, max(1, self.winfo_width()), max(height, self.winfo_height())))
        self._schedule_draw()

    def set_results(self, results, query, types):
        previous = self.results[self.index].key if self.results and self.index < len(self.results) else None
        self.results, self.query, self.types = results, query, types
        self.index = next((i for i, result in enumerate(results) if result.key == previous), 0)
        self.hover, self.pending = None, False
        self.reflow()
        self.see(str(self.index))

    def set_pending(self):
        self.pending = True
        self._schedule_draw()

    def selection(self):
        return (str(self.index),) if self.results else ()

    def selection_set(self, value):
        if not self.results:
            return
        previous = self.index
        self.index = max(0, min(len(self.results) - 1, int(value)))
        for index in (previous, self.index):
            self._paint_background(index)

    def focus(self, value=None):
        return str(self.index)

    def see(self, value):
        row = next((row for row in self.rows if row.index == int(value)), None)
        if row is None:
            return
        top, height = self.canvasy(0), self.winfo_height()
        total = max(1, float(self.cget("scrollregion").split()[3]))
        if row.top < top:
            self.yview_moveto(row.top / total)
        elif row.top + row.height > top + height:
            self.yview_moveto((row.top + row.height - height) / total)

    def identify_row(self, y):
        absolute = self.canvasy(y)
        row = next((row for row in self.rows if row.top <= absolute < row.top + row.height), None)
        return str(row.index) if row is not None and row.index is not None else ""

    def _background(self, index):
        theme = self.app.theme
        if index == self.index:
            return blend(theme.panel_background, theme.accent_soft, .72)
        return theme.schedule_card_hover if index == self.hover else theme.panel_background

    def _paint_background(self, index):
        self.itemconfigure(f"palette-bg:{index}", fill=self._background(index), outline=self._outline(index))

    def _outline(self, index):
        theme = self.app.theme
        return blend(theme.accent, theme.panel_background, .62) if index == self.index else theme.control_border if index == self.hover else self._background(index)

    def _set_hover(self, index):
        if index == self.hover:
            return
        previous, self.hover = self.hover, index
        for value in (previous, index):
            if value is not None:
                self._paint_background(value)

    def _hover(self, event):
        row = self.identify_row(event.y)
        self.configure(cursor="hand2" if row and not self.pending else "")
        self._set_hover(int(row) if row and not self.pending else None)

    def _wheel(self, event):
        self.yview_scroll(-int(event.delta / 120) * 2, "units")
        return "break"

    def _line(self, x, y, text, font, width, color):
        text = elide(text, font, width)
        spans = match_spans(text, self.query) if not self.pending else ()
        half_height = font.metrics("linespace") // 2
        for first, last in spans:
            self.create_rectangle(x + font.measure(text[:first]), y - half_height,
                                  x + font.measure(text[:last]), y + half_height,
                                  fill=self.app.theme.search_match_background, outline="")
        self.create_text(x, y, text=text, fill=color, font=font, anchor="w")
        for first, last in spans:
            self.create_text(x + font.measure(text[:first]), y, text=text[first:last],
                             fill=self.app.theme.search_match_text, font=font, anchor="w")

    def _draw(self):
        self._draw_job = None
        if not self.winfo_exists():
            return
        self.delete("all")
        theme, dp = self.app.theme, self.app.dpi.px
        width, top, bottom = self.winfo_width(), self.canvasy(0), self.canvasy(self.winfo_height())
        if not self.results:
            self.create_text(width / 2, dp(58), text="正在搜索…" if self.pending else "没有找到匹配事项\n试试标题、分类、备注或明确日期",
                             fill=theme.text_muted, font=self.meta_font, justify="center")
        for row in self.rows:
            if row.top + row.height < top or row.top > bottom:
                continue
            if row.index is None:
                self.create_text(dp(12), row.top + dp(15), text=row.label, font=self.meta_font,
                                 fill=theme.text_muted, anchor="w")
                self.create_line(dp(78), row.top + dp(15), width - dp(10), row.top + dp(15), fill=theme.divider)
                continue
            result = self.results[row.index]
            doc = result.document
            rounded_rectangle(self, dp(3), row.top + dp(2), width - dp(3), row.top + row.height - dp(2),
                              dp(theme.metrics.control_radius), fill=self._background(row.index), outline=self._outline(row.index),
                              width=dp(1),
                              tags=f"palette-bg:{row.index}")
            color = theme.event_done if doc and doc.completed else doc.color if doc else theme.accent
            self.create_line(dp(11), row.top + dp(13), dp(11), row.top + row.height - dp(13), fill=color, width=dp(3))
            badge = ""
            if doc:
                badges = []
                if self.types.get(result.key) == "urgent":
                    badges.append("紧急")
                if doc.deadline:
                    badges.append("◆ DDL")
                if doc.completed:
                    badges.append("✓ 已完成")
                badge = " · ".join(badges)
            else:
                badge = "↵"
            badge_width = self.meta_font.measure(badge) + dp(14) if badge else 0
            title_color = theme.text_muted if self.pending else theme.text_done if doc and doc.completed else theme.text_primary
            self._line(dp(24), row.top + dp(19), result.title, self.title_font,
                       width - dp(40) - badge_width, title_color)
            if badge:
                self.create_text(width - dp(13), row.top + dp(19), text=badge, font=self.meta_font, anchor="e",
                                 fill=theme.text_done if doc and doc.completed else theme.event_type_ddl if doc and doc.deadline else theme.event_type_urgent if doc else theme.text_muted)
            detail = result.detail
            if doc:
                # Type/completion has its own quiet semantic marker above.
                detail = f"{doc.category} · {doc.start:%Y-%m-%d}"
                if doc.end != doc.start:
                    detail += f" → {doc.end:%Y-%m-%d}"
            self._line(dp(24), row.top + dp(40 if doc else 34), detail, self.meta_font,
                       width - dp(40), theme.text_muted if self.pending else theme.text_secondary)

    def destroy(self):
        if self._draw_job:
            self.after_cancel(self._draw_job)
            self._draw_job = None
        super().destroy()
        self.title_font = None
        self.meta_font = None
