"""Global-view accelerator parsing and thread-owned Win32 registration."""
from dataclasses import dataclass

DEFAULT_HOTKEY = "Ctrl+Alt+D"
SETTING_KEY = "global_view_hotkey"
MODIFIERS = {"Ctrl": 2, "Alt": 1, "Shift": 4}
KEYS = {**{chr(key): key for key in range(65, 91)},
        **{str(key): 48 + key for key in range(10)},
        **{f"F{key}": 111 + key for key in range(1, 25)}}


@dataclass(frozen=True)
class Shortcut:
    text: str
    modifiers: int
    key: int


def parse_shortcut(value):
    if not isinstance(value, str):
        raise ValueError("快捷键格式无效。")
    if not value.strip():
        return None  # Explicit opt-out.
    parts = [part.strip().upper() for part in value.split("+")]
    if len(parts) < 2 or parts[-1] not in KEYS:
        raise ValueError("请使用 Ctrl / Alt / Shift + 字母、数字或 F1–F24，例如 Ctrl+Alt+D。")
    names = {name.upper(): name for name in MODIFIERS}
    modifiers = parts[:-1]
    if len(set(modifiers)) != len(modifiers) or any(part not in names for part in modifiers):
        raise ValueError("修饰键重复或不支持；可使用 Ctrl、Alt、Shift。")
    if not ({"CTRL", "ALT"} & set(modifiers)):
        raise ValueError("全局快捷键至少需要 Ctrl 或 Alt，避免干扰日常输入。")
    canonical = [name for name in MODIFIERS if name.upper() in modifiers]
    text = "+".join(canonical + [parts[-1]])
    if text in ("Alt+F4", "Ctrl+Alt+F4"):
        raise ValueError("请不要使用窗口关闭快捷键。")
    return Shortcut(text, sum(MODIFIERS[name] for name in canonical), KEYS[parts[-1]])


def saved_shortcut(settings):
    try:
        shortcut = parse_shortcut(settings.get(SETTING_KEY, DEFAULT_HOTKEY))
    except ValueError:
        return DEFAULT_HOTKEY
    return shortcut.text if shortcut else ""


class HotkeyBinding:
    """Used only by the HWND's thread. Keep old registration until disk save succeeds."""
    IDS = (0x2C01, 0x2C02)

    def __init__(self, user, hwnd):
        self.user, self.hwnd = user, hwnd
        self.active = self.pending = None

    def prepare(self, value):
        self.rollback()
        shortcut = parse_shortcut(value)
        if self.active and shortcut == self.active[1]:
            self.pending = self.active
        elif shortcut is not None:
            identifier = next(key for key in self.IDS if not self.active or key != self.active[0])
            if not self.user.RegisterHotKey(self.hwnd, identifier, shortcut.modifiers | 0x4000, shortcut.key):
                raise ValueError("快捷键已被其他程序占用或 Windows 拒绝注册，请换一个组合。原快捷键保持不变。")
            self.pending = (identifier, shortcut)

    def commit(self):
        if self.active and self.active != self.pending:
            self.user.UnregisterHotKey(self.hwnd, self.active[0])
        self.active, self.pending = self.pending, None

    def rollback(self):
        if self.pending and self.pending != self.active:
            self.user.UnregisterHotKey(self.hwnd, self.pending[0])
        self.pending = None

    def matches(self, identifier, packed):
        return bool(self.active and identifier == self.active[0]
                    and (packed & 0xFFFF) == self.active[1].modifiers
                    and ((packed >> 16) & 0xFFFF) == self.active[1].key)

    def close(self):
        self.rollback()
        if self.active:
            self.user.UnregisterHotKey(self.hwnd, self.active[0])
            self.active = None


def save_shortcut(app, value):
    shortcut = parse_shortcut(value)
    value = shortcut.text if shortcut else ""
    tray = app.tray_icon
    if not tray or not tray.is_available:
        raise ValueError("系统托盘服务暂不可用，快捷键未修改。")
    # Reserve both bindings until storage has succeeded; no registration gap on rollback.
    tray.configure_hotkey("prepare", value)
    missing = SETTING_KEY not in app.store.settings
    previous = app.store.settings.get(SETTING_KEY)
    app.store.settings[SETTING_KEY] = value
    try:
        app.store.save()
    except Exception:
        if missing:
            app.store.settings.pop(SETTING_KEY, None)
        else:
            app.store.settings[SETTING_KEY] = previous
        tray.configure_hotkey("rollback")
        raise
    try:
        tray.configure_hotkey("commit")
    except (OSError, ValueError) as error:
        # The service can disappear after the atomic disk save (e.g. Shell teardown).
        # Keep the valid preference for restart, but do not claim it is active now.
        raise ValueError("快捷键设置已保存，但后台服务未完成应用。请重新启动月历后重试。") from error
    return value
