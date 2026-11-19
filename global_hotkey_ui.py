"""Unified shortcut settings for the desktop calendar."""
import tkinter as tk

from global_hotkey import GLOBAL_DEFAULTS, GLOBAL_LABELS, saved_global_shortcuts
from keyboard_shortcuts import LOCAL_COMMANDS, saved_local_shortcuts, save_shortcuts
from desktop_calendar.ui.themed_controls import SettingsScroll, form_button, form_entry
from desktop_calendar.updates.update_dialog import dialog_geometry


def open_hotkey_settings(app):
    blocker = app._lifecycle_blocker()
    if blocker is not None:
        app._focus_lifecycle_owner(blocker)
        return
    HotkeyDialog(app)


class HotkeyDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.withdraw()
        self.app, self._closing = app, False
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        self.title("快捷键设置")
        self.configure(bg=theme.panel_background)
        width, height, x, y = dialog_geometry(app.dpi.work_area(), app.dpi.dpi,
            (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.minsize(min(width, dp(320)), min(height, dp(260)))
        self.global_values = {name: tk.StringVar(self, value=value)
                              for name, value in saved_global_shortcuts(app.store.settings).items()}
        self.local_values = {name: tk.StringVar(self, value=value)
                             for name, value in saved_local_shortcuts(app.store.settings).items()}
        self.status = tk.StringVar(self, value=getattr(app.tray_icon, 'hotkey_error', None) or "")
        self.actions = tk.Frame(self, bg=theme.panel_background)
        self.actions.pack(side='bottom', fill='x', padx=dp(16), pady=dp(12))
        self.save_button, save = form_button(self.actions, app, "保存", self.save, primary=True)
        _, cancel = form_button(self.actions, app, "取消", self.close, quiet=True)
        _, reset = form_button(self.actions, app, "恢复默认", self.reset)
        save.pack(side='right')
        cancel.pack(side='right', padx=(0, dp(6)))
        reset.pack(side='left')
        tk.Label(self, textvariable=self.status, bg=theme.panel_background, fg=theme.text_primary,
                 font=(font, 9), justify='left', wraplength=max(dp(120), width-dp(36))).pack(
                     side='bottom', fill='x', padx=dp(16))

        tk.Frame(self, height=dp(1), bg=theme.divider).pack(side='bottom', fill='x')
        body = SettingsScroll(self, app)
        body.pack(fill='both', expand=True, padx=dp(16), pady=(dp(12), 0))
        content = body.content
        content.columnconfigure(1, weight=1)
        wrapped = []

        def label(text, row, *, heading=False, note=False):
            widget = tk.Label(content, text=text, bg=theme.panel_background,
                              fg=theme.text_secondary if note else theme.text_primary,
                              wraplength=max(dp(100), width-dp(75)) if note else dp(125),
                              font=(font, 8 if note else 10 if heading else 9, 'bold' if heading else 'normal'),
                              anchor='w', justify='left')
            widget.grid(row=row, column=0, columnspan=2 if heading or note else 1, sticky='ew',
                        pady=(dp(8) if heading else dp(3), dp(3)), padx=(0, dp(6)))
            if heading or note:
                wrapped.append(widget)

        first = None
        def field(text, value, row):
            nonlocal first
            label(text, row)
            entry, control = form_entry(content, app, value, width=16)
            control.grid(row=row, column=1, sticky='ew', pady=dp(3),
                         ipady=0 if theme.rounded_widget_shells else dp(4), padx=(0, dp(6)))
            entry.bind('<FocusIn>', lambda _e: body.reveal(entry), add='+')
            if first is None:
                first = entry

        label("系统全局 · 月历在后台运行时也有效", 0, heading=True)
        for row, (name, text) in enumerate(GLOBAL_LABELS.items(), 1):
            field(text, self.global_values[name], row)
        label("全局组合需含 Ctrl 或 Alt，末键为字母、数字或 F1–F24。留空可停用。", 3, note=True)
        label("月历内快捷键", 4, heading=True)
        label("支持 Enter、Home、PgUp/PgDn 和方向键。多个组合用英文逗号隔开；留空可停用。", 5, note=True)
        for row, (name, (text, _default)) in enumerate(LOCAL_COMMANDS.items(), 6):
            field(text, self.local_values[name], row)
        def wrap(event):
            for widget in wrapped:
                widget.configure(wraplength=max(dp(100), event.width-dp(12)))
        body.canvas.bind('<Configure>', wrap, add='+')
        self.bind('<Escape>', lambda _e: self.close())
        self.protocol('WM_DELETE_WINDOW', self.close)
        app.present_modal(self, app)
        self.grab_set()
        first.focus_set()

    def reset(self):
        for name, value in GLOBAL_DEFAULTS.items():
            self.global_values[name].set(value)
        for name, (_, value) in LOCAL_COMMANDS.items():
            self.local_values[name].set(value)

    def save(self):
        try:
            save_shortcuts(self.app, {name: value.get() for name, value in self.global_values.items()},
                           {name: value.get() for name, value in self.local_values.items()})
        except (OSError, ValueError) as error:
            self.status.set(f"快捷键设置未完成：{error}")
            return
        self.close()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()  # Shared overlay Destroy handler restores modal/window state.
