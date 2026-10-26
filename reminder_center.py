"""A bounded reminder-plan viewer. All mutations use existing app/engine APIs."""
from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from update_dialog import dialog_geometry
from reminder_ui import reminder_button


class ReminderPlanDialog(tk.Toplevel):
    GROUPS = (("attention", "需要处理"), ("snoozed", "已稍后提醒"), ("upcoming", "即将提醒"))

    def __init__(self, app):
        super().__init__(app)
        self.withdraw()
        self.app, self._closing = app, False
        self.revision, self.refreshed_at = -1, 0
        self.rows = {}
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        self.title("提醒计划")
        self.configure(bg=theme.panel_background)
        area = app.dpi.work_area()
        width, height, x, y = dialog_geometry(area, app.dpi.dpi,
                                             (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        # More list space on a normal desktop; retain the existing work-area cap.
        height = min(height + dp(80), max(1, area.height - dp(48)))
        y = min(y, area.bottom - height - dp(24))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, dp(360)), min(height, dp(320)))

        def button(parent, text, callback, **options):
            return reminder_button(parent, app, text, callback, **options)

        self.actions = tk.Frame(self, bg=theme.panel_background)
        self.actions.pack(side="bottom", fill="x", padx=dp(12), pady=dp(10))
        self.close_button = button(self.actions, "关闭", self.close)
        self.close_button.pack(side="right")
        button(self.actions, "后台与提醒", self.open_settings).pack(side="right", padx=dp(6))
        button(self.actions, "刷新", self.refresh).pack(side="left")
        self.service = tk.StringVar()
        self.service_label = tk.Label(self, textvariable=self.service, anchor="w", justify="left",
                                     bg=theme.panel_background, fg=theme.text_secondary, font=(font, 8),
                                     wraplength=width-dp(28))
        self.service_label.pack(side="bottom", fill="x", padx=dp(12), pady=(dp(4), 0))
        self.service_divider = tk.Frame(self, bg=theme.divider, height=dp(1))
        self.service_divider.pack(side="bottom", fill="x", padx=dp(12))
        self.selection_actions = tk.Frame(self, bg=theme.panel_background)
        self.selection_actions.pack(side="bottom", fill="x", padx=dp(12), pady=dp(4))
        self.open_button = button(self.selection_actions, "打开事项", self.open_event, primary=True)
        self.open_button.pack(side="left")
        self.cancel_button = button(self.selection_actions, "取消稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(side="left", padx=dp(6))
        self.detail = tk.StringVar(value="选择一项查看详情；双击或“打开事项”将关闭此列表并打开现有编辑器。")
        detail_frame = tk.Frame(self, bg=theme.workspace_detail, padx=dp(8), pady=dp(6),
                                highlightthickness=dp(1), highlightbackground=theme.divider)
        self.detail_frame = detail_frame
        detail_frame.pack(side="bottom", fill="x", padx=dp(12), pady=(dp(8), dp(2)))
        self.detail_heading = tk.Label(detail_frame, text="事项详情", anchor="w", bg=theme.workspace_detail,
                                      fg=theme.text_secondary, font=(font, 8, "bold"))
        self.detail_heading.pack(fill="x", pady=(0, dp(3)))
        self.detail_label = tk.Text(detail_frame, height=4, width=1, wrap="word", relief="flat", bd=0,
                                    bg=theme.workspace_detail, fg=theme.text_primary, font=(font, 9),
                                    cursor="arrow", state="disabled", highlightthickness=0)
        detail_bar = ttk.Scrollbar(detail_frame, command=self.detail_label.yview, style="Global.Vertical.TScrollbar")
        self.detail_bar = detail_bar
        detail_bar.pack(side="right", fill="y")
        self.detail_label.configure(yscrollcommand=detail_bar.set)
        self.detail_label.pack(fill="both", expand=True)
        def render_detail(*_args):
            self.detail_label.configure(state="normal")
            self.detail_label.delete("1.0", "end")
            self.detail_label.insert("1.0", self.detail.get())
            self.detail_label.configure(state="disabled")
            self.detail_label.yview_moveto(0)
        self.detail.trace_add("write", render_detail)
        self.summary = tk.StringVar()
        tk.Label(self, text="提醒计划", anchor="w", bg=theme.panel_background, fg=theme.text_primary,
                 font=(font, 13, "bold")).pack(fill="x", padx=dp(12), pady=(dp(12), 0))
        self.summary_label = tk.Label(self, textvariable=self.summary, justify="left", anchor="w",
                                     bg=theme.panel_background, fg=theme.text_secondary, font=(font, 9),
                                     wraplength=width - dp(28))
        self.summary_label.pack(fill="x", padx=dp(12), pady=(dp(4), dp(10)))
        frame = tk.Frame(self, bg=theme.panel_background)
        frame.pack(fill="both", expand=True, padx=dp(12))
        style = ttk.Style(self)
        style.configure("ReminderPlan.Treeview", background=theme.input_background,
                        fieldbackground=theme.input_background, foreground=theme.text_primary,
                        font=(font, 9), rowheight=dp(29), borderwidth=0, relief="flat")
        style.map("ReminderPlan.Treeview", background=[("selected", theme.accent)],
                  foreground=[("selected", theme.text_on_accent)])
        style.configure("ReminderPlan.Treeview.Heading", font=(font, 9),
                        background=theme.control_background, foreground=theme.control_text)
        self.tree = ttk.Treeview(frame, columns=("when", "status"), show="tree headings",
                                 selectmode="browse", style="ReminderPlan.Treeview")
        self.tree.heading("#0", text="事项 / 分组")
        self.tree.heading("when", text="计划提醒时间")
        self.tree.heading("status", text="状态")
        self.tree.column("#0", width=dp(180), minwidth=dp(100), stretch=True)
        self.tree.column("when", width=dp(136), minwidth=dp(120), stretch=False)
        self.tree.column("status", width=dp(125), minwidth=dp(100), stretch=False)
        self.scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview, style="Global.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview, style="Global.Horizontal.TScrollbar")
        self.tree.configure(yscrollcommand=self.scrollbar.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("alternate", background=theme.workspace_detail)
        self.tree.tag_configure("hover", background=theme.schedule_card_hover)
        for group, color in (("attention", theme.danger), ("snoozed", theme.event_type_ddl), ("upcoming", theme.text_primary)):
            self.tree.tag_configure("group:"+group, background=theme.panel_secondary,
                                    foreground=color, font=(font, 9, "bold"))
        self._hovered = None
        self.tree.bind("<Motion>", self._hover)
        self.tree.bind("<Leave>", lambda _event: self._hover())
        self.empty_label = tk.Label(frame, text="暂无提醒计划\n\n日程提醒与习惯提醒分别管理", justify="center",
                                    bg=theme.input_background, fg=theme.text_secondary, font=(font, 10))
        self.tree.bind("<<TreeviewSelect>>", self.show_selection)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Return>", lambda _e: self.open_event())
        self._compact_detail = None
        self.bind("<Configure>", self._resize, add="+")
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        app.present_modal(self, app)
        self.grab_set()
        self.tree.focus_set()

    def _resize(self, event):
        if event.widget is self:
            wrap = max(1, event.width - self.app.dpi.px(28))
            self.summary_label.configure(wraplength=wrap)
            self.service_label.configure(wraplength=wrap)
            compact = event.height < self.app.dpi.px(440)
            if compact != self._compact_detail:
                self._compact_detail = compact
                self.detail_label.configure(height=1 if compact else 4)
                self.detail_frame.configure(pady=self.app.dpi.px(2 if compact else 6))
                self.detail_heading.configure(text="事项详情 · 滚轮查看更多" if compact else "事项详情")
                if compact:
                    self.service_label.pack_forget()
                    # Native scrollbar arrows impose a minimum height larger
                    # than one compact detail line. Text still scrolls natively.
                    self.detail_bar.pack_forget()
                else:
                    self.detail_bar.pack(side="right", before=self.detail_label, fill="y")
                    self.service_label.pack(side="bottom", before=self.service_divider, fill="x",
                                            padx=self.app.dpi.px(12), pady=(self.app.dpi.px(4), 0))

    def _hover(self, event=None):
        key = self.tree.identify_row(event.y) if event else None
        key = key if key in self.rows else None
        if self._hovered and self.tree.exists(self._hovered):
            self.tree.item(self._hovered, tags=tuple(tag for tag in self.tree.item(self._hovered, "tags") if tag != "hover"))
        if key:
            self.tree.item(key, tags=("hover", *self.tree.item(key, "tags")))
        self._hovered = key

    def refresh(self):
        if self._closing:
            return
        try:
            overview = self.app.reminder_engine.overview(datetime.now())
        except (OSError, ValueError) as error:
            self.summary.set("无法读取提醒计划：" + str(error))
            self.service.set("可稍后刷新重试；当前不会根据不完整的列表执行操作。")
            self.rows = {}
            self.tree.delete(*self.tree.get_children())
            self.empty_label.configure(text="暂时无法读取提醒计划\n\n请检查上方提示后刷新")
            self.empty_label.place(relx=.5, rely=.5, anchor="center")
            self.show_selection()
            return
        selected = self.tree.selection()
        scroll = self.tree.yview()
        opened = {group: bool(self.tree.item(group, "open")) for group, _ in self.GROUPS if self.tree.exists(group)}
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for group, label in self.GROUPS:
            entries = [row for row in overview.rows if row.group == group]
            self.tree.insert("", "end", iid=group, text=f"{label}  {len(entries)}", open=opened.get(group, True), tags=("group:"+group,))
            for index, row in enumerate(entries):
                key = "event:" + row.event_id
                self.rows[key] = row
                self.tree.insert(group, "end", iid=key, text=row.title,
                                 values=(row.when.strftime("%Y-%m-%d %H:%M"), row.status),
                                 tags=("alternate",) if index % 2 else ())
        if selected and selected[0] in self.rows:
            self.tree.selection_set(selected)
        if scroll:
            self.tree.yview_moveto(scroll[0])
        count = len(overview.rows)
        service = self.app.reminder_engine.error or self.app.notification_backend.status
        service = str(service).replace("\n", " ")[:180]
        self.summary.set((f"日程提醒已启用 · {count} 项计划" if overview.enabled else "日程提醒已关闭 · 当前不安排事项通知")
                         + f"\n{overview.submitted} 条已提交 Windows · 不代表已显示或已阅读")
        self.service.set("服务最近状态（非单项诊断）：" + service
                         + "\n仅含日程提醒；习惯单独设置，彻底退出后不再提醒。")
        if count:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="暂无提醒计划\n\n日程提醒与习惯提醒分别管理")
            self.empty_label.place(relx=.5, rely=.5, anchor="center")
        self.revision = self.app.reminder_engine.revision
        self.refreshed_at = time.monotonic()
        self.show_selection()

    def selected_row(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def show_selection(self, _event=None):
        row = self.selected_row()
        self.open_button.configure(state="normal" if row else "disabled")
        self.cancel_button.configure(state="normal" if row and row.snoozed else "disabled")
        if row:
            attempt = ("\n下次最早尝试：" + row.next_attempt.strftime("%Y-%m-%d %H:%M")) if row.next_attempt else "\n当前没有后续自动派发。"
            # A bounded scrollable detail pane keeps full imported titles readable.
            title = row.title.replace("\n", " ")
            self.detail.set(f"{title}\n{row.status} · {'DDL 截止日' if row.ddl else '事项执行日'}：{row.anchor}"
                            + attempt + "\n" + row.detail)
        else:
            self.detail.set("选择一项查看详情；双击或“打开事项”将关闭此列表并打开现有编辑器。")

    def _double_click(self, event):
        key = self.tree.identify_row(event.y)
        if key in self.rows:
            self.tree.selection_set(key)
            self.open_event()
            return "break"

    def open_event(self):
        row = self.selected_row()
        if row is None:
            return
        self.close()
        self.app._activate_search_result(SimpleNamespace(kind="event", key=row.event_id))

    def cancel_snooze(self):
        row = self.selected_row()
        if row and row.snoozed:
            try:
                self.app.reminder_engine.cancel_snooze(row.event_id)
                self.refresh()
                self.detail.set("已取消稍后提醒；不会重新发送原始通知，事项日期与完成状态保持不变。")
            except (OSError, ValueError) as error:
                self.detail.set("取消未完成：" + str(error))

    def open_settings(self):
        self.close()
        self.app.open_reminder_settings()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.app.notification_plan_window = None
        self.destroy()
