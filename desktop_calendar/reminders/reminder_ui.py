"""Tk coordination and lightweight settings, separate from reminder calculations."""
from collections import deque
from copy import deepcopy
from datetime import date, datetime, timedelta
import queue
import re
import time
from types import SimpleNamespace
from uuid import uuid4
import tkinter as tk
from tkinter import messagebox, ttk

from calendar_core import Event, normalize_reminder_time
from notification_backend import WindowsNotificationBackend
from desktop_calendar.reminders.reminder_engine import ReminderEngine, ReminderError, LEADS, parse_activation
from desktop_calendar.reminders.reminder_config import config_plan, field_lines
from desktop_calendar.reminders.reminder_rules import MAX_DAYS
from desktop_calendar.updates.update_dialog import dialog_geometry
from desktop_calendar.reminders.qq_reminders import QQReminderBackend
from desktop_calendar.reminders.notification_model import (
    BRIEF_CLOCKS, BRIEF_GROUP_LABELS, BRIEF_SLOTS, BRIEF_SWITCHES, BRIEF_TITLES, Notice,
    brief_content, brief_enabled, brief_heading, brief_item_context, brief_settings, brief_today_count,
    select_brief_items,
)
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
        self._brief_attempts = {}
        self._brief_results = {}
        self._brief_test_slot = "morning"
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
                    if not self._finish_brief_test("windows", token, "提交失败 · " + str(error)):
                        if token == self._notification_test_token:
                            self._set_test_status("测试未成功：" + str(error))
                elif kind == "test_delivery":
                    token, success = value
                    if self._finish_brief_test("windows", token, "已提交给 Windows" if success else "提交失败"):
                        continue
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
                        brief_test = self._brief_attempts.get("windows")
                        if brief_test and tag == "test" + brief_test[0][:12]:
                            self._finish_brief_test("windows", brief_test[0], "Windows 报告通知失败")
                            continue
                        if not tag.startswith("test"):
                            engine.delivery.acknowledge_tag(tag, datetime.now(), "Windows 报告通知失败")
            except (OSError, ValueError, RuntimeError) as error:
                engine.error = str(error)
        if self._notification_test_pending and time.monotonic() >= self._notification_test_deadline:
            self._notification_test_pending = False
            self._set_test_status("暂未收到 Windows 提交结果；请检查通知设置后重试，不能据此确认已显示。")
        self._poll_qq_reminders(engine)
        for channel, (token, deadline, _scope) in tuple(self._brief_attempts.items()):
            if time.monotonic() >= deadline:
                self._finish_brief_test(channel, token, "等待结果超时")
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
            try:
                engine.remember_qq_capability()
            except (OSError, ValueError, RuntimeError) as error:
                engine.error = str(error)
            if key.startswith("brief-test:"):
                token = key.removeprefix("brief-test:")
                pending = self._brief_attempts.get("qq")
                if pending and pending[0] == token and "HTTP 404" in reason:
                    self._qq_test_capability = pending[2], "主动推送接口未提供（HTTP 404）"
                self._finish_brief_test("qq", token, STATUS_LABELS[status] + (" · " + reason if reason else ""))
                continue
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

    def _finish_brief_test(self, channel, token, result):
        pending = self._brief_attempts.get(channel)
        if not pending or pending[0] != token:
            return False
        del self._brief_attempts[channel]
        self._brief_results[channel] = (pending[2], result)
        dialog = self.notification_settings_window
        if dialog and dialog.winfo_exists():
            dialog.refresh_brief_test()
        return True

    def _brief_qq_issue(self, binding):
        if not binding:
            return "尚未绑定或核验远程服务账号"
        capability = self._qq_test_capability
        return capability[1] if capability and capability[0] == binding.scope and "HTTP 404" in capability[1] else ""

    def send_brief_test(self, items, channels, private, *, slot="morning", progress=None, preview_day=None):
        if self.lifecycle.exiting or self._brief_attempts:
            return
        self._brief_results.clear()
        self._brief_test_slot = slot
        now = datetime.now()
        # Use no event IDs or actions in a manual test. Respect saved privacy
        # even when a draft temporarily turns privacy off.
        private = private or self.reminder_engine.config()["private"]
        items = tuple({} for _ in items) if private else items
        notice = Notice(uuid4().hex + uuid4().hex, "brief", now, now + timedelta(minutes=2), items,
                        manual_test=True, brief_slot=slot, progress=None if private else progress,
                        brief_date=preview_day)
        if "windows" in channels:
            try:
                token = self.notification_backend.send_test(
                    title=f"桌面月历 · {notice.title}（测试）", text=notice.windows_summary())
                self._brief_attempts["windows"] = token, time.monotonic() + 15, None
            except (OSError, ValueError, RuntimeError) as error:
                self._brief_results["windows"] = None, str(error)
        if "qq" in channels:
            binding = self.qq_reminder_backend.binding()
            scope = binding.scope if binding else None
            issue = self._brief_qq_issue(binding)
            if issue:
                self._brief_results["qq"] = scope, issue
            else:
                try:
                    self.qq_reminder_backend.send(notice, binding, private)
                    self._brief_attempts["qq"] = notice.key, time.monotonic() + 45, scope
                except (OSError, ValueError, RuntimeError) as error:
                    self._brief_results["qq"] = scope, str(error)

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
    MODES = {"使用默认即时提醒": "default", "不进行即时提醒": "off",
             "按默认时间明确提醒": "requested", "自定义日期提醒": "custom"}
    PRESERVE_RULE = "保留当前即时提醒规则"

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
        self.config_snapshot = app.reminder_engine.configuration_snapshot()
        config = self.config_snapshot["config"]
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

    def _section(self, parent, title, *, packed=True):
        border, body = card(parent, self.app)
        if packed:
            border.pack(fill="x", pady=(0, self.dp(12)))
        self._label(body, title, size=11, bold=True).pack(fill="x", pady=(0, self.dp(7)))
        return body

    def _paired_sections(self, parent, left_title, right_title):
        dp = self.dp
        pair = tk.Frame(parent, bg=self.theme.panel_background)
        pair.pack(fill="x", pady=(0, dp(12)))
        left = self._section(pair, left_title, packed=False)
        right = self._section(pair, right_title, packed=False)
        current = None

        def arrange(event):
            nonlocal current
            wide = event.width >= dp(620)
            if wide == current:
                return
            current = wide
            pair.columnconfigure(0, weight=1, uniform="pair" if wide else "")
            pair.columnconfigure(1, weight=int(wide), uniform="pair" if wide else "")
            left.master.grid(row=0, column=0, sticky="nsew")
            right.master.grid(row=0 if wide else 1, column=int(wide), sticky="nsew",
                              padx=(dp(12), 0) if wide else 0, pady=0 if wide else (dp(12), 0))

        pair.bind("<Configure>", arrange)
        return left, right

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

    def _field(self, parent, caption, variable, values=None, *, width=108, stretch=False, label_width=None):
        dp = self.dp
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=dp(4))
        label = self._label(row, caption)
        if label_width:
            label.configure(width=label_width)
        label.pack(side="left", padx=(0, dp(12)))
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
        body, aggregate = self._paired_sections(parent, "日程提醒", "通知聚合")
        self._check(body, "默认即时提醒", self.enabled)
        self.clock_field = self._field(body, "默认提醒时间", self.clock)
        self.general_field = self._field(body, "一般 / 紧急提前", self.general, leads)
        self.ddl_field = self._field(body, "DDL / 末日 DDL 提前", self.ddl, leads)
        self._note(body, "只控制使用默认规则的事项，明确设置的即时提醒可独立发送。")
        if not self.config_snapshot["config"]["legacy_explicit"] and self.rules:
            self._note(body, "历史规则保留原关闭状态。可重新选择并保存单事项规则以启用。")

        self._check(aggregate, "合并同一分钟触发的同类提醒", self.aggregate)
        self._check(aggregate, "恢复运行时合并有效的积压提醒", self.merge_backlog)
        self._divider(aggregate)
        example, content = card(aggregate, self.app, padding=8, surface=self.theme.input_background)
        example.pack(fill="x")
        self._label(content, "示例 · 3 项日程合并为 1 条通知",
                    size=9, bold=True, color=self.theme.accent).pack(fill="x")
        self._note(content, "项目组会、整理实验记录、提交阶段报告")

        body = self._section(parent, "单事项规则")
        selector = self._field(body, "选择事项", self.event_choice, tuple(self.event_labels), stretch=True, label_width=12)
        selector.bind("<<ComboboxSelected>>", self.select_event)
        self.mode_field = self._field(body, "提醒规则", self.rule_mode, tuple(self.MODES), stretch=True, label_width=12)
        def choose_rule(_event):
            self.rule_mode_changed = True
            self.update_custom()
        self.mode_field.bind("<<ComboboxSelected>>", choose_rule)
        self.rule_mode_changed = False
        self.custom = tk.Frame(body, bg=body.cget("bg"))
        self._field(self.custom, "提前天数", self.rule_days, tuple(f"{day} 天" for day in range(MAX_DAYS + 1)), label_width=12)
        self._field(self.custom, "提醒时间", self.rule_clock, label_width=12)
        self.rule_hint = self._note(body, "请先选择事项。")

    def _build_brief(self, parent):
        theme, dp = self.theme, self.dp
        slots = self._paired_sections(parent, BRIEF_TITLES["morning"], BRIEF_TITLES["evening"])
        for slot, body in zip(BRIEF_SLOTS, slots):
            self._check(body, "启用" + BRIEF_TITLES[slot], self.brief[BRIEF_SWITCHES[slot]])
            self._field(body, "发送时间", self.brief[BRIEF_CLOCKS[slot]])
            self._note(body, "查看今日待办。" if slot == "morning" else "汇总今日完成情况及剩余待办。")
        self.brief_columns = columns = tk.Frame(parent, bg=theme.panel_background)
        columns.pack(fill="x")
        self.brief_form = settings = tk.Frame(columns, bg=theme.panel_background)

        body = self._section(settings, "简报内容")
        self.brief_options = options = tk.Frame(body, bg=body.cget("bg"))
        options.pack(fill="x")
        self.option_widgets = [self._check(options, title, self.brief[key], packed=False)
                               for key, title in (("timed", "当天定时日程"), ("untimed", "全天或无具体时间"),
                                                  ("unfinished", "其余当天未完成事项"), ("upcoming", "近期未完成 DDL"),
                                                  ("overdue", "已逾期 DDL"), ("completion", "晚间完成统计"),
                                                  ("send_empty", "无事项时仍发送"))]
        self.brief_days = self._field(body, "DDL 展望天数", self.brief["days"],
                                      tuple(f"{day} 天" for day in range(1, 31)))

        body = self._section(settings, "发送通道")
        channels = tk.Frame(body, bg=body.cget("bg"))
        channels.pack(fill="x")
        for channel, title in (("windows", "Windows 通知"), ("qq", "QQ 推送")):
            self._check(channels, title, self.brief[channel], packed=False).pack(side="left", padx=(0, dp(8)), pady=dp(3))
        self._note(body, "最多补发四小时；晨间在进入晚间时段后停止补发，晚间不跨午夜。")

        self.preview_card, preview = card(columns, self.app, surface=theme.input_background)
        self.preview_slot = tk.StringVar(self, value="晨间预览")
        self._field(preview, "简报时段", self.preview_slot, ("晨间预览", "晚间预览"), width=110)
        self.preview_source = tk.StringVar(self, value="本机日程")
        self._field(preview, "简报预览", self.preview_source, ("本机日程", "示例数据"), width=110)
        self._divider(preview, pady=4)
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
        self.brief_test_button = ReminderButton(preview, self.app, "发送晨间测试简报", self.test_brief)
        self.brief_test_button.pack(anchor="w", pady=(dp(8), 0))
        self._focus_scroll(self.brief_test_button)
        self.brief_test_status = tk.StringVar(self)
        self._note(preview, variable=self.brief_test_status)
        self._brief_layout = None
        columns.bind("<Configure>", self._layout_brief)
        for control in (*self.brief.values(), self.private, self.preview_source, self.preview_slot):
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
        self.qq_status_line = tk.StringVar(self)
        self.scan_state = tk.StringVar(self)
        body, qq_body = self._paired_sections(parent, "Windows 系统通知", "QQ 主动推送")
        self._check(body, "启用日程与 DDL 的 Windows 通知", self.windows)
        self._divider(body)
        self._note(body, variable=self.windows_state)
        self._note(body, variable=self.test_status)
        self.test_button = ReminderButton(body, app, "发送 Windows 测试通知", app.test_native_notification)
        self.test_button.configure(state="disabled" if app._notification_test_pending else "normal")
        self.test_button.pack(side="bottom", anchor="w", pady=(dp(8), 0))
        self._focus_scroll(self.test_button)

        body = qq_body
        self._check(body, "启用日程与 DDL 的 QQ 推送", self.qq)
        self._divider(body)
        self._note(body, variable=self.qq_account)
        self.qq_status_labels = tuple(self._note(body, variable=value) for value in
                                      (self.qq_status_line, self.qq_test_status))
        self.qq_test_button = ReminderButton(body, app, "发送 QQ 测试通知", app.test_qq_reminder)
        self.qq_test_button.pack(side="bottom", anchor="w", pady=(dp(8), 0))
        self._focus_scroll(self.qq_test_button)
        self.qq.trace_add("write", lambda *_: self.refresh_channel_status())

        body = self._section(parent, "隐私保护")
        self._check(body, "在通知中隐藏事项标题及详情", self.private)
        self._note(body, "同时适用于 Windows 和 QQ 的日程、DDL 与每日简报。本机日历和提醒计划仍显示事项详情。")

        body = self._section(parent, "后台运行")
        self._note(body, variable=self.scan_state)
        policy = "关闭主窗口后在托盘中继续运行。" if app.lifecycle.close_hides else "关闭主窗口后退出客户端。"
        self._note(body, policy + "唤醒后检查仍在有效期内的积压提醒。")

    def _status_tick(self):
        current = self.tabs.index("current")
        if current == 2:
            self.refresh_channel_status()
        elif current == 1:
            self.refresh_brief_test()
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
            self.qq_account.set("当前绑定尚未通过核验" if configured else "尚未绑定远程服务，绑定后可使用 QQ 主动推送。")
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
        self.qq_status_line.set(f"{self.qq_connection.get()} · {availability}" if binding else self.qq_connection.get())
        test_scope = getattr(app, "_qq_test_scope", None)
        current_test = not test_scope or binding is not None and binding.scope == test_scope
        self.qq_test_status.set(app._qq_test_status if current_test else "当前绑定尚未测试")
        for index, label in enumerate(self.qq_status_labels):
            visible = (bool(worker) or configured) if index == 0 else bool(binding)
            if visible and not label.winfo_manager():
                label.pack(fill="x", pady=(self.dp(4), 0))
            elif not visible:
                label.pack_forget()
        self.qq_test_button.configure(state="normal" if binding and not app._qq_test_token else "disabled")
        last = getattr(app, "_reminder_scan_at", None)
        error = getattr(app, "_reminder_scan_error", "")
        scan = "上次调度扫描未成功" if error else ("调度器运行中" if last else "等待首次调度扫描")
        if last:
            scan += f" · 最近成功 {last:%m-%d %H:%M:%S}"
        self.scan_state.set(scan)

    def update_custom(self):
        self.mode_field.configure(state="readonly" if self.current_id else "disabled")
        if self.current_id and self.MODES.get(self.rule_mode.get()) == "custom":
            self.custom.pack(fill="x", before=self.rule_hint)
        else:
            self.custom.pack_forget()
        hint = "规则只影响提醒时间。" if self.current_id else "请先选择事项。"
        if self.rule_mode.get() == self.PRESERVE_RULE:
            rule = self.rules.get(self.current_id, {})
            if rule.get("mode") == "absolute":
                hint = "固定提醒时间：" + rule["at"].replace("T", " ")
            elif "minutes" in rule:
                hint = f"提前 {rule['minutes']} 分钟"
            elif "days" in rule:
                hint = f"提前 {rule['days']} 天 · {rule.get('time') or '默认提醒时间'}"
            else:
                hint = "按默认钟点及提前天数明确提醒。"
        elif self.rule_mode.get() == "不进行即时提醒":
            hint = "该事项仍可按内容偏好列入晨间或晚间简报。"
        self.rule_hint.configure(text=hint)

    def store_rule(self):
        if not self.current_id:
            return
        if self.rule_mode.get() == self.PRESERVE_RULE:
            return
        mode = self.MODES[self.rule_mode.get()]
        if mode == "default":
            if self.current_id in self.rules:
                self.rules[self.current_id] = {"mode": "default"}
        elif mode == "off":
            self.rules[self.current_id] = {"mode": "off"}
        elif mode == "requested":
            self.rules[self.current_id] = {"mode": "relative", "explicit": True}
        else:
            clock = normalize_reminder_time(self.rule_clock.get())
            if clock is None:
                raise ReminderError("请将单事项提醒时间填写为 HH:MM，例如 09:00。")
            new = {"mode": "custom", "time": clock, "days": self._days(self.rule_days.get())}
            prior = self.rules.get(self.current_id)
            if self.rule_mode_changed or prior not in (new, {**new, "explicit": True}):
                self.rules[self.current_id] = {**new, "explicit": True}

    def select_event(self, _event=None):
        try:
            self.store_rule()
        except ValueError as error:
            self.status.set(str(error))
            self.event_choice.set(next((label for label, identifier in self.event_labels.items()
                                        if identifier == self.current_id), ""))
            return
        self.current_id = self.event_labels.get(self.event_choice.get())
        self.rule_mode_changed = False
        rule = self.rules.get(self.current_id, {})
        choices = tuple(self.MODES)
        mode = rule.get("mode", "default")
        if mode in ("relative", "absolute"):
            choices += (self.PRESERVE_RULE,)
        self.mode_field.configure(values=choices)
        self.rule_mode.set(next((label for label, value in self.MODES.items() if value == mode), self.PRESERVE_RULE))
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
            for slot, field in BRIEF_CLOCKS.items():
                brief[field] = normalize_reminder_time(brief[field])
                if brief[field] is None:
                    raise ReminderError(f"请将{BRIEF_TITLES[slot]}时间填写为 HH:MM。")
            engine = self.app.reminder_engine
            plan = config_plan(self.config_snapshot, {
                "enabled": self.enabled.get(), "private": self.private.get(), "time":clock,
                "general_days":self._days(self.general.get()), "ddl_days":self._days(self.ddl.get()),
                "aggregate":self.aggregate.get(), "merge_backlog":self.merge_backlog.get(),
                "windows":self.windows.get(), "qq":self.qq.get(), "brief":brief,
            }, rules=self.rules)
            authorized = False
            if plan["remote_authorization"]:
                _, plan["binding_scope"] = engine.delivery.qq_status()
                authorized = messagebox.askyesno("授权通知设置",
                    "\n".join(field_lines(plan["fields"]))+"\n\n这些设置可能向已验证的远程服务发送私人日历内容。是否授权？",
                    parent=self)
                if not authorized:
                    return False
            engine.apply_configuration(plan, datetime.now(), authorized=authorized)
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

    def _preview_slot(self):
        return "evening" if self.preview_slot.get() == "晚间预览" else "morning"

    def brief_preview_data(self, raw):
        when, items, _progress = self._brief_preview_content(raw)
        return when, items

    def _brief_preview_content(self, raw):
        slot = self._preview_slot()
        clock = normalize_reminder_time(raw[BRIEF_CLOCKS[slot]])
        if clock is None:
            raise ValueError("请填写有效的发送时间")
        today = self.clock_now()
        samples = self.preview_source.get() == "示例数据"
        if samples:
            today = datetime(2031, 4, 16)
        hour, minute = map(int, clock.split(":"))
        when = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        events = None
        if samples:
            events = tuple(Event(id=f"brief-preview-{index}", title=title, due=due.isoformat(),
                                 has_time=timed, event_type=kind, done=slot == "evening" and index in (0, 1))
                           for index, (title, due, timed, kind) in enumerate((
                               ("项目组会", today.replace(hour=9), True, "general"),
                               ("准备项目材料", today.replace(hour=14), True, "general"),
                               ("整理实验记录", today, False, "general"),
                               ("整理实验记录备份", today.replace(hour=20), True, "general"),
                               ("提交申请", today.replace(hour=17), True, "ddl"),
                               ("提交阶段报告", today + timedelta(days=2), False, "ddl"),
                               ("交付数据说明", today - timedelta(days=2), False, "ddl"),
                           )))
        items, progress = brief_content(self.app.store, when, brief_settings(raw), slot=slot, events=events)
        return when, items, progress

    def test_brief(self):
        raw = self._brief_values()
        channels = [channel for channel in ("windows", "qq") if raw[channel]]
        try:
            when, items, progress = self._brief_preview_content(raw)
        except ValueError as error:
            self.brief_test_status.set(str(error))
            return
        if not channels:
            return
        # Selecting the button explicitly sends the selected preview to the chosen channels.
        self.app.send_brief_test(items, channels, self.private.get(), slot=self._preview_slot(),
                                 progress=progress, preview_day=when.date())
        self.refresh_brief_test()

    def refresh_brief_test(self):
        app = self.app
        pending = app._brief_attempts
        slot = self._preview_slot()
        results = app._brief_results if app._brief_test_slot == slot else {}
        qq_selected = self.brief["qq"].get()
        binding = app.qq_reminder_backend.binding() if qq_selected or "qq" in pending or "qq" in results else None
        issue = app._brief_qq_issue(binding) if qq_selected else ""
        messages = []
        for channel, name in (("windows", "Windows"), ("qq", "QQ")):
            if channel in pending:
                scope = pending[channel][2]
                result = "正在提交"
            elif channel in results:
                scope, result = results[channel]
            else:
                continue
            if scope and (not binding or binding.scope != scope):
                result = "当前账号尚未试发"
            messages.append(f"{name} · {result}")
        can_send = self.brief["windows"].get() or qq_selected and not issue
        if issue and "qq" not in pending and "qq" not in results:
            messages.append(f"QQ · {issue}")
        self.brief_test_status.set("\n".join(messages) if messages else
                                   ("试发当前预览到所选通道。" if can_send else "请选择试发通道。"))
        self.brief_test_button.configure(text="发送" + BRIEF_TITLES[slot][:2] + "测试简报",
                                         state="normal" if can_send and not pending else "disabled")

    def refresh_preview(self, *_args):
        if self._closing or self.tabs.index("current") != 1:
            return
        raw = self._brief_values()
        self.brief_days.configure(state="readonly" if raw["upcoming"] else "disabled")
        for child in self.preview_body.winfo_children():
            child.destroy()
        try:
            when, items, progress = self._brief_preview_content(raw)
        except ValueError as error:
            self.preview_heading.configure(text=str(error))
            self.refresh_brief_test()
            return
        self.refresh_brief_test()
        if self.preview_source.get() == "示例数据":
            source = "示例数据"
        else:
            source = "验收日历" if getattr(self.app, "_reminder_preview_clock", None) is not None else "本机日程"
        slot = self._preview_slot()
        mode = "已启用" if brief_enabled(raw, slot) else "未启用"
        self.preview_date.configure(text=f"{when:%Y-%m-%d %H:%M}  ·  {source}  ·  {mode}")
        self.preview_heading.configure(text=brief_heading(slot, when, brief_today_count(items)))
        if self.private.get() or self.app.reminder_engine.config()["private"]:
            self.preview_heading.configure(text=BRIEF_TITLES[slot])
            self._note(self.preview_body, f"隐私保护已开启，共 {len(items)} 项。")
            return
        if progress is not None:
            self._note(self.preview_body, progress.summary())
        if not items:
            if not progress or not progress.total:
                self._note(self.preview_body, "暂无符合条件的事项。" if raw["send_empty"] else "无事项时跳过发送。")
            return
        dp = self.dp
        shown, counts = select_brief_items(items)
        had_group = progress is not None
        for group, title in BRIEF_GROUP_LABELS.items():
            group_items = [item for item in shown if item["group"] == group]
            if not group_items:
                continue
            if had_group:
                self._divider(self.preview_body, pady=4)
            had_group = True
            is_ddl = group in ("today_ddl", "upcoming", "overdue")
            color = {"today_ddl": self.theme.event_type_urgent, "upcoming": self.theme.event_type_urgent,
                     "overdue": self.theme.danger}.get(group, self.theme.accent)
            heading = f"{title} · {counts[group]} 项" if is_ddl else title
            self._label(self.preview_body, heading, bold=True, color=color).pack(fill="x", pady=(dp(3), dp(2)))
            for item in group_items:
                row = tk.Frame(self.preview_body, bg=self.preview_body.cget("bg"))
                row.pack(fill="x", pady=(0, dp(2)))
                context = brief_item_context(item, when.date())
                if context:
                    context_label = self._label(row, context, muted=True)
                    context_label.configure(width=10 if is_ddl else 6)
                    context_label.pack(side="left", padx=(0, dp(6)))
                title = self._label(row, item["title"].replace("\n", " "))
                title.pack(side="left", fill="x", expand=True)
                title.bind("<Configure>", lambda event, label=title: label.configure(wraplength=event.width))
            if counts[group] > len(group_items):
                self._note(self.preview_body, f"另有 {counts[group] - len(group_items)} 项")

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
