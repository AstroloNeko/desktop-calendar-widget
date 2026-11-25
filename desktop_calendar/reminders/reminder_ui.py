"""Tk coordination and lightweight settings, separate from reminder calculations."""
from collections import deque
from copy import deepcopy
from datetime import datetime
import queue
import re
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

from calendar_core import normalize_reminder_time
from notification_backend import WindowsNotificationBackend
from desktop_calendar.reminders.reminder_engine import ReminderEngine, ReminderError, LEADS, parse_activation
from desktop_calendar.updates.update_dialog import dialog_geometry
from desktop_calendar.reminders.qq_reminders import QQReminderBackend
from desktop_calendar.reminders.notification_model import BRIEF_GROUP_LABELS, brief_items, brief_settings
from desktop_calendar.ui.themed_controls import form_button, form_entry, themed_check


def reminder_button(parent, app, text, command, *, primary=False):
    """Small native-keyboard control with the existing theme's interaction colors."""
    theme, dp = app.theme, app.dpi.px
    background = theme.accent if primary else theme.control_background
    foreground = theme.text_on_accent if primary else theme.control_text
    button = tk.Button(parent, text=text, command=command, font=(app.search_font, 9),
                       bg=background, fg=foreground, activebackground=theme.accent_hover if primary else theme.control_pressed,
                       activeforeground=foreground, disabledforeground=theme.text_disabled,
                       relief="flat", bd=0, highlightthickness=dp(1),
                       highlightbackground=theme.accent if primary else theme.control_border,
                       highlightcolor=theme.input_focus, padx=dp(10), pady=dp(5), cursor="hand2")
    button.bind("<Enter>", lambda _e: button.configure(bg=(theme.accent_hover if primary else theme.control_hover))
                if str(button.cget("state")) != "disabled" else None)
    button.bind("<Leave>", lambda _e: button.configure(bg=background))
    return button


