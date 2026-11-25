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
from desktop_calendar.ui.themed_controls import SettingsScroll, form_button, form_entry, themed_check
from desktop_calendar.remote_presentation import CONNECTION_LABELS
from desktop_calendar.reminders.reminder_widgets import COMBO, ReminderButton, card, combo_style, notebook_style, reminder_font


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
        self._qq_test_scope = None
        self._qq_test_capability = None
        self._reminder_scan_at = None
        self._reminder_scan_error = ""
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
            now = datetime.now()
            delay = engine.tick(now)
        except Exception as error:
            self._reminder_scan_error = engine.error = str(error)
            return 2000
        self._reminder_scan_at, self._reminder_scan_error = now, ""
        return delay

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
                if self._qq_test_scope:
                    if status == "sent":
                        self._qq_test_capability = self._qq_test_scope, "主动推送接口已通过测试"
                    elif "HTTP 404" in reason:
                        self._qq_test_capability = self._qq_test_scope, "主动推送接口未提供（HTTP 404）"
                    else:
                        self._qq_test_capability = self._qq_test_scope, "最近一次主动推送测试未确认可用"
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
            dialog.refresh_channel_status()

    def test_qq_reminder(self):
        if self.lifecycle.exiting or self._qq_test_token:
            return
        try:
            binding = self.qq_reminder_backend.binding()
            self._qq_test_scope = binding.scope if binding else None
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
        self.theme, self.dp = app.theme, app.dpi.px
        theme, dp = self.theme, self.dp
        self.clock_now = getattr(app, "_reminder_preview_clock", datetime.now)
        self.title("后台与提醒")
        self.configure(bg=theme.panel_background)
        area = app.dpi.work_area()
        width, height, x, y = dialog_geometry(area, app.dpi.dpi,
            (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()), preferred=(760, 700))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, dp(480)), min(height, dp(480)))
        self.status = tk.StringVar(self, value=app.reminder_engine.error or "")
        self.test_status = tk.StringVar(self, value=app._notification_test_status)
        self.qq_test_status = tk.StringVar(self, value=app._qq_test_status)
        config = app.reminder_engine.config()
        self.aggregate = tk.BooleanVar(self, value=config["aggregate"])
        self.merge_backlog = tk.BooleanVar(self, value=config["merge_backlog"])
        self.windows = tk.BooleanVar(self, value=config["windows"])
        self.qq = tk.BooleanVar(self, value=config["qq"])
        self.brief = {key: (tk.BooleanVar(self, value=value) if isinstance(value, bool)
                           else tk.StringVar(self, value=f"{value} 天" if key == "days" else value))
                      for key, value in config["brief"].items()}
        self.rules = deepcopy(config["events"])
        self.enabled = tk.BooleanVar(self, value=config["enabled"])
        self.private = tk.BooleanVar(self, value=config["private"])
        self.clock = tk.StringVar(self, value=config["time"])
        self.general = tk.StringVar(self, value=f"{config['general_days']} 天")
        self.ddl = tk.StringVar(self, value=f"{config['ddl_days']} 天")
        self.current_id = None
        self.event_labels = {f"{str(event.title)[:60]} · {event.id[-8:]}": event.id for event in app.store.events}
        self.event_choice = tk.StringVar(self)
        self.rule_mode = tk.StringVar(self, value=next(iter(self.MODES)))
        self.rule_clock = tk.StringVar(self, value="09:00")
        self.rule_days = tk.StringVar(self, value="0 天")

        actions = tk.Frame(self, bg=theme.panel_background)
        actions.pack(side="bottom", fill="x", padx=dp(20), pady=dp(9))
        self.save_button, save = form_button(actions, app, "保存设置", self.save, primary=True)
        close_button, close = form_button(actions, app, "关闭", self.close, quiet=True)
        self.save_button.configure(font=reminder_font(app))
        close_button.configure(font=reminder_font(app))
        save.pack(side="right")
        close.pack(side="right", padx=(0, dp(6)))
        footer_status = self._label(actions, variable=self.status, muted=True, size=8)
        footer_status.pack(side="left", fill="x", expand=True)
        footer_status.bind("<Configure>", lambda event: footer_status.configure(wraplength=event.width))
        self._divider(self, side="bottom", pady=0)

        header = tk.Frame(self, bg=theme.panel_background)
        header.pack(fill="x", padx=dp(20), pady=(dp(11), dp(8)))
        self._label(header, "后台与提醒", size=15, bold=True).pack(fill="x")
        subtitle = self._label(header, "日程通知、每日简报与后台运行", muted=True)
        subtitle.pack(fill="x", pady=(dp(3), 0))

        def fit_header(event):
            if event.widget is self:
                if event.height < dp(505):
                    subtitle.pack_forget()
                elif not subtitle.winfo_manager():
                    subtitle.pack(fill="x", pady=(dp(3), 0))
        self.bind("<Configure>", fit_header, add="+")

        style = ttk.Style(self)
        combo_style(style, app)
        self.tabs = ttk.Notebook(self, style=notebook_style(style, app))
        self.tabs.pack(fill="both", expand=True, padx=dp(20), pady=(0, dp(5)))
        self.tabs.enable_traversal()
        self.forms = []
        for title in ("提醒规则", "每日简报", "通知通道"):
            page = tk.Frame(self.tabs, bg=theme.panel_background)
            self.tabs.add(page, text=title)
            form = SettingsScroll(page, app)
            form.pack(fill="both", expand=True, pady=(dp(8), 0))
            self.forms.append(form)
        self.scroll_canvas, self.brief_canvas, self.channel_canvas = (form.canvas for form in self.forms)
        self._build_rules(self.forms[0].content)
        self._build_brief(self.forms[1].content)
        self._build_channels(self.forms[2].content)

        from desktop_calendar.reminders.reminder_center import ReminderPlanPanel
        page = tk.Frame(self.tabs, bg=theme.panel_background)
        self.tabs.add(page, text="提醒计划")
        self.plan = ReminderPlanPanel(page, app, self.close, clock=self.clock_now)
        self.plan.pack(fill="both", expand=True, pady=(dp(8), 0))
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed, add="+")
        self.update_custom()
        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        app.present_modal(self, app)
        self.grab_set()
        self.after(1000, self._status_tick)

    def _label(self, parent, text="", *, variable=None, size=9, bold=False, muted=False, color=None):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget("bg"), anchor="w",
                        justify="left", fg=color or (self.theme.text_secondary if muted else self.theme.text_primary),
                        font=reminder_font(self.app, size, "bold" if bold else "normal"))

    def _note(self, parent, text="", *, variable=None):
        label = self._label(parent, text, variable=variable, muted=True, size=8)
        label.pack(fill="x", pady=(self.dp(4), 0))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(1, e.width-self.dp(2))))
        return label

    def _divider(self, parent, *, side="top", pady=8):
        tk.Frame(parent, height=self.dp(1), bg=self.theme.divider).pack(side=side, fill="x", pady=self.dp(pady))

    def _section(self, parent, title):
        border, body = card(parent, self.app)
        border.pack(fill="x", pady=(0, self.dp(12)))
        self._label(body, title, size=11, bold=True).pack(fill="x", pady=(0, self.dp(7)))
        return body

    def _focus_scroll(self, widget, geometry=None):
        def reveal(_event):
            index = self.tabs.index("current")
            if index < len(self.forms):
                self.forms[index].reveal(geometry or widget)
        widget.bind("<FocusIn>", reveal, add="+")

    def _check(self, parent, title, variable, *, packed=True):
        bg = parent.cget("bg")
        widget = themed_check(parent, self.theme, self.dp, text=title, variable=variable, bg=bg,
                              fg=self.theme.text_primary, activebackground=bg,
                              activeforeground=self.theme.text_primary, font=reminder_font(self.app), anchor="w",
                              cursor="hand2")
        if packed:
            widget.pack(anchor="w", pady=self.dp(3))
        self._focus_scroll(widget)
        return widget

    def _field(self, parent, caption, variable, values=None, *, width=108, stretch=False):
        dp = self.dp
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=dp(4))
        self._label(row, caption).pack(side="left", padx=(0, dp(12)))
        slot = tk.Frame(row, bg=row.cget("bg"), width=dp(width), height=dp(34))
        slot.pack(side="right", fill="x" if stretch else "none", expand=stretch)
        if values is None:
            widget, control = form_entry(slot, self.app, variable, width=8, rounded=False)
            widget.configure(justify="center", font=reminder_font(self.app))
        else:
            widget = control = ttk.Combobox(slot, textvariable=variable, values=values, state="readonly",
                                            width=8, font=reminder_font(self.app), style=COMBO)
        slot.pack_propagate(False)
        control.pack(fill="both", expand=True)
        self._focus_scroll(widget, slot)
        return widget

    @staticmethod
    def _days(value):
        return int(value.split()[0])

    def _build_rules(self, parent):
        leads = tuple(f"{day} 天" for day in LEADS)
        body = self._section(parent, "日程提醒")
        self._check(body, "启用日程提醒", self.enabled)
        self.clock_field = self._field(body, "默认提醒时间", self.clock)
        self.general_field = self._field(body, "一般 / 紧急事项提前天数", self.general, leads)
        self.ddl_field = self._field(body, "DDL / 末日 DDL 提前天数", self.ddl, leads)
        self._note(body, "有具体时间的事项沿用事项编辑器中的提醒提前量。")

        body = self._section(parent, "通知聚合")
        self._check(body, "合并同一分钟触发的同类提醒", self.aggregate)
        self._check(body, "恢复运行时合并有效的积压提醒", self.merge_backlog)
        self._note(body, "聚合后可以从一条通知查看其中的所有事项。")

        body = self._section(parent, "单事项规则")
        selector = self._field(body, "选择事项", self.event_choice, tuple(self.event_labels), stretch=True)
        selector.bind("<<ComboboxSelected>>", self.select_event)
        self.mode_field = self._field(body, "提醒规则", self.rule_mode, tuple(self.MODES), width=178)
        self.mode_field.bind("<<ComboboxSelected>>", lambda _e: self.update_custom())
        self.custom = tk.Frame(body, bg=body.cget("bg"))
        self._field(self.custom, "提前天数", self.rule_days, leads)
        self._field(self.custom, "提醒时间", self.rule_clock)
        self.rule_hint = self._note(body, "请先选择事项。")

    def _build_brief(self, parent):
        theme, dp = self.theme, self.dp
        self.brief_columns = columns = tk.Frame(parent, bg=theme.panel_background)
        columns.pack(fill="x")
        self.brief_form = settings = tk.Frame(columns, bg=theme.panel_background)

        body = self._section(settings, "基础设置")
        self._check(body, "启用每日行程简报", self.brief["enabled"])
        self._field(body, "每日发送时间", self.brief["time"])

        body = self._section(settings, "简报内容")
        self.brief_options = options = tk.Frame(body, bg=body.cget("bg"))
        options.pack(fill="x")
        self.option_widgets = [self._check(options, title, self.brief[key], packed=False)
                               for key, title in (("timed", "当天定时日程"), ("untimed", "全天或无具体时间"),
                                                  ("unfinished", "当天未完成事项"), ("upcoming", "近期未完成 DDL"),
                                                  ("overdue", "已逾期 DDL"), ("send_empty", "无安排仍发送"))]
        self.brief_days = self._field(body, "DDL 展望天数", self.brief["days"],
                                      tuple(f"{day} 天" for day in range(1, 31)))

        body = self._section(settings, "发送通道")
        self._check(body, "Windows 通知", self.brief["windows"])
        self._check(body, "QQ 推送", self.brief["qq"])
        self._note(body, "此处只控制每日简报。错过发送时间后可在当天四小时内补发。")

        self.preview_card, preview = card(columns, self.app, surface=theme.input_background)
        self._label(preview, "每日行程简报 · 本地预览", color=theme.accent, size=9, bold=True).pack(fill="x")
        self.preview_heading = self._label(preview, size=12, bold=True)
        self.preview_heading.pack(fill="x", pady=(dp(6), dp(3)))
        self.preview_date = self._label(preview, muted=True)
        self.preview_date.pack(fill="x")
        self._divider(preview)
        self.preview_body = tk.Frame(preview, bg=preview.cget("bg"))
        self.preview_body.pack(fill="x")
        self._divider(preview)
        previews = tk.Frame(preview, bg=preview.cget("bg"))
        previews.pack(anchor="w")
        for title in ("查看本周", "查看 DDL"):
            label = tk.Label(previews, text=title, bg=theme.accent_soft, fg=theme.accent,
                             padx=dp(10), pady=dp(5), font=reminder_font(self.app, 8))
            label.pack(side="left", padx=(0, dp(6)))
        self._note(preview, "上方为简报中的按钮样式预览。")
        self._brief_layout = None
        columns.bind("<Configure>", self._layout_brief)
        for control in (*self.brief.values(), self.private):
            control.trace_add("write", self.refresh_preview)

    def _layout_brief(self, event):
        wide = event.width >= self.dp(655)
        two_columns = wide or event.width >= self.dp(490)
        if (wide, two_columns) == self._brief_layout:
            return
        self._brief_layout = wide, two_columns
        self.brief_columns.columnconfigure(0, weight=1)
        self.brief_columns.columnconfigure(1, weight=int(wide))
        self.brief_form.grid(row=0, column=0, sticky="new")
        self.preview_card.grid(row=0 if wide else 1, column=int(wide), sticky="new",
                               padx=(self.dp(12), 0) if wide else 0, pady=(0, self.dp(12)))
        if wide:
            self.brief_columns.columnconfigure((0, 1), uniform="brief")
        else:
            self.brief_columns.columnconfigure((0, 1), uniform="")
        span = 2 if two_columns else 1
        self.brief_options.columnconfigure(0, weight=1)
        self.brief_options.columnconfigure(1, weight=int(two_columns))
        for index, control in enumerate(self.option_widgets):
            control.grid(row=index // span, column=index % span, sticky="w", pady=self.dp(3))

    def _build_channels(self, parent):
        app, dp = self.app, self.dp
        self.windows_state = tk.StringVar(self)
        self.qq_account = tk.StringVar(self)
        self.qq_connection = tk.StringVar(self)
        self.qq_availability = tk.StringVar(self)
        self.scan_state = tk.StringVar(self)
        body = self._section(parent, "Windows 系统通知")
        self._check(body, "启用日程与 DDL 的 Windows 通知", self.windows)
        self._note(body, variable=self.windows_state)
        self.test_button = ReminderButton(body, app, "发送 Windows 测试通知", app.test_native_notification)
        self.test_button.configure(state="disabled" if app._notification_test_pending else "normal")
        self.test_button.pack(anchor="w", pady=(dp(8), 0))
        self._focus_scroll(self.test_button)
        self._note(body, variable=self.test_status)

        body = self._section(parent, "QQ 主动推送")
        self._check(body, "启用日程与 DDL 的 QQ 推送", self.qq)
        self._note(body, variable=self.qq_account)
        self._note(body, variable=self.qq_connection)
        self._note(body, variable=self.qq_availability)
        self.test_prefs = self._note(body)
        self.qq_test_button = ReminderButton(body, app, "发送 QQ 测试通知", app.test_qq_reminder)
        self.qq_test_button.pack(anchor="w", pady=(dp(8), 0))
        self._focus_scroll(self.qq_test_button)
        self._note(body, variable=self.qq_test_status)
        self.qq.trace_add("write", lambda *_: self.refresh_channel_status())

        body = self._section(parent, "隐私保护")
        self._check(body, "在通知中隐藏事项标题及详情", self.private)
        self._note(body, "同时适用于 Windows 和 QQ 的日程、DDL 与每日简报。本机日历和提醒计划仍显示事项详情。")

        body = self._section(parent, "后台运行")
        self._note(body, variable=self.scan_state)
        self._note(body, "关闭主窗口后" + ("在托盘中继续运行。" if app.lifecycle.close_hides else "退出客户端。"))
        self._note(body, "客户端运行时调度有效。唤醒后检查仍在有效期内的积压提醒。")

    def _status_tick(self):
        if self.tabs.index("current") == 2:
            self.refresh_channel_status()
        self.after(1000, self._status_tick)

    def refresh_channel_status(self):
        app = self.app
        status = app.notification_backend.status
        if status == "尚未启用":
            status = "Windows 通知尚未经本机检测"
        self.windows_state.set(status)
        binding = app.qq_reminder_backend.binding()
        worker = app.qq_reminder_backend.get_worker()
        runtime = getattr(app, "chat_runtime", None)
        configured = runtime.core.remote_tray_config.bound if runtime else bool(worker)
        if binding:
            self.qq_account.set(f"已核验账号  {binding.account}")
        else:
            self.qq_account.set("当前绑定尚未通过核验" if configured else "尚未绑定远程服务账号")
        if worker:
            connection = worker.snapshot().connection
            self.qq_connection.set(CONNECTION_LABELS.get(connection, ("远程服务连接状态未知",))[0])
        else:
            self.qq_connection.set("远程服务已关闭" if configured else "绑定账号后可检查连接")
        # Account-scoped capability observations come only from an explicit test.
        capability = getattr(app, "_qq_test_capability", None)
        if binding and capability and capability[0] == binding.scope:
            availability = capability[1]
        else:
            availability = "主动推送接口尚未验证"
        self.qq_availability.set(availability)
        test_scope = getattr(app, "_qq_test_scope", None)
        current_test = not test_scope or binding is not None and binding.scope == test_scope
        self.qq_test_status.set(app._qq_test_status if current_test else "当前绑定尚未测试")
        preference = "启用" if self.qq.get() else "关闭"
        self.test_prefs.configure(text=f"日程提醒  {preference}   ·   DDL 提醒  {preference}（共用上方开关）")
        self.qq_test_button.configure(state="normal" if binding and not app._qq_test_token else "disabled")
        last = getattr(app, "_reminder_scan_at", None)
        error = getattr(app, "_reminder_scan_error", "")
        scan = f"调度扫描失败  {error}" if error else ("调度器运行中" if last else "等待首次调度扫描")
        if last:
            scan += f" · 最近成功 {last:%m-%d %H:%M:%S}"
        self.scan_state.set(scan)

    def update_custom(self):
        self.mode_field.configure(state="readonly" if self.current_id else "disabled")
        if self.current_id and self.MODES[self.rule_mode.get()] == "custom":
            self.custom.pack(fill="x", before=self.rule_hint)
        else:
            self.custom.pack_forget()
        self.rule_hint.configure(text="规则只影响提醒时间。" if self.current_id else "请先选择事项。")

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
            self.rules[self.current_id] = {"mode": "custom", "time": clock, "days": self._days(self.rule_days.get())}

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
        self.rule_days.set(f"{rule.get('days', 0)} 天")
        self.update_custom()

    def _brief_values(self):
        raw = {key: control.get() for key, control in self.brief.items()}
        raw["days"] = self._days(raw["days"])
        return raw

    def save(self):
        try:
            self.store_rule()
            clock = normalize_reminder_time(self.clock.get())
            if clock is None:
                raise ReminderError("请将默认提醒时间填写为 HH:MM，例如 09:00。")
            brief = self._brief_values()
            brief["time"] = normalize_reminder_time(brief["time"])
            if brief["time"] is None:
                raise ReminderError("请将简报发送时间填写为 HH:MM，例如 08:00。")
            self.app.reminder_engine.configure({"enabled": self.enabled.get(), "private": self.private.get(),
                                                "time": clock, "general_days": self._days(self.general.get()),
                                                "ddl_days": self._days(self.ddl.get()), "events": self.rules,
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
        elif current == 2:
            self.refresh_channel_status()

    def refresh_preview(self, *_args):
        if self._closing or self.tabs.index("current") != 1:
            return
        raw = self._brief_values()
        self.brief_days.configure(state="readonly" if raw["upcoming"] else "disabled")
        for child in self.preview_body.winfo_children():
            child.destroy()
        today = self.clock_now()
        clock = normalize_reminder_time(raw["time"])
        demo = getattr(self.app, "_reminder_preview_clock", None) is not None
        source = "演示数据" if demo else "本机日程"
        mode = "已启用" if raw["enabled"] else "未启用"
        self.preview_date.configure(text=f"{today:%Y-%m-%d}  {clock or '—'}  ·  {source}  ·  {mode}")
        if clock is None:
            self.preview_heading.configure(text="请填写有效的发送时间")
            return
        hour, minute = map(int, clock.split(":"))
        when = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        items = brief_items(self.app.store, when, brief_settings(raw), self.private.get())
        greeting = "早上好" if hour < 11 else "下午好" if hour < 18 else "晚上好"
        self.preview_heading.configure(text=f"{greeting}，简报共 {len(items)} 项")
        if not items:
            self._note(self.preview_body, "暂无符合条件的事项。" if raw["send_empty"] else "无事项时跳过发送。")
            return
        if self.private.get():
            self._note(self.preview_body, "隐私保护已开启，仅显示事项总数。")
            return
        dp = self.dp
        for group, title in BRIEF_GROUP_LABELS.items():
            group_items = [item for item in items if item["group"] == group]
            if not group_items:
                continue
            color = {"upcoming": self.theme.event_type_urgent, "overdue": self.theme.danger}.get(group, self.theme.accent)
            self._label(self.preview_body, title, bold=True, color=color).pack(fill="x", pady=(dp(4), dp(2)))
            for item in group_items[:3]:
                row = tk.Frame(self.preview_body, bg=self.preview_body.cget("bg"))
                row.pack(fill="x", pady=(0, dp(2)))
                context = item["date"][5:] if group in ("upcoming", "overdue") else item["time"]
                if context:
                    context_label = self._label(row, context, muted=True)
                    context_label.configure(width=6)
                    context_label.pack(side="left", padx=(0, dp(6)))
                title = self._label(row, item["title"].replace("\n", " "))
                title.pack(side="left", fill="x", expand=True)
                title.bind("<Configure>", lambda event, label=title: label.configure(wraplength=event.width))
            if len(group_items) > 3:
                self._note(self.preview_body, f"另有 {len(group_items)-3} 项")

    def show_plans(self, batch=None):
        self.tabs.select(3)
        self.tab_changed()
        if batch:
            self.plan.show_notice(batch)

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.app.notification_settings_window = None
        self.app.notification_plan_window = None
        self.destroy()
