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


class NativeReminderMixin:
    def _init_native_reminders(self, resource_root):
        self._notification_root = resource_root
        self.notification_backend = WindowsNotificationBackend(resource_root, self.store.data_file)
        self.reminder_engine = ReminderEngine(self.store, self.notification_backend)
        self.notification_settings_window = None
        self._reminder_refresh_job = None
        self._pending_notification_actions = deque(maxlen=16)
        self._notification_activation_times = {}

    def invalidate_reminders(self):
        engine = getattr(self, "reminder_engine", None)
        if engine is None or self.lifecycle.exiting:
            return
        engine.invalidate()
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
            if not engine.config()["enabled"] and self.notification_backend._thread is not None:
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
                elif kind in ("status", "error"):
                    engine.error = value if kind == "error" else ""
                    dialog = self.notification_settings_window
                    if dialog and dialog.winfo_exists():
                        dialog.status.set(value)
                elif kind == "native":
                    event_kind, arguments = value
                    if event_kind == 1:
                        action = parse_activation(arguments)
                        now = time.monotonic()
                        previous = self._notification_activation_times.get(arguments, -10)
                        if action and now - previous > 2:
                            self._notification_activation_times = {k: v for k, v in self._notification_activation_times.items() if now - v < 5}
                            self._notification_activation_times[arguments] = now
                            self._pending_notification_actions.append(action)
                    elif event_kind == 2:
                        tag = arguments.split(":", 1)[0]
                        for key in engine._state()["deliveries"]:
                            if key.startswith(tag) and len(tag) == 16:
                                engine.acknowledge(key, False, datetime.now())
            except (OSError, ValueError, RuntimeError) as error:
                engine.error = str(error)
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
        self.title("日程提醒设置")
        self.configure(bg=theme.panel_background)
        area = app.dpi.work_area()
        width, height, x, y = dialog_geometry(area, app.dpi.dpi,
                                             (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, dp(320)), min(height, dp(260)))
        failures = sum(isinstance(entry, dict) and entry.get("status") in ("failed", "exhausted")
                       for entry in app.reminder_engine._state()["deliveries"].values())
        initial_status = app.reminder_engine.error or (
            f"有 {failures} 条提醒派发失败或已停止重试；请检查系统通知权限，调整提醒时间/日期可生成新计划。"
            if failures else app.notification_backend.status)
        self.status = tk.StringVar(value=initial_status)
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
        def button(parent, text, command):
            return tk.Button(parent, text=text, command=command, bg=theme.control_background,
                             fg=theme.control_text, activebackground=theme.control_hover,
                             activeforeground=theme.control_text, relief="flat", font=(font, 9), padx=dp(10), pady=dp(5))
        button(actions, "关闭", self.close).pack(side="right")
        button(actions, "保存设置", self.save).pack(side="right", padx=dp(6))
        tk.Label(self, textvariable=self.status, bg=theme.panel_background, fg=theme.text_secondary,
                 wraplength=width - dp(30), anchor="w", font=(font, 8)).pack(side="bottom", fill="x", padx=dp(14))
        frame = tk.Frame(self, bg=theme.panel_background)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, bg=theme.panel_background, highlightthickness=0)
        self.scroll_canvas = canvas
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=theme.panel_background, padx=dp(16), pady=dp(12))
        window = canvas.create_window(0, 0, anchor="nw", window=content)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        self.bind("<MouseWheel>", wheel)
        def label(text):
            tk.Label(content, text=text, anchor="w", justify="left", wraplength=width - dp(50),
                     bg=theme.panel_background, fg=theme.text_primary, font=(font, 9)).pack(fill="x", pady=dp(4))
        def check(text, variable):
            tk.Checkbutton(content, text=text, variable=variable, bg=theme.panel_background,
                           fg=theme.text_primary, activebackground=theme.panel_background,
                           selectcolor=theme.input_background, font=(font, 9)).pack(anchor="w", pady=dp(4))
        def field(text, variable, values=None):
            label(text)
            if values is not None:
                widget = ttk.Combobox(content, textvariable=variable, values=values, state="readonly", font=(font, 9))
            else:
                widget = tk.Entry(content, textvariable=variable, bg=theme.input_background, fg=theme.text_primary,
                                  insertbackground=theme.text_primary, font=(font, 9))
            widget.pack(fill="x", pady=(0, dp(5)))
            return widget
        check("启用日程提醒（程序运行或隐藏到托盘时有效）", self.enabled)
        field("无具体时间事项的提醒时间（不是截止时间，HH:MM）", self.clock)
        field("一般 / 紧急事项：提前天数", self.general, tuple(map(str, LEADS)))
        field("DDL / 多日末日 DDL：提前天数", self.ddl, tuple(map(str, LEADS)))
        check("隐私保护：通知中隐藏事项标题", self.private)
        label("有具体时间的事项，默认沿用编辑器原有的提醒提前量；选择“不提醒”时保持不提醒。\n单事项规则可在下面覆盖。习惯提醒保持原有设置。")
        selector = field("单事项规则（选择后可覆盖默认设置）", self.event_choice, tuple(self.event_labels))
        selector.bind("<<ComboboxSelected>>", self.select_event)
        mode = field("提醒规则", self.rule_mode, tuple(self.MODES))
        mode.bind("<<ComboboxSelected>>", lambda _e: self.update_custom())
        self.custom = tk.Frame(content, bg=theme.panel_background)
        self.custom.pack(fill="x")
        tk.Label(self.custom, text="提前天数 / 本地提醒时间", bg=theme.panel_background, fg=theme.text_primary,
                 font=(font, 9)).pack(anchor="w")
        ttk.Combobox(self.custom, textvariable=self.rule_days, values=tuple(map(str, LEADS)),
                     state="readonly", font=(font, 9)).pack(side="left", fill="x", expand=True)
        tk.Entry(self.custom, textvariable=self.rule_clock, bg=theme.input_background, fg=theme.text_primary,
                 insertbackground=theme.text_primary, font=(font, 9)).pack(side="left", fill="x", expand=True)
        self.cancel_button = button(content, "取消该事项的稍后提醒", self.cancel_snooze)
        self.cancel_button.pack(anchor="w", pady=dp(10))
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
        except (OSError, ValueError) as error:
            self.status.set(f"设置未保存：{error}")

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
