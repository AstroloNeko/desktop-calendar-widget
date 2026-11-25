"""A bounded reminder-plan viewer. All mutations use existing app/engine APIs."""
from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from desktop_calendar.reminders.reminder_ui import reminder_button
from desktop_calendar.reminders.notification_dispatch import STATUS_LABELS
from desktop_calendar.reminders.notification_model import BRIEF_GROUP_LABELS


class ReminderPlanPanel(tk.Frame):
    GROUPS = (("attention", "需要处理"), ("snoozed", "已稍后提醒"),
              ("upcoming", "即将提醒"), ("processed", "已处理"))

    def __init__(self, parent, app, close):
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        super().__init__(parent, bg=theme.panel_background)
        self.app, self.close = app, close
        self.revision, self.refreshed_at = -1, 0
        self.rows = {}
        self.brief_rows = {}
        width = dp(500)

        def button(parent, text, callback, **options):
            return reminder_button(parent, app, text, callback, **options)

        self.detail = tk.StringVar(value="选择一项查看详情。")
        detail_frame = tk.Frame(self, bg=theme.workspace_detail, padx=dp(9), pady=dp(6),
                                highlightthickness=dp(1), highlightbackground=theme.divider)
        self.detail_frame = detail_frame
        detail_frame.pack(side="bottom", fill="x", padx=dp(6), pady=(dp(7), dp(3)))
        self.selection_actions = tk.Frame(detail_frame, bg=theme.workspace_detail)
        self.selection_actions.pack(side="bottom", fill="x", pady=(dp(5), 0))
        self.open_button = button(self.selection_actions, "打开事项", self.open_event, primary=True)
        self.open_button.pack(side="left", fill="x", expand=True)
        self.cancel_button = button(self.selection_actions, "取消稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(side="left", fill="x", expand=True, padx=(dp(6), 0))
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
        self.detail_label.tag_configure("detail_title", font=(font, 9, "bold"))
        def render_detail(*_args):
            self.detail_label.configure(state="normal")
            self.detail_label.delete("1.0", "end")
            self.detail_label.insert("1.0", self.detail.get())
            if self.selected_row() is not None:
                self.detail_label.tag_add("detail_title", "1.0", "1.end")
            self.detail_label.configure(state="disabled")
            self.detail_label.yview_moveto(0)
        self.detail.trace_add("write", render_detail)
        self.metrics = tk.Frame(self, bg=theme.panel_background)
        self.metrics.pack(fill="x", padx=dp(6), pady=(dp(7), dp(6)))
        self.metrics.columnconfigure((0, 1, 2), weight=1, uniform="reminder-count")
        self.counts = {}
        colors = (theme.danger, theme.event_type_urgent, theme.accent)
        for column, ((group, label), color) in enumerate(zip(self.GROUPS[:3], colors)):
            card = tk.Frame(self.metrics, bg=theme.workspace_detail, padx=dp(8), pady=dp(4),
                            highlightthickness=dp(1), highlightbackground=theme.divider)
            card.grid(row=0, column=column, sticky="ew", padx=(dp(5) if column else 0, 0))
            self.counts[group] = tk.StringVar(value="0")
            tk.Label(card, textvariable=self.counts[group], bg=theme.workspace_detail, fg=color,
                     anchor="w", font=(font, 13, "bold")).pack(fill="x")
            tk.Label(card, text=label, bg=theme.workspace_detail, fg=theme.text_primary,
                     anchor="w", font=(font, 8)).pack(fill="x")
        self.table_card = tk.Frame(self, bg=theme.workspace_detail, padx=dp(7), pady=dp(5),
                                   highlightthickness=dp(1), highlightbackground=theme.divider)
        self.table_card.pack(fill="both", expand=True, padx=dp(6))
        toolbar = tk.Frame(self.table_card, bg=theme.workspace_detail)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="提醒计划", bg=theme.workspace_detail, fg=theme.text_primary,
                 anchor="w", font=(font, 10, "bold")).pack(side="left")
        self.refresh_button = button(toolbar, "刷新", self.refresh)
        self.refresh_button.pack(side="right")
        self.filters = {"全部计划": "", **{label: group for group, label in self.GROUPS}, "每日简报": "brief"}
        self.filter_choice = tk.StringVar(value="全部计划")
        filter_box = ttk.Combobox(toolbar, textvariable=self.filter_choice, values=tuple(self.filters),
                                  width=9, state="readonly", font=(font, 9))
        filter_box.pack(side="right", padx=dp(5))
        filter_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.summary = tk.StringVar()
        self.summary_label = tk.Label(self.table_card, textvariable=self.summary, justify="left", anchor="w",
                                     bg=theme.workspace_detail, fg=theme.text_secondary, font=(font, 8),
                                     wraplength=width - dp(28))
        self.summary_label.pack(fill="x", pady=(dp(3), dp(4)))
        frame = tk.Frame(self.table_card, bg=theme.workspace_detail)
        frame.pack(fill="both", expand=True)
        style = ttk.Style(self)
        style.configure("ReminderPlan.Treeview", background=theme.input_background,
                        fieldbackground=theme.input_background, foreground=theme.text_primary,
                        font=(font, 9), rowheight=dp(26), borderwidth=0, relief="flat")
        style.map("ReminderPlan.Treeview", background=[("selected", theme.accent_soft)],
                  foreground=[("selected", theme.text_primary)])
        style.configure("ReminderPlan.Treeview.Heading", font=(font, 9),
                        background=theme.control_background, foreground=theme.control_text)
        self.tree = ttk.Treeview(frame, columns=("when", "status"), show="tree headings",
                                 selectmode="browse", style="ReminderPlan.Treeview")
        self.tree.heading("#0", text="事项 / 类型", anchor="w")
        self.tree.heading("when", text="提醒时间", anchor="w")
        self.tree.heading("status", text="状态", anchor="w")
        self.tree.column("#0", width=dp(180), minwidth=dp(100), stretch=True)
        self.tree.column("when", width=dp(112), minwidth=dp(85), stretch=False)
        self.tree.column("status", width=dp(166), minwidth=dp(112), stretch=True)
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
        for group, color in (("attention", theme.danger), ("snoozed", theme.event_type_urgent), ("upcoming", theme.text_primary)):
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

    def _resize(self, event):
        if event.widget is self:
            wrap = max(1, event.width - self.app.dpi.px(28))
            self.summary_label.configure(wraplength=wrap)
            dp = self.app.dpi.px
            if event.height < dp(295):
                self.metrics.pack_forget()
            elif not self.metrics.winfo_manager():
                self.metrics.pack(fill="x", padx=dp(6), pady=(dp(7), dp(6)), before=self.table_card)
            detail_lines = 1 if event.height < dp(335) else (2 if event.height < dp(395) else 3)
            compact = event.height < self.app.dpi.px(440)
            if (compact, detail_lines) != self._compact_detail:
                self._compact_detail = (compact, detail_lines)
                self.detail_label.configure(height=detail_lines if compact else 4)
                self.detail_frame.configure(pady=self.app.dpi.px(2 if compact else 6))
                self.detail_heading.configure(text="事项详情 · 滚轮查看更多" if compact else "事项详情")
                if compact and detail_lines == 1:
                    self.detail_bar.pack_forget()
                else:
                    self.detail_bar.pack(side="right", before=self.detail_label, fill="y")

    def _hover(self, event=None):
        key = self.tree.identify_row(event.y) if event else None
        key = key if key in self.rows else None
        if self._hovered and self.tree.exists(self._hovered):
            self.tree.item(self._hovered, tags=tuple(tag for tag in self.tree.item(self._hovered, "tags") if tag != "hover"))
        if key:
            self.tree.item(key, tags=("hover", *self.tree.item(key, "tags")))
        self._hovered = key

    def refresh(self):
        try:
            now = datetime.now()
            overview = self.app.reminder_engine.overview(now)
        except (OSError, ValueError) as error:
            self.summary.set("无法读取提醒计划：" + str(error))
            for count in self.counts.values():
                count.set("—")
            self.rows = {}
            self.brief_rows = {}
            self.tree.delete(*self.tree.get_children())
            self.empty_label.configure(text="暂时无法读取提醒计划\n\n请检查上方提示后刷新")
            self.empty_label.place(relx=.5, rely=.5, anchor="center")
            self.show_selection()
            return
        selected = self.tree.selection()
        scroll = self.tree.yview()
        opened = {group: bool(self.tree.item(group, "open")) for group, _ in (*self.GROUPS, ("brief", "")) if self.tree.exists(group)}
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        filtered_group = self.filters[self.filter_choice.get()]
        visible_rows = 0
        for group, label in self.GROUPS:
            entries = [row for row in overview.rows if row.group == group]
            if group in self.counts:
                self.counts[group].set(str(len(entries)))
            if not entries or (filtered_group and group != filtered_group):
                continue
            self.tree.insert("", "end", iid=group, text=f"{label}  {len(entries)}",
                             open=opened.get(group, True), tags=("group:"+group,))
            for index, row in enumerate(entries):
                key = "event:" + row.event_id
                self.rows[key] = row
                short_date = "%m-%d %H:%M" if row.when.year == now.year else "%Y-%m-%d %H:%M"
                title = row.title.replace("\n", " ")
                self.tree.insert(group, "end", iid=key, text=f"{title}  ·  DDL" if row.ddl else title,
                                 values=(row.when.strftime(short_date), row.status),
                                 tags=("alternate",) if index % 2 else ())
            visible_rows += len(entries)
        self.brief_rows = {}
        if filtered_group in ("", "brief"):
            batches = self.app.reminder_engine._state()["batches"]
            briefs = [(key, batch) for key, batch in batches.items() if batch["kind"] == "brief"]
            if briefs:
                self.tree.insert("", "end", iid="brief", text="每日简报记录",
                                 open=opened.get("brief", filtered_group == "brief"))
                for identifier, batch in sorted(briefs, key=lambda item: item[1]["when"], reverse=True)[:30]:
                    key = "brief:" + identifier
                    self.brief_rows[key] = identifier
                    status = " / ".join(STATUS_LABELS[entry["status"]] for entry in batch["channels"].values())
                    self.tree.insert("brief", "end", iid=key, text="每日行程简报",
                                     values=(batch["when"].replace("T", " ")[:16], status))
                visible_rows += len(self.brief_rows)
        if selected and (selected[0] in self.rows or selected[0] in self.brief_rows):
            self.tree.selection_set(selected)
            if scroll:
                self.tree.yview_moveto(scroll[0])
        elif self.rows:
            key = next(iter(self.rows))
            self.tree.selection_set(key)
            self.tree.see(key)
        summary = (f"{len(overview.rows)} 项计划" if overview.enabled else "日程提醒已关闭")
        self.summary.set(f"{summary} · Windows 已提交 {overview.submitted} · QQ 已发送 {overview.qq_sent}")
        if visible_rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="此分组暂无记录" if filtered_group else "暂无提醒计划")
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
        selected = self.tree.selection()
        if selected and selected[0] in self.brief_rows:
            self.show_notice_details(self.brief_rows[selected[0]])
        elif row:
            attempt = ("\n下次最早尝试  " + row.next_attempt.strftime("%Y-%m-%d %H:%M")) if row.next_attempt else ""
            title = row.title.replace("\n", " ")
            when = row.when.strftime("%Y-%m-%d %H:%M")
            self.detail.set(f"{title}\n计划提醒时间  {when}\n{row.detail}"
                            + attempt + self.notice_items(row.batch))
        else:
            self.detail.set("选择一项查看详情。")

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

    def show_notice(self, identifier):
        if self.filter_choice.get() != "全部计划":
            self.filter_choice.set("全部计划")
            self.refresh()
        for key, row in self.rows.items():
            if row.batch == identifier:
                self.tree.see(key)
                self.tree.selection_set(key)
                self.show_selection()
                return
        key = "brief:" + identifier
        if key in self.brief_rows:
            self.tree.see(key)
            self.tree.selection_set(key)
            self.show_selection()
        else:
            self.show_notice_details(identifier)

    def show_notice_details(self, identifier):
        record = self.app.reminder_engine.batch_record(identifier)
        if record:
            labels = [f"{channel.upper() if channel == 'qq' else 'Windows'} · {STATUS_LABELS[entry['status']]}"
                      + (f" · {entry['reason']}" if entry.get("reason") else "")
                      for channel, entry in record["channels"].items()]
            self.detail.set("\n".join(labels) + self.notice_items(identifier))

    def notice_items(self, identifier):
        if not identifier:
            return ""
        record = self.app.reminder_engine.batch_record(identifier)
        if not record:
            return ""
        if record["kind"] == "brief":
            members = [(item["event_id"], item) for item in record.get("brief_items", [])]
        else:
            members = [(event_id, {}) for event_id, _ in self.app.reminder_engine.batch_members(identifier)]
        lines = []
        current_group = None
        for event_id, item in members:
            event = self.app.store.event_by_id(event_id)
            section = item.get("group")
            if section and section != current_group:
                lines.append(BRIEF_GROUP_LABELS.get(section, section))
                current_group = section
            if event:
                parts = [item.get("time") or item.get("date"), str(event.title).replace("\n", " ")]
                lines.append("  ".join(filter(None, parts)))
        return "\n\n" + "\n".join(lines) if lines else ""