class NativeReminderMixin:
    def _init_native_reminders(self, resource_root):
        self.notification_backend = WindowsNotificationBackend(resource_root, self.store.data_file)
        def remote_worker():
            runtime = getattr(self, "chat_runtime", None)
            return runtime.core.remote if runtime else None
        self.qq_reminder_backend = QQReminderBackend(remote_worker)
        self.reminder_engine = ReminderEngine(self.store, self.notification_backend, self.qq_reminder_backend)
        self._qq_test_token = None
        self._qq_test_status = "尚未发送 QQ 测试通知"
        self.notification_settings_window = None
        self.notification_plan_window = None
        self._reminder_views_dirty = True
        self._notification_test_token = None
        self._notification_test_pending = False
        self._notification_test_clicked = False
        self._notification_test_deadline = 0
        self._notification_test_status = "尚未发送测试通知"
        self._reminder_refresh_job = None
        self._pending_notification_actions = deque(maxlen=16)
        self._notification_activation_times = {}

    def invalidate_reminders(self):
        engine = getattr(self, "reminder_engine", None)
        if engine is None or self.lifecycle.exiting:
            return
        engine.invalidate()
        self._reminder_views_dirty = True
        if self._reminder_refresh_job is None:
            def flush():
                self._reminder_refresh_job = None
                self._native_reminders_tick()
            self._reminder_refresh_job = self.after_idle(flush)

    def _native_reminders_tick(self):
        engine = getattr(self, "reminder_engine", None)
        if engine is None or self.lifecycle.exiting:
            return 15000
        try:
            return engine.tick(datetime.now())
        except Exception as error:
            engine.error = str(error)
            return 2000

    def _schedule_calendar_reminders(self):
        try:
            self._native_reminders_tick()
        finally:
            if not self.lifecycle.exiting:
                self.after(2000, self._schedule_calendar_reminders)

    def poll_native_notifications(self):
        engine = getattr(self, "reminder_engine", None)
        if engine is None or self.lifecycle.exiting:
            return
        for _ in range(32):
            try:
                kind, value = self.notification_backend.events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "delivery":
                    key, success = value
                    engine.acknowledge(key, success, datetime.now())
                elif kind == "notice_delivery":
                    key, attempt, status, reason = value
                    engine.delivery.acknowledge(key, "windows", attempt, status, datetime.now(), reason)
                elif kind == "test_error":
                    token, error = value
                    if token == self._notification_test_token:
                        self._set_test_status("测试未成功：" + str(error))
                elif kind == "test_delivery":
                    token, success = value
                    if token == self._notification_test_token:
                        self._notification_test_pending = False
                        if success and not self._notification_test_clicked:
                            self._set_test_status("已提交给 Windows；请检查右下角或 Win+N。这不代表系统已显示通知。")
                        else:
                            self._set_test_status(self._notification_test_status)
                elif kind in ("status", "error"):
                    engine.error = value if kind == "error" else ""
                    dialog = self.notification_settings_window
                    if dialog and dialog.winfo_exists():
                        dialog.status.set(value)
                elif kind == "native":
                    event_kind, arguments = value
                    if event_kind == 1:
                        if self._notification_test_token and arguments == "test=" + self._notification_test_token:
                            self._notification_test_pending = False
                            self._notification_test_clicked = True
                            self._set_test_status("已收到测试通知点击，通知接收与回调正常。")
                            continue
                        action = parse_activation(arguments)
                        if isinstance(arguments, str) and re.fullmatch(r"batch=[0-9a-f]{64}", arguments):
                            action = {"batch": arguments[6:]}
                        now = time.monotonic()
                        previous = self._notification_activation_times.get(arguments, -10)
                        if action and now - previous > 2:
                            self._notification_activation_times = {k: v for k, v in self._notification_activation_times.items() if now - v < 5}
                            self._notification_activation_times[arguments] = now
                            self._pending_notification_actions.append(action)
                    elif event_kind == 2:
                        tag = arguments.split(":", 1)[0]
                        if self._notification_test_token and tag == "test" + self._notification_test_token[:12]:
                            self._notification_test_pending = False
                            self._set_test_status("Windows 报告测试通知失败，请检查通知权限或专注设置。")
                            continue
                        engine.delivery.acknowledge_tag(tag, datetime.now(), "Windows 报告通知失败")
            except (OSError, ValueError, RuntimeError) as error:
                engine.error = str(error)
        if self._notification_test_pending and time.monotonic() >= self._notification_test_deadline:
            self._notification_test_pending = False
            self._set_test_status("暂未收到 Windows 提交结果；请检查通知设置后重试，不能据此确认已显示。")
        self._poll_qq_reminders(engine)
        self._refresh_reminder_views()
        if not self._pending_notification_actions:
            return
        action = self._pending_notification_actions[0]
        blocker = self._lifecycle_blocker()
        if blocker is not None and not ("batch" in action and blocker is self.notification_settings_window):
            return
        self._pending_notification_actions.popleft()
        try:
            if "batch" in action:
                if engine.batch_record(action["batch"]) is not None:
                    if not self.lifecycle.hidden or self.restore_from_tray():
                        self.open_reminder_plans(action["batch"])
                return
            if not engine.config()["enabled"]:
                return
            event = self.store.event_by_id(action["event"])
            if event is None:
                if self.restore_from_tray():
                    self._show_feedback("这项日程已删除")
                return
            if not engine.activation_is_current(event.id, action["plan"]):
                if self.restore_from_tray():
                    self._show_feedback("日程已修改、完成或提醒已取消")
                return
            if action["action"] != "open":
                engine.snooze(event.id, action["plan"], int(action["action"]), datetime.now())
                return
            if not self.restore_from_tray():
                self._pending_notification_actions.appendleft(action)
                return
            # Exactly the search reveal path: effective date, filter escape,
            # scroll/highlight and the existing modal editor, no second CRUD.
            self._activate_search_result(SimpleNamespace(kind="event", key=event.id))
        except (OSError, ValueError, RuntimeError) as error:
            engine.error = str(error)

    def _poll_qq_reminders(self, engine):
        from desktop_calendar.reminders.notification_dispatch import STATUS_LABELS
        for _ in range(32):
            try:
                key, attempt, status, reason = self.qq_reminder_backend.events.get_nowait()
            except queue.Empty:
                break
            if key == self._qq_test_token:
                self._qq_test_token = None
                self._set_qq_test_status(STATUS_LABELS[status] + (" · " + reason if reason else ""))
            else:
                try:
                    engine.delivery.acknowledge(key, "qq", attempt, status, datetime.now(), reason)
                except (OSError, ValueError, RuntimeError) as error:
                    engine.error = str(error)

    def _set_qq_test_status(self, text):
        self._qq_test_status = text
        dialog = self.notification_settings_window
        if dialog and dialog.winfo_exists():
            dialog.qq_test_status.set(text)
            dialog.qq_test_button.configure(state="disabled" if self._qq_test_token else "normal")

    def test_qq_reminder(self):
        if self.lifecycle.exiting or self._qq_test_token:
            return
        try:
            self._qq_test_token = self.qq_reminder_backend.test()
            self._set_qq_test_status("正在发送 QQ 测试通知")
        except (ValueError, RuntimeError) as error:
            self._set_qq_test_status(str(error))

    def open_reminder_settings(self):
        if self.notification_settings_window and self.notification_settings_window.winfo_exists():
            return
        if self._active_modal_window() is not None or self.update_busy:
            return
        try:
            self.reminder_engine.config()
            self.reminder_engine._state()
        except ValueError as error:
            self._show_feedback(str(error))
            return
        self.notification_settings_window = ReminderSettingsDialog(self)

    def open_reminder_plans(self, batch=None):
        self.open_reminder_settings()
        dialog = self.notification_settings_window
        if dialog:
            dialog.show_plans(batch)

    def _set_test_status(self, text):
        self._notification_test_status = text
        dialog = self.notification_settings_window
        if dialog and dialog.winfo_exists():
            dialog.test_status.set(text)
            dialog.test_button.configure(state="disabled" if self._notification_test_pending else "normal")

    def test_native_notification(self):
        # User-requested synthetic test, even when real event reminders are off.
        if self.lifecycle.exiting or self._notification_test_pending:
            return
        try:
            self._notification_test_token = self.notification_backend.send_test()
            self._notification_test_pending = True
            self._notification_test_clicked = False
            self._notification_test_deadline = time.monotonic() + 15
            self._set_test_status("正在提交测试通知；不包含事项内容，不改变提醒开关或计划。")
        except (OSError, ValueError, RuntimeError) as error:
            self._set_test_status("测试未成功：" + str(error))

    def _refresh_reminder_views(self):
        dialog = self.notification_plan_window
        if dialog and dialog.winfo_exists():
            if (self._reminder_views_dirty or dialog.revision != self.reminder_engine.revision
                    or time.monotonic() - dialog.refreshed_at >= 15):
                dialog.refresh()
                self._reminder_views_dirty = False

    def stop_native_reminders(self):
        backend = getattr(self, "notification_backend", None)
        if backend is not None:
            self.qq_reminder_backend.close()
            backend.close()


