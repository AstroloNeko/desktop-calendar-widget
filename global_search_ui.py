"""Shared Compact/Global command palette. No task CRUD or date algorithms."""

import tkinter as tk
from tkinter import ttk

from event_search import EventSearchService


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


class SearchDialog(tk.Toplevel):
    def __init__(self, app, *, font, center):
        super().__init__(app)
        self.withdraw()
        self.app = app
        self._closed = False
        self._debounce = None
        self._poll_job = None
        self._results = ()
        self._images = []
        self._previous_focus = app.focus_get()
        self.title("全局搜索 · 快捷命令")
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        theme, dp = app.theme, app.dpi.px
        self.configure(bg=theme.panel_background)
        self.minsize(dp(300), dp(240))
        shell = tk.Frame(self, bg=theme.panel_background, padx=dp(14), pady=dp(12))
        shell.pack(fill="both", expand=True)
        self.query = tk.StringVar()
        self.entry = tk.Entry(shell, textvariable=self.query, bg=theme.input_background,
                              fg=theme.text_primary, insertbackground=theme.text_primary,
                              relief="flat", highlightthickness=dp(1), highlightbackground=theme.input_border,
                              highlightcolor=theme.input_focus, font=(font, 10))
        self.entry.pack(fill="x", ipady=dp(7))
        self.hint = tk.Label(shell, text="搜索标题、备注、分类，或输入日期 / 快捷命令", anchor="w",
                             bg=theme.panel_background, fg=theme.text_muted, font=(font, 8))
        self.hint.pack(fill="x", pady=dp(7))
        style = ttk.Style(self)
        style.configure("Search.Treeview", background=theme.panel_background, fieldbackground=theme.panel_background,
                        foreground=theme.text_primary, font=(font, 9), rowheight=dp(48), borderwidth=0)
        style.map("Search.Treeview", background=[("selected", theme.accent_soft)],
                  foreground=[("selected", theme.text_primary)])
        frame = tk.Frame(shell, bg=theme.panel_background)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, show="tree", selectmode="browse", style="Search.Treeview")
        self.tree.column("#0", stretch=True, minwidth=dp(100))
        scrollbar = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("completed", foreground=theme.text_done)
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
        self.query.trace_add("write", self._changed)
        area = app.dpi.work_area()
        center(self, app, min(640, app.dpi.logical(area.width) - 32), min(460, app.dpi.logical(area.height) - 48), y_offset=32)
        self.request()
        self._poll_job = self.after(30, self._poll)

    def present(self):
        self.app.present_modal(self, self.app)
        self.grab_set()
        self.entry.focus_force()

    def _changed(self, *_args):
        self.app.search_service.cancel_pending()
        self.ticket = None
        self._show(())
        self.hint.configure(text="正在搜索…")
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = self.after(70, self.request)

    def request(self):
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
        self._poll_job = self.after(30, self._poll)

    def _show(self, results, error=None):
        self._results = results
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._images.clear()
        dp, theme = self.app.dpi.px, self.app.theme
        for index, result in enumerate(results):
            doc = result.document
            image = tk.PhotoImage(master=self, width=dp(5), height=dp(28))
            image.put(doc.color if doc else theme.accent, to=(0, 0, dp(5), dp(28)))
            self._images.append(image)
            # One row, two bounded lines; details remain available in the editor.
            title = result.title.replace("\n", " ")
            self.tree.insert("", "end", iid=str(index), text=f"{title}\n{result.detail}", image=image,
                             tags=("completed",) if doc and doc.completed else ())
        if results:
            self.tree.selection_set("0")
            self.tree.focus("0")
        self.hint.configure(text=f"搜索失败：{error}" if error else "没有匹配事项或命令" if not results else f"{len(results)} 条结果（事项最多显示 100 条） · ↑↓ 选择 · Enter 打开 · Esc 关闭")

    def move(self, delta):
        if self._results:
            selected = self.tree.selection()
            index = max(0, min(len(self._results) - 1, int(selected[0]) + delta if selected else 0))
            self.tree.selection_set(str(index))
            self.tree.focus(str(index))
            self.tree.see(str(index))
        return "break"

    def _click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.after_idle(self.activate)
        return "break"

    def activate(self):
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
        for job in (self._debounce, self._poll_job):
            if job:
                self.after_cancel(job)
        self.app.search_service.cancel_pending()
        if self.grab_current() is self:
            self.grab_release()
        self.app._leave_modal(self)
        self.app.search_window = None
        self.destroy()
        if restore_focus:
            try:
                target = self._previous_focus
                (target if target is not None and target.winfo_exists() else self.app).focus_set()
            except tk.TclError:
                self.app.focus_set()
        self.app.after(120, self.app.restore_window_mode_if_idle)
