"""Global workspace chrome. Presentation only; commands remain on CalendarApp."""
from __future__ import annotations

import tkinter as tk


HEADER_HEIGHT = 58


def header_layout(width: int) -> dict[str, tuple[int, int, int, int]]:
    """Logical-pixel bounds; reserve a real drag gap even at minimum width."""
    compact = width < 900
    padding, gap = (8, 3) if compact else (12, 5)
    title_width = 150 if compact else 192
    button_height = 32
    y = (HEADER_HEIGHT - button_height) // 2
    result = {"title": (padding, 4, title_width, 50)}
    x = padding + title_width + gap
    for key in ("previous", "today", "following"):
        result[key] = (x, y, 30 if compact else 32, button_height)
        x += result[key][2] + gap
    navigation_end = x
    x = width - padding
    for key in ("minimize", "compact"):
        x -= 34
        result[key] = (x, y, 34, button_height)
        x -= gap
    x -= gap
    widths = (80, 42, 58, 62, 30) if compact else (90, 48, 64, 72, 32)
    for key, size in reversed(tuple(zip(("create", "habits", "ddl", "pin", "more"), widths))):
        x -= size
        result[key] = (x, y, size, button_height)
        x -= gap
    result["drag"] = (navigation_end, 0, max(0, x - navigation_end), HEADER_HEIGHT)
    return result


class GlobalHeader(tk.Frame):
    def __init__(self, parent, app, *, button_factory, tooltip_factory, font_family):
        theme = app.theme
        super().__init__(parent, bg=theme.header_background, height=app.dpi.px(HEADER_HEIGHT))
        self.app = app
        self.pack_propagate(False)
        self.month = tk.Label(self, text="", bg=theme.header_background,
                              fg=theme.header_text, font=(font_family, 14, "bold"), anchor="w")
        self.subtitle = tk.Label(self, text="", bg=theme.header_background,
                                 fg=theme.header_subtext, font=(font_family, 8), anchor="w")
        commands = (
            ("previous", "‹", lambda: app.change_month(-1), "上个月"),
            ("today", "今", app.go_today, "返回今天"),
            ("following", "›", lambda: app.change_month(1), "下个月"),
            ("create", "＋ 新建事项", app.open_new_event, "新建事项"),
            ("habits", "习惯", app.open_routine_manager, "习惯管理"),
            ("ddl", "DDL列表", app.open_ddl_list, "查看全部DDL事项"),
            ("pin", "窗口置顶", app.toggle_window_mode,
             lambda: "取消置顶" if app.window_mode == "pinned" else "窗口置顶"),
            ("more", "…", app.show_main_menu, "更多选项"),
            ("compact", "", app.return_to_compact_view, "返回紧凑视图"),
            ("minimize", "−", app.minimize_global, "最小化到任务栏"),
        )
        self.controls = {}
        self.tooltips = []
        for key, text, command, tip in commands:
            button = button_factory(self, app, text, command, width=34, height=32,
                                    font_size=13 if key in ("previous", "following", "minimize") else 8,
                                    surface_background=theme.header_background,
                                    accented=key == "create", outlined=key in ("habits", "ddl", "pin"),
                                    icon="compact" if key == "compact" else None)
            button.bind("<Configure>", lambda _e, control=button: control.draw(), add="+")
            self.controls[key] = button
            self.tooltips.append(tooltip_factory(button, tip))
        # Whitelist only blank chrome and title labels. Buttons are never drag targets.
        for area in (self, self.month, self.subtitle):
            area.bind("<ButtonPress-1>", self._start_drag)
            area.bind("<B1-Motion>", app._drag_window)
            area.bind("<ButtonRelease-1>", app._end_drag)
        self.bind("<Configure>", self._layout)
        self.bind("<Unmap>", lambda _e: self.hide_tooltips(), add="+")
        self.bind("<Destroy>", lambda e: self.hide_tooltips() if e.widget is self else None, add="+")

    def _start_drag(self, event):
        if self.app._active_modal_window() is None and self.app.state() == "normal":
            self.hide_tooltips()
            self.app._start_drag(event)

    def _layout(self, _event=None):
        dp = self.app.dpi.px
        bounds = header_layout(self.app.dpi.logical(self.winfo_width()))
        x, y, width, _ = bounds["title"]
        self.month.place(x=dp(x), y=dp(y), width=dp(width), height=dp(28))
        self.subtitle.place(x=dp(x), y=dp(y + 29), width=dp(width), height=dp(18))
        for key, control in self.controls.items():
            x, y, width, height = bounds[key]
            control.place(x=dp(x), y=dp(y), width=dp(width), height=dp(height))

    def hide_tooltips(self):
        for tooltip in self.tooltips:
            tooltip.hide()