class ReminderSettingsDialog(tk.Toplevel):
    MODES = {"使用默认提醒设置": "default", "不提醒": "off", "自定义日期提醒": "custom"}

    def __init__(self, app):
        super().__init__(app)
        self.withdraw()
        self.app, self._closing = app, False
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        self.title("后台与提醒")
        self.configure(bg=theme.panel_background)
        area = app.dpi.work_area()
        width, height, x, y = dialog_geometry(area, app.dpi.dpi,
                                             (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, dp(320)), min(height, dp(260)))
        initial_status = app.reminder_engine.error or ""
        self.status = tk.StringVar(value=initial_status)
        self.test_status = tk.StringVar(value=app._notification_test_status)
        self.qq_test_status = tk.StringVar(value=app._qq_test_status)
        config = app.reminder_engine.config()
        self.aggregate = tk.BooleanVar(value=config["aggregate"])
        self.merge_backlog = tk.BooleanVar(value=config["merge_backlog"])
        self.windows = tk.BooleanVar(value=config["windows"])
        self.qq = tk.BooleanVar(value=config["qq"])
        self.brief = {key: (tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value)))
                      for key, value in config["brief"].items()}
        self.rules = deepcopy(config["events"])
        self.enabled = tk.BooleanVar(value=config["enabled"])
        self.private = tk.BooleanVar(value=config["private"])
        self.clock = tk.StringVar(value=config["time"])
        self.general = tk.StringVar(value=str(config["general_days"]))
        self.ddl = tk.StringVar(value=str(config["ddl_days"]))
        self.current_id = None
        self.event_labels = {f"{str(event.title)[:60]} · {event.id[-8:]}": event.id for event in app.store.events}
        self.event_choice = tk.StringVar()
        self.rule_mode = tk.StringVar(value="使用默认提醒设置")
        self.rule_clock = tk.StringVar(value="09:00")
        self.rule_days = tk.StringVar(value="0")
        actions = tk.Frame(self, bg=theme.panel_background)
        actions.pack(side="bottom", fill="x", padx=dp(14), pady=dp(10))
        def button(parent, text, command, **options):
            return reminder_button(parent, app, text, command, **options)
        _, close = form_button(actions, app, "关闭", self.close, quiet=True)
        self.save_button, save = form_button(actions, app, "保存设置", self.save, primary=True)
        save.pack(side="right")
        close.pack(side="right", padx=(0, dp(5)))
        self.footer_status = tk.Label(actions, textvariable=self.status, bg=theme.panel_background,
                                      fg=theme.text_secondary, wraplength=width - dp(184), anchor="w",
                                      justify="left", font=(font, 8))
        self.footer_status.pack(side="left", fill="x", expand=True)
        tk.Frame(self, bg=theme.divider, height=dp(1)).pack(side="bottom", fill="x")
        header = tk.Frame(self, bg=theme.panel_background)
        header.pack(fill="x", padx=dp(16), pady=(dp(14), dp(10)))
        tk.Label(header, text="后台与提醒", bg=theme.panel_background, fg=theme.text_primary,
                 font=(font, 14, "bold"), anchor="w").pack(fill="x")
        tk.Label(header, text="后台运行 · 日程通知 · 每日行程", bg=theme.panel_background,
                 fg=theme.text_secondary, font=(font, 9), anchor="w").pack(fill="x", pady=(dp(4), 0))
        style = ttk.Style(self)
        style.configure("CalendarReminders.TNotebook", background=theme.panel_background, borderwidth=0)
        if "ReminderTabs.tab" not in style.element_names():
            style.element_create("ReminderTabs.tab", "from", "clam", "Notebook.tab")
        style.layout("CalendarReminders.TNotebook.Tab", [("ReminderTabs.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"sticky": "nswe", "children": [("Notebook.label", {"side": "top", "sticky": ""})]})
        ]})])
        style.configure("CalendarReminders.TNotebook.Tab", font=(font, 9), padding=(dp(8), dp(5)),
                        borderwidth=1, focuscolor=theme.input_focus)
        style.map("CalendarReminders.TNotebook.Tab",
                  background=[("selected", theme.accent_soft), ("active", theme.control_hover),
                              ("!selected", theme.panel_background)],
                  bordercolor=[("selected", theme.accent), ("!selected", theme.panel_background)],
                  lightcolor=[("selected", theme.accent_soft), ("!selected", theme.panel_background)],
                  darkcolor=[("selected", theme.accent_soft), ("!selected", theme.panel_background)],
                  foreground=[("selected", theme.text_primary), ("!selected", theme.text_secondary)])
        self.tabs = ttk.Notebook(self, style="CalendarReminders.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=dp(10))
        self.tabs.enable_traversal()
        pages = []
        for title in ("提醒规则", "每日简报", "通知通道", "提醒计划"):
            page = tk.Frame(self.tabs, bg=theme.panel_background)
            self.tabs.add(page, text=title)
            pages.append(page)
        def scrollable(frame):
            canvas = tk.Canvas(frame, bg=theme.panel_background, highlightthickness=0)
            scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview,
                                      style="Global.Vertical.TScrollbar")
            def reveal_scroll(first, last):
                scrollbar.set(first, last)
                if float(first) == 0 and float(last) == 1:
                    if scrollbar.winfo_manager():
                        scrollbar.pack_forget()
                elif not scrollbar.winfo_manager():
                    scrollbar.pack(side="right", fill="y", before=canvas)
            canvas.configure(yscrollcommand=reveal_scroll)
            canvas.pack(side="left", fill="both", expand=True)
            content = tk.Frame(canvas, bg=theme.panel_background, padx=dp(6), pady=dp(8))
            window = canvas.create_window(0, 0, anchor="nw", window=content)
            content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
            return content, canvas
        content, self.scroll_canvas = scrollable(pages[0])
        brief_content, self.brief_canvas = scrollable(pages[1])
        channel_content, self.channel_canvas = scrollable(pages[2])
        canvases = dict(zip(map(str, pages[:3]), (self.scroll_canvas, self.brief_canvas, self.channel_canvas)))
        def wheel(event):
            canvas = canvases.get(self.tabs.select())
            if canvas is not None:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
        self.bind("<MouseWheel>", wheel)
        wrapped = []
        field_labels = []
        def section(title):
            block = tk.Frame(content, bg=theme.workspace_detail, padx=dp(11), pady=dp(9),
                             highlightthickness=dp(1), highlightbackground=theme.divider)
            block.pack(fill="x", pady=(0, dp(8)))
            tk.Label(block, text=title, anchor="w", bg=theme.workspace_detail,
                     fg=theme.text_primary, font=(font, 10, "bold")).pack(fill="x", pady=(0, dp(4)))
            return block

        def divider():
            tk.Frame(body, bg=theme.divider, height=dp(1)).pack(fill="x", pady=dp(6))
        def label(text):
            widget = tk.Label(body, text=text, anchor="w", justify="left", wraplength=width - dp(76),
                              bg=theme.workspace_detail, fg=theme.text_secondary, font=(font, 8))
            widget.pack(fill="x", pady=dp(3))
            wrapped.append(widget)
            return widget
        def check(text, variable, *, inline=None):
            widget = themed_check(inline or body, theme, dp, text=text, variable=variable, bg=theme.workspace_detail,
                           fg=theme.text_primary, activebackground=theme.workspace_detail,
                           activeforeground=theme.accent, selectcolor=theme.input_background,
                           font=(font, 9), cursor="hand2", anchor="w", wraplength=width-dp(90))
            if inline is None:
                widget.pack(fill="x", pady=dp(2))
                wrapped.append(widget)
            else:
                widget.pack(side="left", padx=(0, dp(9)), pady=dp(2))
        def field(text, variable, values=None, *, wide=False):
            row = tk.Frame(body, bg=theme.workspace_detail)
            row.pack(fill="x", pady=dp(3))
            caption = tk.Label(row, text=text, anchor="w", justify="left", bg=theme.workspace_detail,
                               fg=theme.text_primary, font=(font, 9), wraplength=dp(230))
            caption.pack(side="top" if wide else "left", anchor="w", pady=(0, dp(3)) if wide else 0)
            if not wide:
                field_labels.append(caption)
            if values is not None:
                widget = control = ttk.Combobox(row, textvariable=variable, values=values, state="readonly",
                                                 font=(font, 9), width=8)
            else:
                widget, control = form_entry(row, app, variable, width=9, rounded=False)
                widget.configure(justify="center")
            control.pack(side="top" if wide else "right", fill="x" if wide or values is not None else "none",
                         expand=not wide and values is not None,
                         padx=(dp(12), 0) if values is not None and not wide else 0,
                         ipady=dp(3))
            return widget
        body = section("日程提醒")
        check("启用日程提醒", self.enabled)
        field("默认提醒时间", self.clock)
        field("一般 / 紧急 · 提前天数", self.general, tuple(map(str, LEADS)))
        field("DDL / 末日 DDL · 提前天数", self.ddl, tuple(map(str, LEADS)))
        label("有具体时间的事项沿用事项编辑器中的提醒提前量。")
        body = section("通知聚合")
        check("合并同一分钟触发的同类提醒", self.aggregate)
        check("恢复运行时合并有效的积压提醒", self.merge_backlog)
        body = section("单事项规则")
        selector = field("选择事项", self.event_choice, tuple(self.event_labels), wide=True)
        selector.bind("<<ComboboxSelected>>", self.select_event)
        mode = field("提醒规则", self.rule_mode, tuple(self.MODES))
        mode.bind("<<ComboboxSelected>>", lambda _e: self.update_custom())
        self.custom = tk.Frame(body, bg=theme.workspace_detail)
        self.custom.pack(fill="x")
        self.custom.columnconfigure((0, 1), weight=1)
        for column, text in enumerate(("提前天数", "本地提醒时间 · HH:MM")):
            tk.Label(self.custom, text=text, bg=theme.workspace_detail, fg=theme.text_secondary,
                     font=(font, 8), anchor="w").grid(row=0, column=column, sticky="w", pady=(dp(4), dp(3)))
        ttk.Combobox(self.custom, textvariable=self.rule_days, values=tuple(map(str, LEADS)), width=8,
                     state="readonly", font=(font, 9)).grid(row=1, column=0, sticky="ew", padx=(0, dp(6)))
        _, clock = form_entry(self.custom, app, self.rule_clock, width=8, rounded=False)
        clock.grid(row=1, column=1, sticky="ew", ipady=dp(3))
        self.cancel_button = button(body, "取消该事项的稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(anchor="w", pady=(dp(6), 0))
        label("规则只影响提醒时间。")
        content = brief_content
        body = section("每日行程简报")
        check("启用每日行程简报", self.brief["enabled"])
        field("每日发送时间", self.brief["time"])
        divider()
        for key, caption in (("timed", "当天有具体时间的日程"), ("untimed", "全天及无具体时间的事项"),
                             ("unfinished", "当天未完成的事项"), ("upcoming", "近期未完成的 DDL")):
            check(caption, self.brief[key])
        field("DDL 展望天数", self.brief["days"], tuple(map(str, range(1, 31))))
        check("已逾期未完成的 DDL", self.brief["overdue"])
        check("无事项时仍发送简报", self.brief["send_empty"])
        divider()
        channel_choices = tk.Frame(body, bg=theme.workspace_detail)
        channel_choices.pack(fill="x")
        check("Windows 通知", self.brief["windows"], inline=channel_choices)
        check("QQ 推送", self.brief["qq"], inline=channel_choices)
        label("有效补发时间为当天计划发送后的四小时内。")
        body = section("简报预览")
        self.preview_heading = tk.Label(body, bg=theme.workspace_detail, fg=theme.text_primary,
                                        font=(font, 10, "bold"), anchor="w", justify="left")
        self.preview_heading.pack(fill="x", pady=(dp(2), dp(3)))
        self.preview_body = label("")
        self.preview_body.configure(font=(font, 9), fg=theme.text_primary)
        for control in (*self.brief.values(), self.private):
            control.trace_add("write", self.refresh_preview)
        content = channel_content
        body = section("Windows 系统通知")
        check("启用 Windows 通知", self.windows)
        self.test_button = button(body, "发送 Windows 测试通知", app.test_native_notification)
        self.test_button.configure(state="disabled" if app._notification_test_pending else "normal")
        self.test_button.pack(fill="x", pady=dp(4))
        self.test_status_label = label("")
        self.test_status_label.configure(textvariable=self.test_status)
        body = section("QQ 主动推送")
        check("启用 QQ 通知", self.qq)
        label("使用当前已核验的远程服务绑定。")
        check("隐私保护：隐藏事项标题与详情", self.private)
        self.qq_test_button = button(body, "发送 QQ 测试通知", app.test_qq_reminder)
        self.qq_test_button.configure(state="disabled" if app._qq_test_token else "normal")
        self.qq_test_button.pack(fill="x", pady=dp(4))
        label("").configure(textvariable=self.qq_test_status)
        body = section("后台运行")
        label("关闭主窗口 → " + ("隐藏到系统托盘" if app.lifecycle.close_hides else "彻底退出"))
        label("全局视图最小化 → 任务栏")
        label("睡眠恢复后检查有效积压。双击托盘图标可恢复窗口。")
        from desktop_calendar.reminders.reminder_center import ReminderPlanPanel
        self.plan = ReminderPlanPanel(pages[3], app, self.close)
        self.plan.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed, add="+")
        def rewrap(event):
            if event.widget is self:
                # Labels inside the scrolling area must follow the viewport,
                # not their initial size (small work areas / manual resizing).
                for widget in wrapped:
                    widget.configure(wraplength=max(dp(120), event.width - dp(76)))
                for widget in field_labels:
                    widget.configure(wraplength=max(dp(110), event.width * 2 // 5 - dp(16)))
                self.footer_status.configure(wraplength=max(dp(88), event.width - dp(184)))
        self.bind("<Configure>", rewrap, add="+")
        self.update_custom()
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        app.present_modal(self, app)
        self.grab_set()

    def update_custom(self):
        if self.MODES[self.rule_mode.get()] == "custom":
            self.custom.pack(fill="x", before=self.cancel_button)
        else:
            self.custom.pack_forget()

    def store_rule(self):
        if not self.current_id:
            return
        mode = self.MODES[self.rule_mode.get()]
        if mode == "default":
            self.rules.pop(self.current_id, None)
        elif mode == "off":
            self.rules[self.current_id] = {"mode": "off"}
        else:
            clock = normalize_reminder_time(self.rule_clock.get())
            if clock is None:
                raise ReminderError("请将单事项提醒时间填写为 HH:MM，例如 09:00。")
            self.rules[self.current_id] = {"mode": "custom", "time": clock, "days": int(self.rule_days.get())}

    def select_event(self, _event=None):
        try:
            self.store_rule()
        except ValueError as error:
            self.status.set(str(error))
            self.event_choice.set(next((label for label, identifier in self.event_labels.items()
                                        if identifier == self.current_id), ""))
            return
        self.current_id = self.event_labels.get(self.event_choice.get())
        rule = self.rules.get(self.current_id, {})
        self.rule_mode.set(next(label for label, mode in self.MODES.items() if mode == rule.get("mode", "default")))
        self.rule_clock.set(rule.get("time", self.clock.get()))
        self.rule_days.set(str(rule.get("days", 0)))
        self.update_custom()

    def save(self):
        try:
            self.store_rule()
            clock = normalize_reminder_time(self.clock.get())
            if clock is None:
                raise ReminderError("请将默认提醒时间填写为 HH:MM，例如 09:00。")
            brief = {key: control.get() for key, control in self.brief.items()}
            brief["time"] = normalize_reminder_time(brief["time"])
            if brief["time"] is None:
                raise ReminderError("请将简报发送时间填写为 HH:MM，例如 08:00。")
            brief["days"] = int(brief["days"])
            self.app.reminder_engine.configure({"enabled": self.enabled.get(), "private": self.private.get(),
                                                "time": clock, "general_days": int(self.general.get()),
                                                "ddl_days": int(self.ddl.get()), "events": self.rules,
                                                "aggregate": self.aggregate.get(), "merge_backlog": self.merge_backlog.get(),
                                                "windows": self.windows.get(), "qq": self.qq.get(), "brief": brief})
            self.app.invalidate_reminders()
            self.close()
            return True
        except (OSError, ValueError) as error:
            self.status.set(f"设置未保存：{error}")
            return False

    def tab_changed(self, _event=None):
        if self._closing:
            return
        current = self.tabs.index("current")
        self.app.notification_plan_window = self.plan if current == 3 else None
        if current == 3:
            self.plan.refresh()
        elif current == 1:
            self.refresh_preview()

    def refresh_preview(self, *_args):
        if self._closing or self.tabs.index("current") != 1:
            return
        raw = {key: variable.get() for key, variable in self.brief.items()}
        raw["days"] = int(raw["days"]) if raw["days"].isdecimal() else None
        clock = normalize_reminder_time(raw["time"])
        if clock is None:
            self.preview_heading.configure(text="请填写有效的发送时间")
            self.preview_body.configure(text="")
            return
        today = datetime.now()
        hour, minute = map(int, clock.split(":"))
        preview_time = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        items = brief_items(self.app.store, preview_time, brief_settings(raw), self.private.get())
        self.preview_heading.configure(text=f"{today:%m-%d} · {clock} · 今天简报共 {len(items)} 项")
        if not items:
            text = "今天没有符合简报条件的事项。"
        elif self.private.get():
            text = "隐私保护已开启，通知预览仅显示事项总数。"
        else:
            lines = []
            group = None
            for item in items[:6]:
                if item["group"] != group:
                    group = item["group"]
                    lines.append(BRIEF_GROUP_LABELS[group])
                context = item.get("time") or (item["date"][5:] if group in ("upcoming", "overdue") else "")
                lines.append("  ".join(filter(None, (context, item["title"].replace("\n", " ")))))
            if len(items) > 6:
                lines.append(f"还有 {len(items) - 6} 项")
            text = "\n".join(lines)
        self.preview_body.configure(text=text)

    def show_plans(self, batch=None):
        self.tabs.select(3)
        self.tab_changed()
        if batch:
            self.plan.show_notice(batch)

    def cancel_snooze(self):
        if not self.current_id:
            return
        try:
            self.app.reminder_engine.cancel_snooze(self.current_id)
            self.status.set("已取消该事项的稍后提醒")
        except (OSError, ValueError) as error:
            self.status.set(f"取消未完成：{error}")

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.app.notification_settings_window = None
        self.app.notification_plan_window = None
        self.destroy()
