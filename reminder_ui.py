"""Tk coordination and lightweight settings, separate from reminder calculations."""
from collections import deque
from copy import deepcopy
from datetime import datetime
import queue
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

from calendar_core import normalize_reminder_time
from notification_backend import WindowsNotificationBackend
from reminder_engine import ReminderEngine, ReminderError, LEADS, parse_activation
from update_dialog import dialog_geometry
from ui_draw import blend


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
        self._notification_root = resource_root
        self.notification_backend = WindowsNotificationBackend(resource_root, self.store.data_file)
        self.reminder_engine = ReminderEngine(self.store, self.notification_backend)
        self.notification_settings_window = None
        self.notification_plan_window = None
        self._reminder_views_dirty = True
        self._notification_test_token = None
        self._notification_test_pending = False
        self._notification_test_clicked = False
        self._notification_test_until = 0
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
            delay = engine.tick(datetime.now())
            if (not engine.config()["enabled"] and self.notification_backend._thread is not None
                    and time.monotonic() >= self._notification_test_until):
                self.notification_backend.close()
                self.notification_backend = WindowsNotificationBackend(self._notification_root, self.store.data_file)
                engine.backend = self.notification_backend
            return delay
        except (OSError, ValueError, RuntimeError) as error:
            engine.error = str(error)
            return 15000

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
                        for key in engine._state()["deliveries"]:
                            if key.startswith(tag) and len(tag) == 16:
                                engine.acknowledge(key, False, datetime.now())
            except (OSError, ValueError, RuntimeError) as error:
                engine.error = str(error)
        if self._notification_test_pending and time.monotonic() >= self._notification_test_deadline:
            self._notification_test_pending = False
            self._set_test_status("暂未收到 Windows 提交结果；请检查通知设置后重试，不能据此确认已显示。")
        self._refresh_reminder_views()
        if not self._pending_notification_actions or self._lifecycle_blocker() is not None:
            return
        action = self._pending_notification_actions.popleft()
        try:
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

    def open_reminder_settings(self):
        if self._active_modal_window() is not None or self.update_busy:
            return
        try:
            self.reminder_engine.config()
            self.reminder_engine._state()
        except ValueError as error:
            self._show_feedback(str(error))
            return
        self.notification_settings_window = ReminderSettingsDialog(self)

    def open_reminder_plans(self):
        if self._active_modal_window() is not None or self.update_busy:
            return
        try:
            self.reminder_engine.overview(datetime.now())
        except (OSError, ValueError) as error:
            self._show_feedback(str(error))
            return
        from reminder_center import ReminderPlanDialog
        self.notification_plan_window = ReminderPlanDialog(self)

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
            self._notification_test_until = time.monotonic() + 60
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
        attention = sum(row.group == "attention" for row in app.reminder_engine.overview(datetime.now()).rows)
        initial_status = app.reminder_engine.error or (
            f"有 {attention} 项当前提醒需要处理；可保存并查看提醒计划，了解失败、错过或稍后提醒状态。"
            if attention else app.notification_backend.status)
        self.status = tk.StringVar(value=initial_status)
        self.test_status = tk.StringVar(value=app._notification_test_status)
        config = app.reminder_engine.config()
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
        button(actions, "关闭", self.close).pack(side="right")
        self.save_button = button(actions, "保存设置", self.save, primary=True)
        self.save_button.pack(side="right", padx=dp(6))
        button(actions, "保存并查看计划", self.show_plans).pack(side="left")
        tk.Label(self, textvariable=self.status, bg=theme.panel_background, fg=theme.text_secondary,
                 wraplength=width - dp(30), anchor="w", justify="left", font=(font, 8)).pack(side="bottom", fill="x", padx=dp(14))
        tk.Frame(self, bg=theme.divider, height=dp(1)).pack(side="bottom", fill="x")
        header = tk.Frame(self, bg=theme.panel_background)
        header.pack(fill="x", padx=dp(16), pady=(dp(14), dp(10)))
        tk.Label(header, text="后台与提醒", bg=theme.panel_background, fg=theme.text_primary,
                 font=(font, 14, "bold"), anchor="w").pack(fill="x")
        tk.Label(header, text="后台运行 · 日程通知 · 隐私与规则", bg=theme.panel_background,
                 fg=theme.text_secondary, font=(font, 9), anchor="w").pack(fill="x", pady=(dp(3), 0))
        frame = tk.Frame(self, bg=theme.panel_background)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, bg=theme.panel_background, highlightthickness=0)
        self.scroll_canvas = canvas
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview, style="Global.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=theme.panel_background, padx=dp(16), pady=dp(2))
        self.content = content
        window = canvas.create_window(0, 0, anchor="nw", window=content)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        self.bind("<MouseWheel>", wheel)
        self.sections = []
        wrapped = []
        field_labels = []
        def section(title, subtitle):
            block = tk.Frame(content, bg=theme.workspace_detail, padx=dp(10), pady=dp(8),
                             highlightthickness=dp(1), highlightbackground=theme.divider)
            block.pack(fill="x", pady=(0, dp(10)))
            self.sections.append(block)
            tk.Label(block, text=title, anchor="w", bg=theme.workspace_detail,
                     fg=theme.text_primary, font=(font, 10, "bold")).pack(fill="x")
            hint = tk.Label(block, text=subtitle, anchor="w", justify="left", bg=theme.workspace_detail,
                            fg=theme.text_secondary, font=(font, 8), wraplength=width-dp(76))
            hint.pack(fill="x", pady=(dp(2), dp(6)))
            wrapped.append(hint)
            return block
        body = section("后台运行", "隐藏到托盘不等于退出；彻底退出后不再提醒。")
        def label(text):
            widget = tk.Label(body, text=text, anchor="w", justify="left", wraplength=width - dp(76),
                              bg=theme.workspace_detail, fg=theme.text_secondary, font=(font, 8))
            widget.pack(fill="x", pady=dp(3))
            wrapped.append(widget)
            return widget
        def check(text, variable):
            widget = tk.Checkbutton(body, text=text, variable=variable, bg=theme.workspace_detail,
                           fg=theme.text_primary, activebackground=theme.workspace_detail,
                           activeforeground=theme.accent, selectcolor=theme.input_background,
                           font=(font, 9), cursor="hand2", anchor="w", wraplength=width-dp(90))
            widget.pack(fill="x", pady=dp(4))
            wrapped.append(widget)
        def entry(parent, variable):
            return tk.Entry(parent, textvariable=variable, bg=theme.input_background, fg=theme.text_primary,
                            insertbackground=theme.text_primary, font=(font, 9), relief="flat", bd=0,
                            highlightthickness=dp(1), highlightbackground=theme.input_border,
                            highlightcolor=theme.input_focus)
        def field(text, variable, values=None, *, wide=False):
            row = tk.Frame(body, bg=theme.workspace_detail)
            row.pack(fill="x", pady=dp(4))
            caption = tk.Label(row, text=text, anchor="w", justify="left", bg=theme.workspace_detail,
                               fg=theme.text_primary, font=(font, 9), wraplength=dp(230))
            caption.pack(side="top" if wide else "left", anchor="w", pady=(0, dp(3)) if wide else 0)
            if not wide:
                field_labels.append(caption)
            if values is not None:
                widget = ttk.Combobox(row, textvariable=variable, values=values, state="readonly", font=(font, 9), width=13)
            else:
                widget = entry(row, variable)
                widget.configure(width=15)
            widget.pack(side="top" if wide else "right", fill="x" if wide else "none")
            return widget
        label("关闭窗口 → " + ("隐藏到系统托盘" if app.lifecycle.close_hides else "彻底退出")
              + "\n双击托盘图标恢复；右键托盘菜单可彻底退出。")
        label("全局视图最小化 → 任务栏（保持现有行为）")
        body = section("日程提醒", "先开启日程提醒，再设置默认时间；习惯提醒在习惯编辑器单独管理。")
        check("启用日程提醒", self.enabled)
        field("默认提醒时间", self.clock)
        label("HH:MM · 用于无具体时间事项，不是截止时间。")
        field("一般 / 紧急 · 提前天数", self.general, tuple(map(str, LEADS)))
        field("DDL / 末日 DDL · 提前天数", self.ddl, tuple(map(str, LEADS)))
        check("隐私保护：通知中隐藏事项标题", self.private)
        label("有具体时间的事项沿用编辑器的提醒提前量；选择“不提醒”时保持不提醒。")
        body = section("测试通知", "只测试 Windows 通知通道，不包含日历内容，也不会开启真实提醒。")
        self.test_button = button(body, "发送测试通知", app.test_native_notification)
        self.test_button.configure(state="disabled" if app._notification_test_pending else "normal")
        self.test_button.pack(anchor="w", pady=dp(5))
        self.test_status_label = tk.Label(body, textvariable=self.test_status, anchor="w", justify="left",
                 wraplength=width - dp(76), bg=blend(theme.accent_soft, theme.workspace_detail, .4),
                 fg=theme.text_primary, font=(font, 9), padx=dp(8), pady=dp(7))
        self.test_status_label.pack(fill="x", pady=(dp(4), dp(2)))
        wrapped.append(self.test_status_label)
        label("提交 Windows ≠ 已显示或已阅读。未看到时，请检查通知权限、专注/勿扰与 Win+N。")
        body = section("单事项规则", "可覆盖默认提醒，不改变事项日期或 DDL；未选择时不写入规则。")
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
        entry(self.custom, self.rule_clock).grid(row=1, column=1, sticky="ew")
        self.cancel_button = button(body, "取消该事项的稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(anchor="w", pady=dp(10))
        def rewrap(event):
            if event.widget is self:
                # Labels inside the scrolling area must follow the viewport,
                # not their initial size (small work areas / manual resizing).
                for widget in wrapped:
                    widget.configure(wraplength=max(dp(120), event.width - dp(76)))
                for widget in field_labels:
                    widget.configure(wraplength=max(dp(90), event.width-dp(240)))
                for widget in self.winfo_children():
                    if isinstance(widget, tk.Label):
                        widget.configure(wraplength=max(dp(120), event.width - dp(30)))
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
            self.app.reminder_engine.configure({"enabled": self.enabled.get(), "private": self.private.get(),
                                                "time": clock, "general_days": int(self.general.get()),
                                                "ddl_days": int(self.ddl.get()), "events": self.rules})
            self.app.invalidate_reminders()
            self.close()
            return True
        except (OSError, ValueError) as error:
            self.status.set(f"设置未保存：{error}")
            return False

    def show_plans(self):
        if self.save():
            self.app.open_reminder_plans()

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
        self.destroy()
