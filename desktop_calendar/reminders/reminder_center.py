"""A bounded reminder-plan viewer. All mutations use existing app/engine APIs."""
from collections import Counter
from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

from calendar_core import EVENT_TYPE_LABELS
from desktop_calendar.reminders.notification_model import BRIEF_TITLES
from desktop_calendar.reminders.reminder_widgets import card, reminder_font
from desktop_calendar.reminders.reminder_overview import batch_channel_statuses
from desktop_calendar.ui.themed_controls import RoundedCardShell, SettingsScroll, form_button
from desktop_calendar.ui.ui_draw import blend
from desktop_calendar.updates.update_dialog import dialog_geometry
from desktop_calendar.reminders.notification_dispatch import STATUS_LABELS
from desktop_calendar.reminders.notification_model import BRIEF_GROUP_LABELS


class PlanFilterCard(tk.Button):
    """One native focus target for the count, label and full hit area."""

    def __init__(self, parent, app, color, text, command):
        self.theme, self.color = app.theme, color
        self.selected = self.hovered = self.focused = False
        dp = app.dpi.px
        self.rounded = self.theme.rounded_widget_shells
        self.surface = (RoundedCardShell(parent, self.theme, dp, self.theme.workspace_detail,
                                         surface=parent.cget("bg"), radius=dp(8))
                        if self.rounded else tk.Frame(parent, bg=self.theme.divider, cursor="hand2"))
        super().__init__(self.surface, textvariable=text, command=command, takefocus=True,
                         anchor="w", padx=dp(9), pady=dp(6), font=reminder_font(app, 9, "bold"),
                         relief="flat", bd=0, highlightthickness=0 if self.rounded else dp(1),
                         highlightcolor=self.theme.input_focus, cursor="hand2")
        self.pack(fill="both", expand=True, padx=dp(5 if self.rounded else 1), pady=dp(2 if self.rounded else 1))
        self.surface.bind("<Enter>", lambda _event: self.set_hover(True))
        self.surface.bind("<Leave>", lambda event: self.set_hover(
            self.surface.winfo_containing(event.x_root, event.y_root) is self))
        self.surface.bind("<Button-1>", self.activate)
        self.bind("<Enter>", lambda _event: self.set_hover(True))
        self.bind("<Leave>", lambda event: self.set_hover(
            self.surface.winfo_containing(event.x_root, event.y_root) is self.surface))
        self.bind("<Return>", self.activate)
        self.bind("<space>", self.activate)
        self.bind("<FocusIn>", lambda _event: self.set_focus(True))
        self.bind("<FocusOut>", lambda _event: self.set_focus(False))
        self.paint()

    def activate(self, _event):
        self.invoke()
        return "break"

    def set_hover(self, hovered):
        self.hovered = hovered
        self.paint()

    def set_selected(self, selected):
        self.selected = selected
        self.paint()

    def set_focus(self, focused):
        self.focused = focused
        self.paint()

    def paint(self):
        theme = self.theme
        base = theme.workspace_detail
        tinted = blend(base, self.color, .20 if self.hovered else .12)
        fill = tinted if self.selected else theme.control_hover if self.hovered else base
        edge = theme.input_focus if self.focused else self.color if self.selected or self.hovered else theme.divider
        text = self.color if self.selected else theme.text_primary
        if self.rounded:
            self.surface.set_fill(fill)
            self.surface.set_outline(edge)
        else:
            self.surface.configure(bg=edge)
        self.configure(bg=fill, fg=text, highlightbackground=fill, activebackground=tinted, activeforeground=self.color)


