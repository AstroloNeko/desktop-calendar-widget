"""Small themed modal for the shared Global workspace accelerator."""
import tkinter as tk

from global_hotkey import DEFAULT_HOTKEY, saved_shortcut, save_shortcut
from reminder_ui import reminder_button
from update_dialog import dialog_geometry


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
        self.title("全局唤起快捷键")
        self.configure(bg=theme.panel_background)
        width, height, x, y = dialog_geometry(app.dpi.work_area(), app.dpi.dpi,
            (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.value = tk.StringVar(value=saved_shortcut(app.store.settings))
        self.status = tk.StringVar(value=getattr(app.tray_icon, 'hotkey_error', None) or
                                  "输入组合键名称后保存；留空可关闭全局快捷键。")
        self.actions = tk.Frame(self, bg=theme.panel_background)
        self.actions.pack(side='bottom', fill='x', padx=dp(16), pady=dp(12))
        reminder_button(self.actions, app, "取消", self.close).pack(side='right')
        self.save_button = reminder_button(self.actions, app, "保存", self.save, primary=True)
        self.save_button.pack(side='right', padx=dp(6))
        reminder_button(self.actions, app, "恢复默认", lambda: self.value.set(DEFAULT_HOTKEY)).pack(side='left')
        content = tk.Frame(self, bg=theme.panel_background)
        content.pack(fill='both', expand=True, padx=dp(16), pady=dp(12))
        def label(text=None, **options):
            widget = tk.Label(content, text=text, bg=theme.panel_background, fg=theme.text_primary,
                              font=(font, 10), anchor='w', justify='left', wraplength=width-dp(40), **options)
            widget.pack(fill='x', pady=dp(7))
            return widget
        label("一键打开全局视图")
        label("默认 Ctrl+Alt+D；可自定义为 Ctrl+Shift+D、Alt+F8 等。\n支持字母、数字和 F1–F24，至少包含 Ctrl 或 Alt。")
        self.entry = tk.Entry(content, textvariable=self.value, font=(font, 11),
            bg=theme.input_background, fg=theme.text_primary, insertbackground=theme.text_primary,
            relief='flat', highlightthickness=dp(1), highlightbackground=theme.input_border,
            highlightcolor=theme.input_focus)
        self.entry.pack(fill='x', pady=dp(10), ipady=dp(5))
        label("程序需要在后台运行。唤起后直接进入全局视图；有未关闭的编辑弹窗时先显示弹窗，保留未保存内容。")
        label(textvariable=self.status)
        self.bind('<Escape>', lambda _e: self.close())
        self.protocol('WM_DELETE_WINDOW', self.close)
        app.present_modal(self, app)
        self.grab_set()
        self.entry.focus_set()

    def save(self):
        try:
            save_shortcut(self.app, self.value.get())
        except (OSError, ValueError) as error:
            self.status.set(f"快捷键设置未完成：{error}")
            self.entry.focus_set()
            return
        self.close()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()  # Shared overlay Destroy handler restores modal/window state.
