"""Shared Compact/Global command palette. No task CRUD or date algorithms."""

import tkinter as tk
from tkinter import ttk

from event_search import EventSearchService
from dpi_utils import LogicalCanvas
from search_palette_ui import PaletteList
from ui_draw import draw_control_icon


class GlobalSearchMixin:
    def open_search(self):
        window = self.search_window
        if window and window.winfo_exists():
            window.present()
            return "break"
        # Do not steal a modal editor's grab or discard its unsaved text.
        owner = self._active_modal_window()
        if owner is not None:
            owner.focus_set()
            return "break"
        self._prepare_modal_open()
        self._cancel_flow_drag(redraw=False)
        if self.search_service is None:
            self.search_service = EventSearchService()
        self.search_window = SearchDialog(self, font=self.search_font, center=self.search_center)
        self.search_window.present()
        return "break"

    def _invalidate_search(self):
        service = getattr(self, "search_service", None)
        if service is not None:
            service.invalidate()
        window = getattr(self, "search_window", None)
        if window and window.winfo_exists():
            window.request()

    def _activate_search_result(self, result):
        if result.kind == "date":
            self.select_day(result.target_date)
            return
        if result.kind == "event":
            event = self.store.event_by_id(result.key)
            if event is None:
                self._invalidate_search()
                return
            self.select_day(self.store.event_start_date(event))
            if self.view_mode == "global":
                self._search_reveal_event_id = event.id
                self._render_global_timeline(refresh_sidebar=False, select_event_id=event.id)
                self._scroll_search_target_into_view(event.id)
                if hasattr(self, "_flash_event"):
                    self._flash_event(event.id)
            # Opening the existing editor guarantees access even when Flow
            # packs the task below '+更多', or Compact's list is collapsed.
            self.open_editor(event)
            return
        if result.key == "new":
            self.open_new_event()
        elif result.key == "today":
            self.go_today()
        elif result.key == "ddl":
            self.open_ddl_list()
        elif result.key == "categories":
            self.open_category_manager()
        elif result.key in ("flow", "timeline"):
            self.enter_global_view()
            self.set_global_display_mode(result.key)
        elif result.key == "date":
            self.open_search()
            self.search_window.hint.configure(text="输入明确日期，例如 2026-11-05，然后按 Enter")

    def _scroll_search_target_into_view(self, event_id):
        if self.global_display_mode == "flow":
            blocks = self.calendar_flow_layout.block_by_item_id(event_id)
            if blocks:
                height = max(1, len(self.calendar_flow_layout.weeks) * self._global_flow_week_height)
                self.global_flow_canvas.yview_moveto(blocks[0].week_index * self._global_flow_week_height / height)
        else:
            row = next((index for index, item in enumerate(self.timeline_model.items) if item.id == event_id), 0)
            self.global_timeline_canvas.yview_moveto(row / max(1, len(self.timeline_model.items)))
            item = self.timeline_model.item_by_id(event_id)
            if item:
                self._global_xview("moveto", (item.visible_start - self.timeline_model.period_start).days / len(self.timeline_model.days))
            self._refresh_timeline_rows()