class ReminderPlanPanel(tk.Frame):
    GROUPS = {"attention": "需要处理", "snoozed": "已稍后提醒", "upcoming": "即将提醒", "processed": "已处理"}
    COLUMNS = (("item", "事项 / 类型", 109), ("business", "提醒状态", 65),
               ("when", "计划提醒时间", 90), ("windows", "Windows 通知", 79), ("qq", "QQ 通知", 68))
    EMPTY_DETAIL = "选择一项查看详情。"

    def __init__(self, parent, app, close, *, clock=datetime.now):
        theme, dp = app.theme, app.dpi.px
        super().__init__(parent, bg=theme.panel_background)
        self.app, self.close, self.clock = app, close, clock
        self.revision, self.refreshed_at = -1, 0
        self.rows = {}
        self.brief_rows = {}
        self.active_filter = None
        width = dp(500)

        self.detail = tk.StringVar(value=self.EMPTY_DETAIL)
        self.details_title = tk.StringVar(value=self.EMPTY_DETAIL)
        self.details_content = tk.StringVar()
        detail_card, detail_frame = card(self, app, padding=9)
        self.detail_card = detail_card
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
        self.open_button, open_control = form_button(self.selection_actions, app, "打开事项", self.open_event,
                                                     primary=True, font=reminder_font(app), compact=True)
        open_control.pack(side="left")
        self.cancel_button, cancel = form_button(self.selection_actions, app, "取消稍后提醒", self.cancel_snooze,
                                                font=reminder_font(app), compact=True)
        cancel.pack(side="left", padx=(dp(6), 0))
        self.details_button, details = form_button(self.selection_actions, app, "通知明细", self.open_details,
                                                  font=reminder_font(app), compact=True)
        details.pack(side="left", padx=(dp(6), 0))
        self.detail.trace_add("write", self._render_detail)
        self.metrics = tk.Frame(self, bg=theme.panel_background)
        self.metrics.pack(fill="x", pady=(0, dp(8)))
        self.metrics.columnconfigure((0, 1, 2), weight=1, uniform="reminder-count")
        self.counts = {}
        self.filter_cards = {}
        colors = (theme.danger, theme.event_type_urgent, theme.accent)
        for column, (group, color) in enumerate(zip(self.GROUPS, colors)):
            count = self.counts[group] = tk.StringVar(self, value="0")
            text = tk.StringVar(self, value=f"0   {self.GROUPS[group]}")
            def update_caption(*_args, count=count, text=text, label=self.GROUPS[group]):
                text.set(f"{count.get()}   {label}")
            count.trace_add("write", update_caption)
            hub = PlanFilterCard(self.metrics, app, color, text, lambda group=group: self.toggle_filter(group))
            self.filter_cards[group] = hub
            hub.surface.grid(row=0, column=column, sticky="ew", padx=(dp(6) if column else 0, 0))
        self.table_card, table_body = card(self, app, padding=8)
        self.table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_body, bg=theme.workspace_detail)
        toolbar.pack(fill="x", pady=(0, dp(6)))
        self.list_title = tk.StringVar(self, value="全部提醒计划")
        tk.Label(toolbar, textvariable=self.list_title, bg=theme.workspace_detail, fg=theme.text_primary,
                 anchor="w", font=reminder_font(app, 10, "bold")).pack(side="left")
        self.refresh_button, refresh = form_button(toolbar, app, "刷新", self.refresh,
                                                   font=reminder_font(app), compact=True)
        refresh.pack(side="right")
        self.show_all_button, self._show_all_control = form_button(
            toolbar, app, "查看全部", lambda: self.set_filter(None), font=reminder_font(app), compact=True)
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
        self.tree = ttk.Treeview(frame, columns=tuple(key for key, _text, _size in self.COLUMNS), show="headings",
                                 selectmode="browse", style="ReminderPlan.Treeview")
        for identifier, text, minimum in self.COLUMNS:
            self.tree.heading(identifier, text=text, anchor="w")
            self.tree.column(identifier, width=dp(minimum), minwidth=1, stretch=False)
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
        base = [dp(minimum) for _key, _title, minimum in self.COLUMNS]
        total = sum(base)
        if event.width < total:
            widths = [max(1, width * event.width // total) for width in base]
        else:
            extra = event.width - total
            weights = (.53, .08, .15, .13, .11)
            widths = [width + int(extra * weight) for width, weight in zip(base, weights)]
        widths[0] += max(0, event.width - sum(widths))
        for (key, _title, _minimum), width in zip(self.COLUMNS, widths):
            self.tree.column(key, width=width)

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

    def toggle_filter(self, group):
        self.set_filter(None if group == self.active_filter else group)

    def set_filter(self, group):
        if group == self.active_filter:
            return
        previous = self.active_filter
        self.active_filter = group
        for key, hub in self.filter_cards.items():
            hub.set_selected(key == group)
        if group:
            self._show_all_control.pack(side="right", padx=self.app.dpi.px(6))
        else:
            if self.show_all_button.focus_get() is self.show_all_button:
                self.filter_cards[previous].focus_set()
            self._show_all_control.pack_forget()
        self.refresh(reset_scroll=True)

    def _title(self, visible_count):
        label = self.GROUPS.get(self.active_filter, "全部提醒计划")
        self.list_title.set(f"{label} · {visible_count} 条")

    def refresh(self, *, reset_scroll=False):
        selected = self.tree.selection()
        scroll = self.tree.yview()
        try:
            now = self.clock()
            engine = self.app.reminder_engine
            overview = engine.overview(now)
            batches = engine._state()["batches"] if self.active_filter is None else {}
        except (OSError, ValueError) as error:
            self.summary.set("无法读取提醒计划：" + str(error))
            for count in self.counts.values():
                count.set("—")
            self.rows = {}
            self.brief_rows = {}
            self.tree.delete(*self.tree.get_children())
            self._title(0)
            self.empty_label.configure(text="暂时无法读取提醒计划。请刷新后重试。")
            self.empty_label.place(relx=.5, rely=.5, anchor="center")
            self.show_selection()
            return

        counts = Counter(row.group for row in overview.rows)
        for group, count in self.counts.items():
            count.set(str(counts[group]))
        self.rows = {}
        self.brief_rows = {}
        records = {}
        kinds = {event.id: event.event_type for event in self.app.store.events}
        for row in overview.rows:
            if self.active_filter and row.group != self.active_filter:
                continue
            key = f"event:{row.event_id}"
            self.rows[key] = row
            clock_format = "%m-%d %H:%M" if row.when.year == now.year else "%Y-%m-%d %H:%M"
            kind = "DDL" if row.ddl else EVENT_TYPE_LABELS.get(kinds.get(row.event_id), "日程")
            title = f"{kind} · {' '.join(row.title.splitlines())}"
            records[key] = (title, self.GROUPS.get(row.group, row.status), row.when.strftime(clock_format),
                            row.windows, row.qq)

        briefs = ((key, batch) for key, batch in batches.items() if batch["kind"] == "brief")
        for identifier, batch in sorted(briefs, key=lambda item: item[1]["when"], reverse=True)[:30]:
            key = f"brief:{identifier}"
            self.brief_rows[key] = identifier
            when = batch["when"].replace("T", " ")[:16]
            if when.startswith(f"{now.year}-"):
                when = when[5:]
            records[key] = (BRIEF_TITLES[batch.get("brief_slot", "morning")], "简报记录", when, *batch_channel_statuses(batch))

        old_keys = self.tree.get_children()
        self.tree.delete(*(key for key in old_keys if key not in records))
        for index, (key, values) in enumerate(records.items()):
            tags = ("alternate",) if index % 2 else ()
            if key == self._hovered:
                tags = ("hover", *tags)
            if self.tree.exists(key):
                self.tree.item(key, values=values, tags=tags)
                if self.tree.index(key) != index:
                    self.tree.move(key, "", index)
            else:
                self.tree.insert("", "end", iid=key, values=values, tags=tags)
        if reset_scroll:
            self.tree.yview_moveto(0)
            if selected and selected[0] in records:
                self.tree.see(selected[0])
        elif scroll:
            self.tree.yview_moveto(scroll[0])
        if self._hovered not in records:
            self._hovered = None

        self._title(len(records))
        prefix = "" if overview.enabled else "默认即时提醒关闭 · "
        self.summary.set(f"{prefix}Windows 已提交 {overview.submitted} · QQ 已发送 {overview.qq_sent}")
        if records:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="此类暂无提醒计划" if self.active_filter else "暂无提醒计划")
            self.empty_label.place(relx=.5, rely=.5, anchor="center")
        self.revision = engine.revision
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
            self.detail.set(self.EMPTY_DETAIL)

    def _render_detail(self, *_args):
        detail = self.detail.get()
        if self._compact and detail == self.EMPTY_DETAIL:
            self.detail_card.pack_forget()
        elif not self.detail_card.winfo_manager():
            self.detail_card.pack(side="bottom", fill="x", pady=(self.app.dpi.px(8), 0), before=self.metrics)
        lines = detail.splitlines()
        self.details_title.set(lines[0] if len(lines) > 1 else detail)
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

        _, close_control = form_button(dialog, self.app, "关闭", close, font=reminder_font(self.app))
        close_control.pack(side="bottom", anchor="e", padx=dp(14), pady=dp(10))
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
                changed = self.app.reminder_engine.cancel_snooze(row.event_id, key=row.plan_key)
                self.refresh()
                self.app._show_feedback("已取消稍后提醒" if changed else "提醒已变化，请重新选择")
            except (OSError, ValueError) as error:
                self.detail.set("取消未完成：" + str(error))

    def show_notice(self, identifier):
        if self.active_filter and not any(row.batch == identifier for row in self.rows.values()):
            self.set_filter(None)
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
