"""A bounded reminder-plan viewer. All mutations use existing app/engine APIs."""
from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from desktop_calendar.reminders.reminder_widgets import COMBO, ReminderButton, card, reminder_font
from desktop_calendar.reminders.reminder_overview import batch_channel_statuses
from desktop_calendar.ui.themed_controls import SettingsScroll
from desktop_calendar.updates.update_dialog import dialog_geometry
from desktop_calendar.reminders.notification_dispatch import STATUS_LABELS
from desktop_calendar.reminders.notification_model import BRIEF_GROUP_LABELS


class ReminderPlanPanel(tk.Frame):
    GROUPS = (("attention", "需要处理"), ("snoozed", "已稍后提醒"),
              ("upcoming", "即将提醒"), ("processed", "已处理"))

    def __init__(self, parent, app, close, *, clock=datetime.now):
        theme, dp = app.theme, app.dpi.px
        super().__init__(parent, bg=theme.panel_background)
        self.app, self.close, self.clock = app, close, clock
        self.revision, self.refreshed_at = -1, 0
        self.rows = {}
        self.brief_rows = {}
        width = dp(500)

        self.detail = tk.StringVar(value="选择一项查看详情。")
        self.details_title = tk.StringVar(value="选择一项查看详情。")
        self.details_content = tk.StringVar()
        detail_card, detail_frame = card(self, app, padding=9)
        self.detail_frame = detail_frame
        detail_card.pack(side="bottom", fill="x", pady=(dp(8), 0))
        self.detail_heading = tk.Label(detail_frame, textvariable=self.details_title, anchor="w",
                                      bg=detail_frame.cget("bg"), fg=theme.text_primary, font=reminder_font(app, 9, "bold"))
        self.detail_heading.pack(fill="x")
        self.detail_label = tk.Label(detail_frame, textvariable=self.details_content, anchor="w", justify="left",
                                     bg=detail_frame.cget("bg"), fg=theme.text_secondary, font=reminder_font(app, 8))
        self.detail_label.pack(fill="x", pady=(dp(3), 0))
        self.selection_actions = tk.Frame(detail_frame, bg=detail_frame.cget("bg"))
        self.selection_actions.pack(fill="x", pady=(dp(5), 0))
        self.open_button = ReminderButton(self.selection_actions, app, "打开事项", self.open_event)
        self.open_button.pack(side="left")
        self.cancel_button = ReminderButton(self.selection_actions, app, "取消稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(side="left", padx=(dp(6), 0))
        self.details_button = ReminderButton(self.selection_actions, app, "通知明细", self.open_details)
        self.details_button.pack(side="left", padx=(dp(6), 0))
        self.detail.trace_add("write", self._render_detail)
        self.metrics = tk.Frame(self, bg=theme.panel_background)
        self.metrics.pack(fill="x", pady=(0, dp(8)))
        self.metrics.columnconfigure((0, 1, 2), weight=1, uniform="reminder-count")
        self.counts = {}
        colors = (theme.danger, theme.event_type_urgent, theme.accent)
        for column, ((group, label), color) in enumerate(zip(self.GROUPS[:3], colors)):
            metric, body = card(self.metrics, app, padding=6)
            metric.grid(row=0, column=column, sticky="ew", padx=(dp(6) if column else 0, 0))
            self.counts[group] = tk.StringVar(value="0")
            tk.Label(body, textvariable=self.counts[group], bg=body.cget("bg"), fg=color,
                     anchor="w", font=reminder_font(app, 11, "bold"), width=3).pack(side="left")
            tk.Label(body, text=label, bg=body.cget("bg"), fg=theme.text_primary,
                     anchor="w", font=reminder_font(app, 8)).pack(side="left", fill="x", expand=True)
        self.table_card, table_body = card(self, app, padding=8)
        self.table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_body, bg=theme.workspace_detail)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="提醒计划", bg=theme.workspace_detail, fg=theme.text_primary,
                 anchor="w", font=reminder_font(app, 10, "bold")).pack(side="left")
        self.refresh_button = ReminderButton(toolbar, app, "刷新", self.refresh)
        self.refresh_button.pack(side="right")
        self.filters = {"全部计划": "", **{label: group for group, label in self.GROUPS}, "每日简报": "brief"}
        self.filter_choice = tk.StringVar(value="全部计划")
        filter_box = ttk.Combobox(toolbar, textvariable=self.filter_choice, values=tuple(self.filters),
                                  width=9, state="readonly", font=reminder_font(app, 9), style=COMBO)
        filter_box.pack(side="right", padx=dp(5))
        filter_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.summary = tk.StringVar()
        self.summary_label = tk.Label(table_body, textvariable=self.summary, justify="left", anchor="w",
                                     bg=theme.workspace_detail, fg=theme.text_secondary, font=reminder_font(app, 8),
                                     wraplength=width - dp(28))
        self.summary_label.pack(fill="x", pady=(dp(3), dp(4)))
        frame = tk.Frame(table_body, bg=theme.workspace_detail)
        frame.pack(fill="both", expand=True)
        style = ttk.Style(self)
        style.configure("ReminderPlan.Treeview", background=theme.input_background,
                        fieldbackground=theme.input_background, foreground=theme.text_primary,
                        font=reminder_font(app, 9), rowheight=dp(26), borderwidth=0, relief="flat")
        style.map("ReminderPlan.Treeview", background=[("selected", theme.accent_soft)],
                  foreground=[("selected", theme.text_primary)])
        style.layout("ReminderPlan.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("ReminderPlan.Treeview.Heading", font=reminder_font(app, 8, "bold"),
                        background=theme.panel_secondary, foreground=theme.text_primary, padding=(dp(3), dp(4)))
        if "ReminderHeading.cell" not in style.element_names():
            style.element_create("ReminderHeading.cell", "from", "clam", "Treeheading.cell")
        style.layout("ReminderPlan.Treeview.Heading", [
            ("ReminderHeading.cell", {"sticky": "nswe", "children": [
                ("Treeheading.padding", {"sticky": "nswe", "children": [
                    ("Treeheading.text", {"sticky": "w"})]})]})])
        self.tree = ttk.Treeview(frame, columns=("when", "windows", "qq"), show="tree headings",
                                 selectmode="browse", style="ReminderPlan.Treeview")
        for identifier, text, minimum in (("#0", "事项 / 类型", 92), ("when", "计划提醒时间", 104),
                                         ("windows", "Windows 通知", 88), ("qq", "QQ 通知", 82)):
            self.tree.heading(identifier, text=text, anchor="w")
            self.tree.column(identifier, width=dp(minimum), minwidth=dp(minimum), stretch=False)
        self.scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview,
                                        style="Global.Vertical.TScrollbar")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")

        def scroll(first, last):
            self.scrollbar.set(first, last)
            if float(first) == 0 and float(last) == 1:
                self.scrollbar.grid_remove()
            elif not self.scrollbar.winfo_manager():
                self.scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.configure(yscrollcommand=scroll)
        self.tree.bind("<Configure>", self._size_columns)
        self.tree.bind("<Button-1>", self._block_column_resize, add="+")
        self.tree.tag_configure("alternate", background=theme.workspace_detail)
        self.tree.tag_configure("hover", background=theme.schedule_card_hover)
        for group, color in (("attention", theme.danger), ("snoozed", theme.event_type_urgent), ("upcoming", theme.text_primary)):
            self.tree.tag_configure("group:"+group, background=theme.panel_secondary,
                                    foreground=color, font=reminder_font(app, 9, "bold"))
        self._hovered = None
        self.tree.bind("<Motion>", self._hover)
        self.tree.bind("<Leave>", lambda _event: self._hover())
        self.empty_label = tk.Label(frame, text="暂无提醒计划\n\n日程提醒与习惯提醒分别管理", justify="center",
                                    bg=theme.input_background, fg=theme.text_secondary, font=reminder_font(app, 10))
        self.tree.bind("<<TreeviewSelect>>", self.show_selection)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Return>", lambda _e: self.open_event())
        self._compact = False
        self.bind("<Configure>", self._resize, add="+")
        self._render_detail()

    def _resize(self, event):
        if event.widget is self:
            wrap = max(1, event.width - self.app.dpi.px(25))
            self.summary_label.configure(wraplength=wrap)
            self.detail_heading.configure(wraplength=wrap)
            self.detail_label.configure(wraplength=wrap)
            compact = event.height < self.app.dpi.px(390)
            if self._compact != compact:
                self._compact = compact
                self.detail_frame.configure(pady=self.app.dpi.px(5 if compact else 9))
                if compact:
                    self.summary_label.pack_forget()
                else:
                    self.summary_label.pack(fill="x", pady=(self.app.dpi.px(3), self.app.dpi.px(4)),
                                            before=self.tree.master)
                self._render_detail()

    def _size_columns(self, event):
        dp = self.app.dpi.px
        widths = {"when": max(dp(104), round(event.width * .24)),
                  "windows": max(dp(88), round(event.width * .19)),
                  "qq": max(dp(82), round(event.width * .17))}
        for key, width in widths.items():
            self.tree.column(key, width=width)
        self.tree.column("#0", width=max(dp(92), event.width - sum(widths.values())))

    def _block_column_resize(self, event):
        if self.tree.identify_region(event.x, event.y) == "separator":
            return "break"

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
            now = self.clock()
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
                self.tree.insert(group, "end", iid=key, text=f"DDL · {title}" if row.ddl else title,
                                 values=(row.when.strftime(short_date), row.windows, row.qq),
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
                    self.tree.insert("brief", "end", iid=key, text="每日行程简报",
                                     values=(batch["when"].replace("T", " ")[:16], *batch_channel_statuses(batch)))
                visible_rows += len(self.brief_rows)
        if selected and (selected[0] in self.rows or selected[0] in self.brief_rows):
            self.tree.selection_set(selected)
            if scroll:
                self.tree.yview_moveto(scroll[0])
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
        self.details_button.configure(state="disabled")
        if selected and selected[0] in self.brief_rows:
            self.show_notice_details(self.brief_rows[selected[0]])
        elif row:
            attempt = ("\n下次最早尝试  " + row.next_attempt.strftime("%Y-%m-%d %H:%M")) if row.next_attempt else ""
            title = row.title.replace("\n", " ")
            when = row.when.strftime("%Y-%m-%d %H:%M")
            self.details_button.configure(state="normal")
            self.detail.set(f"{title}\n{row.rule}  ·  {when}\nWindows  {row.windows}    ·    QQ  {row.qq}"
                            + f"\n{row.detail}" + attempt + self.notice_items(row.batch))
        else:
            self.detail.set("选择一项查看详情。")

    def _render_detail(self, *_args):
        lines = self.detail.get().splitlines()
        self.details_title.set(lines[0] if len(lines) > 1 else self.detail.get())
        summary = lines[1:3] if self._compact else lines[1:4]
        row = self.selected_row()
        if len(summary) == 3 and row and row.reason:
            reason = " ".join(row.reason.split())
            summary[2] += " · " + (reason if len(reason) <= 72 else reason[:71] + "…")
        self.details_content.set("\n".join(summary))
        if len(lines) > 1:
            self.selection_actions.pack(fill="x", pady=(self.app.dpi.px(5), 0))
            self.detail_label.pack(fill="x", before=self.selection_actions, pady=(self.app.dpi.px(3), 0))
        else:
            self.detail_label.pack_forget()
            self.selection_actions.pack_forget()

    def open_details(self):
        owner = self.winfo_toplevel()
        dialog = tk.Toplevel(owner)
        dialog.withdraw()
        dialog.title("通知明细")
        theme, dp = self.app.theme, self.app.dpi.px
        dialog.configure(bg=theme.panel_background)
        width, height, x, y = dialog_geometry(self.app.dpi.work_area(), self.app.dpi.dpi,
            (owner.winfo_rootx(), owner.winfo_rooty(), owner.winfo_width(), owner.winfo_height()),
            preferred=(520, 440))
        dialog.geometry(f"{width}x{height}{x:+d}{y:+d}")

        def close():
            dialog.destroy()
            if owner.winfo_exists():
                owner.grab_set()
                self.details_button.focus_set()

        ReminderButton(dialog, self.app, "关闭", close).pack(side="bottom", anchor="e", padx=dp(14), pady=dp(10))
        form = SettingsScroll(dialog, self.app)
        form.pack(fill="both", expand=True, padx=dp(14), pady=dp(10))
        label = tk.Label(form.content, text=self.detail.get(), bg=theme.panel_background, fg=theme.text_primary,
                         font=reminder_font(self.app), justify="left", anchor="nw")
        label.pack(fill="x")
        label.bind("<Configure>", lambda event: label.configure(wraplength=event.width))
        dialog.bind("<Escape>", lambda _event: close())
        dialog.protocol("WM_DELETE_WINDOW", close)
        self.app.present_modal(dialog, owner)
        dialog.grab_set()

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
            self.details_button.configure(state="normal")
            self.detail.set("通知明细\n" + "\n".join(labels) + self.notice_items(identifier))

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
