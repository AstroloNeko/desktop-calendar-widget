"""Shared right-hand workspace: the day agenda, chat and quick-add footer."""
import tkinter as tk
from tkinter import ttk

from calendar_core import RoutineItem, WEEKDAYS
from desktop_calendar.assistant.calendar_chat_ui import EmbeddedChat, chat_runtime_for


class GlobalSidebar(tk.Frame):
    DOCK_BELOW = 1000
    MIN_WIDTH = 360
    MAX_WIDTH = 540

    def __init__(self, parent, app, *, button_factory, font_family, snapshot=None):
        self.app = app
        theme, dp = app.theme, app.dpi.px
        bg = theme.workspace_detail
        super().__init__(parent, width=dp(self.MIN_WIDTH), bg=bg, highlightthickness=dp(1),
                         highlightbackground=theme.workspace_grid, highlightcolor=theme.workspace_grid)
        self.pack_propagate(False)
        mode, self.preferred_width, self.drawer_open, self.session_id, self.draft = (
            snapshot or ("day", None, False, None, ""))
        self.mode = "day"
        self.chat = None
        self.day_dirty = True
        self.day = None
        self.workspace_width = 0
        self._drawer = None

        grip = tk.Frame(self, width=dp(4), bg=theme.workspace_grid, cursor="sb_h_double_arrow")
        grip.pack(side="left", fill="y")
        grip.bind("<B1-Motion>", self._resize_from_left)
        body = tk.Frame(self, bg=bg)
        body.pack(side="left", fill="both", expand=True)
        app._build_global_quick_add(body)

        surface = theme.control_background if theme.rounded_widget_shells else bg
        tabs = tk.Frame(body, bg=bg)
        tabs.pack(fill="x", padx=dp(12), pady=(dp(8), dp(5)))
        self.tabs = {}
        for name, text in (("day", "当日详情"), ("assistant", "AI 助理")):
            button = button_factory(tabs, app, text, lambda mode=name: self.switch(mode, focus=True),
                                    width=82, height=23, font_size=8, surface_background=surface, outlined=True)
            button.pack(side="left", padx=(0, dp(4)))
            self.tabs[name] = button

        self.pages = tk.Frame(body, bg=bg)
        self.pages.pack(fill="both", expand=True)
        self.pages.rowconfigure(0, weight=1)
        self.pages.columnconfigure(0, weight=1)
        self.day_page = tk.Frame(self.pages, bg=bg, padx=dp(12))
        self.day_page.grid(row=0, column=0, sticky="nsew")
        self.date_label = tk.Label(self.day_page, bg=bg, fg=theme.text_primary,
                                   font=(font_family, 10, "bold"), anchor="w")
        self.date_label.pack(fill="x", pady=(dp(5), dp(6)))
        scroll = ttk.Scrollbar(self.day_page, orient="vertical", style="Global.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        canvas = tk.Canvas(self.day_page, width=1, height=1, bg=bg, highlightthickness=0,
                           yscrollcommand=scroll.set)
        self.canvas = canvas
        scroll.configure(command=canvas.yview)
        canvas.pack(fill="both", expand=True)
        self.rows = tk.Frame(canvas, bg=bg)
        inner = canvas.create_window(0, 0, window=self.rows, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner, width=e.width))
        self.rows.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", self.scroll_day)
        self.rows.bind("<MouseWheel>", self.scroll_day)
        self.font_family = font_family
        self.switch(mode)

    def switch(self, mode, *, focus=False):
        app = self.app
        if mode == "assistant" and self.chat is None:
            runtime = chat_runtime_for(app)
            if runtime is None:
                self.switch("day")
                return
            self.chat = EmbeddedChat(self.pages, app, runtime, lambda: self.switch("day", focus=True),
                                     session_id=self.session_id, draft=self.draft)
            self.chat.grid(row=0, column=0, sticky="nsew")
        elif mode == "assistant" and self.mode != "assistant":
            self.chat.resume(self.chat.session_id)
        self.mode = mode
        theme = app.theme
        for name, button in self.tabs.items():
            active = name == mode
            if theme.rounded_widget_shells:
                button.segment_active = active
                button.accented = button.outlined = False
                button.foreground = theme.text_primary if active else theme.text_secondary
            else:
                button.accented = active
                button.outlined = not active
            button.draw()
        if mode == "assistant":
            self.day_page.grid_remove()
            self.chat.grid()
            if focus:
                self.chat.input.focus_set()
        else:
            # Both pages retain widget state, but only the active page is mapped
            # so Tab traversal cannot reach hidden inputs or actions.
            self.day_page.grid()
            if self.chat is not None:
                self.chat.grid_remove()
            self.refresh_day()
            if focus:
                app.focus_set()

    def refresh_day(self):
        app = self.app
        day = app.selected
        if self.mode != "day" or (day == self.day and not self.day_dirty):
            return
        different_day = day != self.day
        self.day, self.day_dirty = day, False
        self.date_label.configure(text=f"{day.month}月{day.day}日 · {WEEKDAYS[day.weekday()]}")
        for child in self.rows.winfo_children():
            child.destroy()
        items = app.store.agenda_items_on(day)
        for item in items:
            build = app._build_routine_card if isinstance(item, RoutineItem) else app._build_event_card
            build(item, parent=self.rows, scroll=self.scroll_day)
        if not items:
            theme = app.theme
            empty = tk.Label(self.rows, text="这一天很清静", bg=theme.workspace_detail, fg=theme.text_secondary,
                             font=(self.font_family, 9, "bold"))
            empty.pack(pady=app.dpi.px(20))
            empty.bind("<MouseWheel>", self.scroll_day)
        if different_day:
            self.canvas.yview_moveto(0)

    def scroll_day(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def arrange(self, width):
        self.workspace_width = width
        dp = self.app.dpi.px
        logical = self.app.dpi.logical(width)
        drawer = logical < self.DOCK_BELOW
        if drawer != self._drawer:
            # Keep focused input visible when shrinking across the breakpoint.
            focused = self.focus_get()
            if drawer and self._drawer is False and (
                self.mode == "assistant" or (focused and str(focused).startswith(str(self) + "."))
            ):
                self.drawer_open = True
            if drawer:
                self.grid_forget()
            else:
                self.place_forget()
                self.grid(row=0, column=2, sticky="nsew", padx=(dp(self.app.theme.metrics.workspace_gap), 0))
            self._drawer = drawer
        maximum = min(self.MAX_WIDTH, max(self.MIN_WIDTH, logical - 560)) if not drawer else self.MAX_WIDTH
        preferred = self.preferred_width or min(430, max(self.MIN_WIDTH, logical // 3))
        pixels = min(max(1, width - dp(12)), dp(max(self.MIN_WIDTH, min(maximum, preferred))))
        if not drawer:
            if int(self.cget("width")) != pixels:
                self.configure(width=pixels)
        elif self.drawer_open:
            inset = dp(self.app.theme.metrics.workspace_padding)
            self.place(relx=1, rely=0, anchor="ne", x=-inset, width=pixels, relheight=1)
            self.lift()
        else:
            self.place_forget()

    def _resize_from_left(self, event):
        width = self.winfo_rootx() + self.winfo_width() - event.x_root
        self.preferred_width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, self.app.dpi.logical(width)))
        self.arrange(self.workspace_width)

    def show(self):
        self.drawer_open = True
        self.arrange(self.workspace_width or self.master.winfo_width())

    def toggle_drawer(self):
        if self._drawer:
            self.drawer_open = not self.drawer_open
            self.arrange(self.workspace_width)
            if not self.drawer_open:
                self.app.focus_set()
        else:
            self.switch("day", focus=True)

    def snapshot(self):
        session = self.chat.session_id if self.chat else self.session_id
        draft = self.chat.input.get("1.0", "end-1c") if self.chat else self.draft
        return self.mode, self.preferred_width, self.drawer_open, session, draft
