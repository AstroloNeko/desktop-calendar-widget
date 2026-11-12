"""Isolated real Windows hotkey/GUI smoke. No real data or notifications.

Posts WM_HOTKEY for reproducible route checks; --keyboard additionally injects
one registered chord through Windows input. Never overrides another app's chord.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from global_hotkey import DEFAULT_HOTKEY, save_shortcut
from global_hotkey_ui import HotkeyDialog
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from tools.check_system_tray import wait
from tray_icon import TrayIcon
from win_integration import is_window_topmost


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keyboard', action='store_true')
    args = parser.parse_args()
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user.keybd_event.argtypes = [w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t]
    cases, errors = [], []
    with tempfile.TemporaryDirectory(prefix='calendar-hotkey-qa-') as folder:
        store = Store(Path(folder) / 'data.json')
        with patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                wait(app, lambda: app.tray_icon and app.tray_icon.is_available, 'tray ready')
                tray = app.tray_icon
                assert not tray.hotkey_error, tray.hotkey_error
                def trigger():
                    binding = tray._hotkey_binding.active
                    assert binding
                    spec = binding[1]
                    assert user.PostMessageW(tray._hwnd, tray.WM_HOTKEY, binding[0], (spec.key << 16) | spec.modifiers)
                    pump(app, .35)
                # Verify actual OS ownership/conflict, without replacing any existing registration.
                competitor = TrayIcon('Isolated conflict QA', Path('missing.ico'), lambda _: None, hotkey=DEFAULT_HOTKEY)
                try:
                    assert competitor.start() and competitor.hotkey_error
                    assert competitor.is_available
                finally:
                    assert competitor.stop()
                for theme in ('modern', 'aero', 'paper', 'frutiger'):
                    app.set_theme(theme)
                    for pinned in (False, True):
                        app.return_to_compact_view()
                        app.window_mode = 'pinned' if pinned else 'desktop'
                        app.apply_window_mode()
                        pump(app, .15)
                        assert app.hide_to_tray()
                        trigger()
                        assert app.view_mode == 'global' and not app.lifecycle.hidden
                        assert is_window_topmost(app) == pinned
                        app.minimize_global()
                        pump(app, .1)
                        trigger()
                        assert app.state() != 'iconic'
                        geometry = app._capture_window_geometry()
                        trigger()
                        assert app._capture_window_geometry() == geometry
                        window = HotkeyDialog(app)
                        pump(app, .1)
                        assert app.grab_current() is window
                        assert window.winfo_viewable()
                        assert window.actions.winfo_rooty() + window.actions.winfo_height() <= window.winfo_rooty() + window.winfo_height()
                        trigger()
                        assert app.grab_current() is window  # Do not steal modal ownership.
                        assert window.winfo_viewable()
                        window.value.set('Alt+F23')
                        window.save()
                        pump(app, .1)
                        assert not window.winfo_exists() and app.grab_current() is None
                        assert tray._hotkey_binding.active[1].text == 'Alt+F23'
                        trigger()
                        save_shortcut(app, DEFAULT_HOTKEY)
                        app.return_to_compact_view()
                        app.open_editor()
                        pump(app, .1)
                        editor = app.editor_window
                        trigger()
                        assert app.editor_window is editor and editor.winfo_viewable()
                        assert app.grab_current() is editor and app.view_mode == 'compact'
                        editor.close()
                        pump(app, .1)
                        assert app.grab_current() is None
                        cases.append((theme, pinned))
                if args.keyboard:
                    # Route through the real Windows hotkey registration, not WM_HOTKEY injection.
                    app.return_to_compact_view()
                    assert app.hide_to_tray()
                    keys = (0x11, 0x12, 0x44)  # Ctrl, Alt, D.
                    try:
                        for key in keys:
                            user.keybd_event(key, 0, 0, 0)
                    finally:
                        for key in reversed(keys):
                            user.keybd_event(key, 0, 2, 0)
                    wait(app, lambda: not app.lifecycle.hidden and app.view_mode == 'global', 'real keyboard hotkey', timeout=5)
                save_shortcut(app, '')
                assert tray._hotkey_binding.active is None
                save_shortcut(app, DEFAULT_HOTKEY)
                assert not errors, errors
                result = dict(actual_dpi=dpi_for_window(app), theme_pinned_cases=len(cases),
                              real_registration_conflict=True, keyboard_injection=args.keyboard,
                              route_modal_geometry_disable='passed', errors=errors)
                print(json.dumps(result), flush=True)
            finally:
                modal = app._active_modal_window()
                if modal:
                    modal.close()
                cleanup(app)
            # Exit freed the chord: a second service can claim it without a process restart.
            probe = TrayIcon('Exit cleanup QA', Path('missing.ico'), lambda _: None, hotkey=DEFAULT_HOTKEY)
            try:
                assert probe.start() and not probe.hotkey_error
            finally:
                assert probe.stop()
            print('Hotkey exit cleanup: PASS', flush=True)


if __name__ == '__main__':
    main()
