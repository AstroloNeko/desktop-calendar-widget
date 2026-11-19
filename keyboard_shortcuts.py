"""Editable calendar commands and compatibility with saved global hotkeys."""
from global_hotkey import (GLOBAL_DEFAULTS, GLOBAL_LABELS, GLOBAL_SETTING_KEYS,
                           KEYS, MODIFIERS, parse_shortcut, saved_global_shortcuts)

LOCAL_SETTING = "keyboard_hotkeys"
LOCAL_COMMANDS = {
    "search": ("搜索事项 / 快捷命令", "Ctrl+K"),
    "primary": ("打开当天事项详情 / 新建事项", "Ctrl+N, N, Enter"),
    "today": ("回到今天", "Ctrl+T, Home, T"),
    "toggle_view": ("切换紧凑 / 全局视图", "Ctrl+G"),
    "undo": ("撤销日历操作", "Ctrl+Z"),
    "redo": ("重做日历操作", "Ctrl+Y, Ctrl+Shift+Z"),
    "previous_month": ("上个月", "PgUp"),
    "next_month": ("下个月", "PgDn"),
    "left_day": ("向前一天", "Left"),
    "right_day": ("向后一天", "Right"),
    "up_week": ("向前一周", "Up"),
    "down_week": ("向后一周", "Down"),
    "save_editor": ("保存事项 / 习惯 / 分类编辑", "Ctrl+Enter"),
    "save_event": ("保存事项编辑（备用键）", "Ctrl+S"),
}
SPECIAL_KEYS = {
    "ENTER": ("Enter", "Return"), "HOME": ("Home", "Home"),
    "PGUP": ("PgUp", "Prior"), "PGDN": ("PgDn", "Next"),
    **{key.upper(): (key, key) for key in ("Left", "Right", "Up", "Down")},
}
EDITORS = {"save_editor", "save_event"}
TEXT_WIDGETS = {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}


def parse_local(value, *, editor=False):
    """Return canonical labels and Tk sequences for one command."""
    if not isinstance(value, str):
        raise ValueError("快捷键格式无效。")
    result = {}
    for combo in value.split(",") if value.strip() else ():
        *mods, key = (part.strip().upper() for part in combo.split("+"))
        if key not in KEYS and key not in SPECIAL_KEYS:
            raise ValueError("请使用字母、数字、F1–F24、Enter、Home、PgUp/PgDn 或方向键。")
        canonical = [name for name in MODIFIERS if name.upper() in mods]
        if len(canonical) != len(mods):
            raise ValueError("修饰键重复或不支持；可使用 Ctrl、Alt、Shift。")
        label, keysym = SPECIAL_KEYS.get(key, (key, key.lower() if len(key) == 1 else key))
        text = "+".join([*canonical, label])
        if text in ("Alt+F4", "Ctrl+Alt+F4"):
            raise ValueError("请不要使用窗口关闭快捷键。")
        if editor and not {"Ctrl", "Alt"}.intersection(canonical):
            raise ValueError("编辑器的保存快捷键至少需要 Ctrl 或 Alt。")
        if editor and text in {"Ctrl+" + k for k in "ACVXYZ"} | {"Ctrl+Shift+Z"}:
            raise ValueError("请保留编辑器的标准文本操作快捷键。")
        if text in result:
            raise ValueError("同一行不能重复填写相同快捷键。")
        if len(key) == 1 and key.isalpha() and "Shift" in canonical:
            keysym = keysym.upper()
        result[text] = "<" + "-".join([*["Control" if m == "Ctrl" else m for m in canonical], "KeyPress", keysym]) + ">"
    return result


def saved_local_shortcuts(settings):
    stored = settings.get(LOCAL_SETTING, {})
    if not isinstance(stored, dict):
        stored = {}
    result = {}
    used = set()
    for name, (_, default) in LOCAL_COMMANDS.items():
        try:
            entries = parse_local(stored.get(name, default), editor=name in EDITORS)
        except ValueError:
            entries = parse_local(default, editor=name in EDITORS)
        result[name] = ", ".join(text for text in entries if text not in used)
        used.update(entries)
    return result


def validate_shortcuts(global_values, local_values):
    occupied = {}
    globals_out, locals_out = {}, {}
    def claim(text, label):
        if text in occupied:
            raise ValueError(f"{text} 同时用于「{occupied[text]}」和「{label}」。")
        occupied[text] = label

    for name, default in GLOBAL_DEFAULTS.items():
        shortcut = parse_shortcut(global_values.get(name, default))
        text = shortcut.text if shortcut else ""
        if text:
            claim(text, GLOBAL_LABELS[name])
        globals_out[name] = text
    for name, (label, default) in LOCAL_COMMANDS.items():
        entries = parse_local(local_values.get(name, default), editor=name in EDITORS)
        for text in entries:
            claim(text, label)
        locals_out[name] = ", ".join(entries)
    return globals_out, locals_out


def save_shortcuts(app, global_values, local_values):
    globals_out, locals_out = validate_shortcuts(global_values, local_values)
    settings = app.store.settings
    tray = app.tray_icon
    update_native = globals_out != saved_global_shortcuts(settings)
    if update_native and (not tray or not tray.is_available):
        raise ValueError("系统托盘服务不可用；未修改快捷键。")
    # Also retry a conflicted startup configuration if the tray is available.
    if tray and tray.is_available and getattr(tray, "hotkey_error", None):
        update_native = True
    if update_native:
        tray.configure_hotkey("prepare", globals_out)
    changes = {GLOBAL_SETTING_KEYS[name]: value for name, value in globals_out.items()}
    changes[LOCAL_SETTING] = locals_out
    before = {key: settings[key] for key in changes if key in settings}
    settings.update(changes)
    try:
        app.store.save()
    except BaseException:
        for key in changes.keys() - before.keys():
            settings.pop(key, None)
        settings.update(before)
        if update_native:
            try:
                tray.configure_hotkey("rollback")
            except (ValueError, OSError):
                pass
        raise
    try:
        if update_native:
            tray.configure_hotkey("commit")
    except (ValueError, OSError) as error:
        try:
            tray.configure_hotkey("rollback")
        except (ValueError, OSError):
            pass
        raise ValueError("设置已保存；系统全局快捷键需重启后生效。") from error
    finally:
        app._apply_calendar_shortcuts()


def bind_history_shortcuts(window, app):
    values = saved_local_shortcuts(app.store.settings)
    for name in ("undo", "redo"):
        for sequence in parse_local(values[name]).values():
            window.bind(sequence, lambda event, redo=name == "redo": app._history_shortcut(event, redo=redo))


def bind_editor_shortcuts(editor, app, *, event=False):
    values = saved_local_shortcuts(app.store.settings)
    def save(_event):
        editor.save()
        return "break"
    for name in ("save_editor", "save_event") if event else ("save_editor",):
        for sequence in parse_local(values[name], editor=True).values():
            editor.bind(sequence, save)