class SearchDialog(tk.Toplevel):
    def __init__(self, app, *, font, center):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self._closed = False
        self._debounce = None
        self._poll_job = None
        self._results = ()
        self._pending = True
        self._activate_when_ready = False
        self._activation_job = None
        self._previous_focus = app.focus_get()
        self.title("全局搜索 · 快捷命令")
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        theme, dp = app.theme, app.dpi.px
        self.configure(bg=theme.panel_background)
        self.minsize(dp(300), dp(240))
        shell = tk.Frame(self, bg=theme.panel_background, padx=dp(14), pady=dp(12))
        shell.pack(fill="both", expand=True)
        heading = tk.Frame(shell, bg=theme.panel_background)
        heading.pack(fill="x", pady=(0, dp(10)))
        tk.Label(heading, text="搜索事项与命令", anchor="w", bg=theme.panel_background,
                 fg=theme.text_primary, font=(font, 10, "bold")).pack(side="left")
        tk.Label(heading, text="ESC 关闭", bg=theme.panel_background,
                 fg=theme.text_muted, font=(font, 8)).pack(side="right")
        self.query = tk.StringVar()
        self.input_shell = tk.Frame(shell, bg=theme.input_background, padx=dp(5), pady=dp(4),
                                    highlightthickness=dp(1), highlightbackground=theme.input_border,
                                    highlightcolor=theme.input_focus)
        self.input_shell.pack(fill="x", pady=(0, dp(7)))
        search_icon = LogicalCanvas(self.input_shell, dpi=app.dpi, width=26, height=28,
                                     bg=theme.input_background, bd=0, highlightthickness=0)
        search_icon.pack(side="left", padx=(0, dp(5)))
        draw_control_icon(search_icon, "search", 14, 14, foreground=theme.text_secondary)
        self.entry = tk.Entry(self.input_shell, textvariable=self.query, bg=theme.input_background,
                              fg=theme.text_primary, insertbackground=theme.text_primary,
                              relief="flat", highlightthickness=0, font=(font, 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=dp(4))
        self.entry.bind("<FocusIn>", lambda _event: self.input_shell.configure(highlightbackground=app.theme.input_focus))
        self.entry.bind("<FocusOut>", lambda _event: self.input_shell.configure(highlightbackground=app.theme.input_border))
        self.hint = tk.Label(shell, text="搜索标题、备注、分类，或输入日期 / 快捷命令", anchor="w",
                             bg=theme.panel_background, fg=theme.text_muted, font=(font, 8))
        self.hint.pack(side="bottom", fill="x", pady=(dp(8), 0))
        frame = tk.Frame(shell, bg=theme.panel_background)
        frame.pack(fill="both", expand=True)
        self.tree = PaletteList(frame, app, font)
        scrollbar = ttk.Scrollbar(frame, command=self.tree.yview, style="Global.Vertical.TScrollbar")
        self.tree.scrollbar = scrollbar
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self._click)
        self.bind("<Escape>", lambda _event: self.close() or "break")
        self.bind("<Return>", lambda _event: self.activate() or "break")
        self.bind("<Up>", lambda _event: self.move(-1))
        self.bind("<Down>", lambda _event: self.move(1))
        # Treeview has class-level arrow bindings. Consume them before the
        # class binding, otherwise one key press advances two results.
        for widget in (self.entry, self.tree):
            widget.bind("<Up>", lambda _event: self.move(-1))
            widget.bind("<Down>", lambda _event: self.move(1))
        self.bind("<Control-k>", lambda _event: self.present() or "break")
        for sequence in ("<Control-z>", "<Control-y>", "<Control-Shift-Z>"):
            self.bind(sequence, lambda _event: "break")
        self._query_trace = self.query.trace_add("write", self._changed)
        area = app.dpi.work_area()
        center(self, app, min(600, app.dpi.logical(area.width) - 32), min(428, app.dpi.logical(area.height) - 48), y_offset=32)
        self._palette_dpi = app.dpi.dpi
        self.bind("<Configure>", self._check_palette_dpi, add="+")
        self.request()
        self._poll_job = self.after(30, self._poll)

    def present(self):
        self.app.present_modal(self, self.app)
        self.grab_set()
        self.entry.focus_force()

    def _changed(self, *_args):
        if self._activation_job:
            self.after_cancel(self._activation_job)
            self._activation_job = None
        self.app.search_service.cancel_pending()
        self.ticket = None
        self._results = ()
        self._pending = True
        self._activate_when_ready = False
        self.tree.set_pending()
        self.hint.configure(text="正在搜索…")
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = self.after(45, self._debounced_request)

    def _debounced_request(self):
        self._debounce = None
        self.request()

    def request(self):
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = None
        if not self._closed:
            self.ticket = self.app.search_service.request(self.app.store, self.query.get())

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        delivery = self.app.search_service.poll()
        if delivery and delivery[0] == self.ticket:
            _, results, error = delivery
            self._show(results, error)
        self._poll_job = self.after(20, self._poll)

    def _show(self, results, error=None):
        self._pending = False
        self._results = results
        self.tree.set_results(results, self.query.get(), {event.id: event.event_type for event in self.app.store.events})
        self.hint.configure(text=f"搜索失败：{error}" if error else "没有匹配事项或命令" if not results else f"{len(results)} 条结果（事项最多显示 100 条） · ↑↓ 选择 · Enter 打开 · Esc 关闭")
        if self._activate_when_ready:
            self._activate_when_ready = False
            if not error:
                self._queue_activate()

    def _check_palette_dpi(self, event):
        if event.widget is self and self._palette_dpi != self.app.dpi.dpi:
            self._palette_dpi = self.app.dpi.dpi
            self.minsize(self.app.dpi.px(300), self.app.dpi.px(240))
            self.tree.reflow()

    def move(self, delta):
        if self._results:
            selected = self.tree.selection()
            index = max(0, min(len(self._results) - 1, int(selected[0]) + delta if selected else 0))
            self.tree.selection_set(str(index))
            self.tree.focus(str(index))
            self.tree.see(str(index))
        return "break"

    def _click(self, event):
        if self._pending:
            return "break"
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._queue_activate()
        return "break"

    def _queue_activate(self):
        if self._activation_job:
            self.after_cancel(self._activation_job)
        self._activation_job = self.after_idle(self.activate)

    def activate(self):
        self._activation_job = None
        if self._pending and not self._closed:
            self._activate_when_ready = True
            return
        selected = self.tree.selection()
        if not selected or self._closed:
            return
        result = self._results[int(selected[0])]
        self.close(restore_focus=False)
        self.app.after_idle(lambda: self.app._activate_search_result(result))

    def close(self, *, restore_focus=True):
        if self._closed:
            return
        self._closed = True
        for job in (self._debounce, self._poll_job, self._activation_job):
            if job:
                self.after_cancel(job)
        query = self.query
        query.trace_remove("write", self._query_trace)
        self.app.search_service.cancel_pending()
        if self.grab_current() is self:
            self.grab_release()
        self.app._leave_modal(self)
        self.app.search_window = None
        self.destroy()
        # Tk Variables must be released on the UI thread, not left in a widget
        # reference cycle that a background search allocation might collect.
        self.query = None
        del query
        if restore_focus:
            try:
                target = self._previous_focus
                (target if target is not None and target.winfo_exists() else self.app).focus_set()
            except tk.TclError:
                self.app.focus_set()
        self.app.after(120, self.app.restore_window_mode_if_idle)
