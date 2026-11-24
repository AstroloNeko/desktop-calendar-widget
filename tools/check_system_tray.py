"""Opt-in Windows 11 Shell/Tk tray smoke with isolated data.

Registers real Shell icons and sends native callback messages to the service's
real HWND. This is NOT a claim of physical mouse clicks or an Explorer restart.
No network, real calendar data, installation or desktop-process termination.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from tray_icon import GUID, TrayIcon
from win_integration import is_window_topmost, window_handle


class IconIdentifier(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("guidItem", GUID)]


S, U = ctypes.WinDLL("shell32"), ctypes.WinDLL("user32")
S.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(IconIdentifier), ctypes.POINTER(w.RECT)]
S.Shell_NotifyIconGetRect.restype = ctypes.c_long
U.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
U.GetWindowLongPtrW.restype = ctypes.c_ssize_t
U.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]


def registered(hwnd):
    identifier = IconIdentifier()
    identifier.cbSize, identifier.hWnd, identifier.uID = ctypes.sizeof(identifier), hwnd, 1
    rectangle = w.RECT()
    return S.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rectangle)) == 0


def wait(app, predicate, label, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(app, .03)
        if predicate():
            return
    raise AssertionError(label)


def send(tray, action):
    identifier = next(identifier for identifier, value in TrayIcon.COMMANDS.items() if value == action)
    assert U.PostMessageW(tray._hwnd, TrayIcon.WM_COMMAND, identifier, 0)


def source_checks(sqlite=False):
    errors = []
    with tempfile.TemporaryDirectory(prefix="calendar-tray-qa-") as temporary:
        store = Store(Path(temporary)/"calendar_data.json")
        with patch.object(CalendarApp, "check_reminders"):
            if sqlite:
                from desktop_storage_startup import StartupOptions, create_desktop_app
                store.save()
                app = create_desktop_app(StartupOptions(Path(temporary), True), CalendarApp, confirm=lambda: True)
                store = app.store
            else:
                app = CalendarApp(store)
            snapshot = store.repository.revision if sqlite else store.data_file.read_bytes
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                wait(app, lambda: app.tray_icon and app.tray_icon.is_available, "tray registration")
                tray = app.tray_icon
                assert registered(tray._hwnd), "Shell does not know the icon"
                print("Actual Shell icon registered; native DPI", dpi_for_window(app), flush=True)
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for view in ("compact", "global"):
                        (app.enter_global_view if view == "global" else app.return_to_compact_view)()
                        for pinned in (False, True):
                            app.window_mode = "pinned" if pinned else "desktop"
                            app.desktop_session_active = True
                            app.apply_window_mode()
                            pump(app, .3)
                            before = snapshot()
                            geometry = app._capture_window_geometry()
                            history = app.event_commands
                            send(tray, "hide")
                            wait(app, lambda: app.lifecycle.hidden, "native menu hide")
                            assert app.state() == "withdrawn" and registered(tray._hwnd)
                            # A real callback message, delivered on the tray thread.
                            U.PostMessageW(tray._hwnd, TrayIcon.CALLBACK_MESSAGE, 0,
                                           (1 << 16) | TrayIcon.WM_LBUTTONDBLCLK)
                            wait(app, lambda: not app.lifecycle.hidden, "double-click restore")
                            pump(app, .3)
                            assert app.view_mode == view and app.event_commands is history
                            assert app._capture_window_geometry() == geometry, (geometry, app._capture_window_geometry())
                            assert snapshot() == before, "hide/restore wrote user data"
                            assert is_window_topmost(app) == pinned
                            hwnd = window_handle(app)
                            assert not U.GetWindowLongPtrW(hwnd, -16) & 0x00C00000, "native caption returned"
                            assert not app.overrideredirect() if view == "global" else app.overrideredirect()
                            app.open_editor()
                            pump(app)
                            modal = app.editor_window
                            assert app.grab_current() is modal
                            send(tray, "hide")
                            pump(app, .35)
                            assert not app.lifecycle.hidden and app.grab_current() is modal
                            send(tray, "exit")
                            pump(app, .35)
                            assert not app.lifecycle.stopped and app.grab_current() is modal
                            modal.close()
                            pump(app, .15)
                    app.open_category_editor()
                    pump(app)
                    category = app.category_editor
                    send(tray, "hide")
                    send(tray, "exit")
                    pump(app, .35)
                    assert app.grab_current() is category and not app.lifecycle.hidden and not app.lifecycle.stopped
                    category.close()
                    pump(app, .15)
                    print(theme, "Compact/Global, pinned on/off, geometry, modal hide/exit guards: PASS", flush=True)
                app.return_to_compact_view()
                pump(app, .3)
                send(tray, "hide")
                wait(app, lambda: app.lifecycle.hidden, "hide compact before global")
                send(tray, "global")
                wait(app, lambda: app.view_mode == "global" and not app.lifecycle.hidden, "explicit Global")
                pump(app, .3)
                app.minimize_global()
                pump(app, .2)
                assert app.state() == "iconic" and not app.lifecycle.hidden
                send(tray, "show")
                wait(app, lambda: app.state() == "normal", "taskbar minimize restore")
                pump(app, .3)
                baseline = app._capture_window_geometry()
                for _ in range(20):
                    send(tray, "hide")
                    wait(app, lambda: app.lifecycle.hidden, "cycle hide")
                    send(tray, "show")
                    wait(app, lambda: not app.lifecycle.hidden, "cycle show")
                pump(app, .3)
                assert app._capture_window_geometry() == baseline
                assert len([t for t in threading.enumerate() if t.name == "calendar-tray"]) == 1
                # Simulate only the loss/broadcast protocol, not restarting the
                # user's Explorer. Real re-registration is checked with Shell.
                send(tray, "hide")
                wait(app, lambda: app.lifecycle.hidden, "hide before Explorer protocol")
                tray._api.shell.Shell_NotifyIconW(tray.NIM_DELETE, ctypes.byref(tray._nid))
                assert not registered(tray._hwnd)
                U.PostMessageW(tray._hwnd, tray._taskbar_created, 0, 0)
                wait(app, lambda: registered(tray._hwnd), "TaskbarCreated re-add")
                send(tray, "show")
                wait(app, lambda: not app.lifecycle.hidden, "restore after re-add")
                # Explorer can also vanish without a broadcast. Keep the real
                # helper alive but remove only this QA icon; the slow native
                # health probe must reveal the main window without a callback.
                send(tray, "hide")
                wait(app, lambda: app.lifecycle.hidden, "hide before silent icon loss")
                tray._api.shell.Shell_NotifyIconW(tray.NIM_DELETE, ctypes.byref(tray._nid))
                wait(app, lambda: not app.lifecycle.hidden, "silent Shell icon loss recovery")
                U.PostMessageW(tray._hwnd, tray._taskbar_created, 0, 0)
                wait(app, lambda: registered(tray._hwnd) and tray.is_available, "icon re-add after silent loss")
                # A crashed tray thread is detected even without its callback.
                send(tray, "hide")
                wait(app, lambda: app.lifecycle.hidden, "hide before tray loss")
                old_hwnd = tray._hwnd
                tray.stop()
                wait(app, lambda: not app.lifecycle.hidden, "failed tray must reveal window")
                assert not registered(old_hwnd)
                app._start_tray_icon()
                tray = app.tray_icon
                assert tray.is_available and registered(tray._hwnd)
                probe = tk.Entry(app)
                probe.place(x=15, y=15, width=180)
                probe.focus_force()
                pump(app)
                probe.event_generate("<KeyPress-a>")
                assert probe.get() == "a"
                probe.event_generate("<Control-k>")
                wait(app, lambda: app.search_window and app.search_window.winfo_ismapped(), "Ctrl+K after tray restore")
                send(tray, "exit")
                wait(app, lambda: app.lifecycle.stopped, "exit cancels search and terminates app")
                assert not tray._thread.is_alive() and not registered(tray._hwnd or 0)
                assert not errors, errors
                print("20 cycles; taskbar minimize; Shell TaskbarCreated protocol; tray-loss recovery; search/exit: PASS", flush=True)
            finally:
                if not app.lifecycle.stopped:
                    for overlay in list(app.overlay_windows):
                        if overlay.winfo_exists() and hasattr(overlay, "close"):
                            overlay.close()
                    cleanup(app)


def repeated_start_stop():
    for index in range(10):
        commands = queue.Queue()
        tray = TrayIcon(f"Calendar QA {index}", Path(__file__).resolve().parents[1]/"assets/calendar.ico", commands.put)
        assert tray.start(), tray.error
        hwnd, thread = tray._hwnd, tray._thread
        assert registered(hwnd)
        assert tray.start() and tray._thread is thread
        assert tray.stop()
        assert not thread.is_alive() and not registered(hwnd)
        assert tray.stop()
    missing = TrayIcon("Calendar missing-icon QA", Path("missing-icon.ico"), lambda _action: None)
    assert missing.start(), missing.error
    assert registered(missing._hwnd)
    assert missing.stop()
    print("10 actual Shell service start/stops, idempotence, missing-icon fallback: PASS", flush=True)


def badge_screenshots(directory):
    from app import enable_dpi_awareness
    from tools.check_habit_schedules import capture_own_window
    from tray_badge import BADGE_COLORS, TrayBadgeRenderer

    enable_dpi_awareness()
    directory.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).resolve().parents[1] / 'assets'
    tray = TrayIcon('桌面月历', assets / 'calendar.ico', lambda _action: None)
    window = tk.Tk()
    window.title('桌面月历托盘角标')
    window.configure(bg='#F2F3F5')
    photos = []
    cases = (('green', '实时在线'), ('yellow', '正在重新连接'),
             ('red', '绑定已失效，请重新绑定'), ('gray', '远程服务已关闭'))

    def add_case(parent, title, color, icon):
        size = tray._icon_size
        row = tk.Frame(parent, bg='#F2F3F5')
        row.pack(fill='x', padx=14, pady=5)
        tk.Label(row, text=title, width=23, anchor='w', bg='#F2F3F5', fg='#182131',
                 font=('Segoe UI', 10)).pack(side='left')
        for bg in (0xFAFAFA, 0x232731):
            shade = f'#{bg:06X}'
            panel = tk.Frame(row, bg=shade, padx=10, pady=8)
            panel.pack(side='left', padx=(4, 0))
            with renderer.bitmap(size) as (bitmap, pixels):
                pixels[:] = [0xFF000000 | bg] * (size * size)
                renderer.draw(bitmap, icon, size)
                assert any(pixel & 0xFFFFFF == BADGE_COLORS[color] for pixel in pixels)
                raster = bytes(channel for pixel in pixels for channel
                               in ((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255))
            photo = tk.PhotoImage(master=window, data=f'P6\n{size} {size}\n255\n'.encode() + raster, format='PPM')
            zoom = photo.zoom(2)
            photos.extend((photo, zoom))
            tk.Label(panel, image=photo, bg=shade, bd=0).pack(side='left', padx=(0, 10))
            tk.Label(panel, image=zoom, bg=shade, bd=0).pack(side='left')

    try:
        assert tray.start(), tray.error
        renderer = TrayBadgeRenderer(tray._api.user)
        captured = []
        for color, title in cases:
            tray.update_status(color, f'微信远控：{title}')
            wait(window, lambda: tray._applied_status[0] == color, f'Shell applied {color}')
            assert registered(tray._hwnd)
            assert title in tray._nid.szTip
            icon = tray._nid.hIcon
            assert icon and icon != tray._icon
            captured.append((title, color, icon))
            row = tk.Frame(window, bg='#F2F3F5')
            row.pack()
            add_case(row, title, color, icon)
            pump(window, .12)
            capture_own_window(window, directory / f'tray-{color}.png')
            row.destroy()
        for title, color, icon in captured:
            add_case(window, title, color, icon)
        pump(window, .12)
        capture_own_window(window, directory / 'tray-badges.png')
        known = dict(tray._badge_icons)
        for _ in range(4):
            for title, color, _icon in captured:
                tray.update_status(color, f'微信远控：{title}')
                wait(window, lambda: tray._applied_status[0] == color, f'cached {color}')
        assert tray._badge_icons == known
        tray.update_status(None, '')
        wait(window, lambda: tray._applied_status[0] is None, 'restored original tray icon')
        assert tray._nid.hIcon == tray._icon
        print(f'Four registered badge icons, two background colors, {tray._icon_size}px native size; cached reuse: PASS')
    finally:
        assert tray.stop()
        assert not tray._badge_icons
        window.destroy()


def packaged_checks(executable):
    """Normal app.py bootloader/EXE, not a test entrypoint or injected runtime."""
    from tools.check_update_compatibility import process_path, windows, wait_until
    from PyInstaller.archive.readers import CArchiveReader
    executable = executable.resolve()
    pyz = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
    assert "tray_icon" in pyz.toc and "app_lifecycle" in pyz.toc
    assert "pystray" not in pyz.toc
    U.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM), w.LPARAM]
    U.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    U.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    U.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    U.IsIconic.argtypes = [w.HWND]
    U.GetDpiForWindow.argtypes = [w.HWND]
    kernel = ctypes.WinDLL("kernel32")
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]

    def helpers():
        result = []
        @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        def collect(hwnd, _param):
            name, pid = ctypes.create_unicode_buffer(256), w.DWORD()
            U.GetClassNameW(hwnd, name, 256)
            if name.value.startswith("DesktopCalendarTray_"):
                U.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if process_path(pid.value) == executable:
                    result.append((int(hwnd), pid.value))
            return True
        U.EnumWindows(collect, 0)
        return result

    def command(helper, action):
        identifier = next(key for key, value in TrayIcon.COMMANDS.items() if value == action)
        assert U.PostMessageW(helper, TrayIcon.WM_COMMAND, identifier, 0)

    def bounds(hwnd):
        rectangle = w.RECT()
        assert U.GetWindowRect(hwnd, ctypes.byref(rectangle))
        return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom

    def resources(handle):
        times = [w.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
        seconds = sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) / 10**7
        count = w.DWORD()
        assert kernel.GetProcessHandleCount(handle, ctypes.byref(count))
        return seconds, count.value

    with tempfile.TemporaryDirectory(prefix="calendar-tray-exe-") as temporary:
        folder = Path(temporary)
        data = folder/"DesktopCalendar/calendar_data.json"
        data.parent.mkdir()
        data.write_text(json.dumps({"version": 8, "events": [], "routines": [], "categories": [],
            "settings": {"theme": "modern", "window_mode": "desktop"}}), encoding="utf-8")
        environment = dict(os.environ, APPDATA=str(folder))
        for index in range(10):
            settings = json.loads(data.read_bytes())
            theme = ("modern", "aero", "paper", "frutiger")[index % 4]
            pinned = bool(index % 2)
            settings["settings"].update(theme=theme, window_mode="pinned" if pinned else "desktop")
            data.write_text(json.dumps(settings), encoding="utf-8")
            child = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment)
            handle = None
            try:
                helper, pid = wait_until(lambda: helpers(), "formal EXE tray helper")[0]
                assert len(helpers()) == 1 and registered(helper)
                assert child.poll() is None
                hwnd = wait_until(lambda: windows(executable), "formal Compact window")[0][0]
                assert U.GetDpiForWindow(hwnd) == 192, "This smoke expects the current 200% monitor"
                wait_until(lambda: bool(U.GetWindowLongPtrW(hwnd, -20) & 8) == pinned,
                           "formal startup topmost preference")
                if index == 0:
                    command(helper, "hide")
                    wait_until(lambda: not windows(executable), "formal Compact hide")
                    assert registered(helper)
                    U.PostMessageW(helper, TrayIcon.CALLBACK_MESSAGE, 0, (1 << 16) | TrayIcon.WM_LBUTTONDBLCLK)
                    wait_until(lambda: windows(executable), "formal Compact double-click callback")
                    hwnd = windows(executable)[0][0]
                    U.PostMessageW(hwnd, TrayIcon.WM_CLOSE, 0, 0)
                    wait_until(lambda: not windows(executable), "formal existing close-to-tray policy")
                    command(helper, "show")
                    wait_until(lambda: windows(executable), "formal restore after window close")
                    command(helper, "global")
                    hwnd = wait_until(lambda: next((hwnd for hwnd, _pid in windows(executable)
                        if U.GetWindowLongPtrW(hwnd, -20) & 0x40000), None), "formal Global window")
                    time.sleep(.5)  # Let the existing debounced geometry save settle.
                    assert not U.GetWindowLongPtrW(hwnd, -16) & 0x00C00000
                    U.PostMessageW(hwnd, 0x112, 0xF020, 0)  # WM_SYSCOMMAND / SC_MINIMIZE.
                    wait_until(lambda: U.IsIconic(hwnd), "formal taskbar minimize")
                    command(helper, "show")
                    wait_until(lambda: not U.IsIconic(hwnd), "formal taskbar restore")
                    time.sleep(.5)
                    geometry, baseline = bounds(hwnd), data.read_bytes()
                    handle = kernel.OpenProcess(0x1000, False, pid)
                    assert handle
                    _, handles_before = resources(handle)
                    for _ in range(20):
                        command(helper, "hide")
                        wait_until(lambda: not windows(executable), "formal Global hide")
                        command(helper, "show")
                        wait_until(lambda: windows(executable), "formal Global show")
                    time.sleep(.5)
                    assert bounds(hwnd) == geometry, (geometry, bounds(hwnd))
                    assert data.read_bytes() == baseline
                    _, handles_after = resources(handle)
                    assert handles_after <= handles_before + 8, (handles_before, handles_after)
                    command(helper, "hide")
                    wait_until(lambda: not windows(executable), "formal idle hide")
                    cpu_before, _count = resources(handle)
                    time.sleep(3)
                    cpu_after, _count = resources(handle)
                    assert cpu_after - cpu_before < .3, "hidden process has unexpectedly busy CPU"
                    assert data.read_bytes() == baseline
                    print(f"Formal EXE: Compact/Global/20 cycles; no hidden writes; handles {handles_before}->{handles_after}; "
                          f"idle CPU {cpu_after-cpu_before:.4f}s / 3s; native DPI 192: PASS", flush=True)
                command(helper, "exit")
                child.wait(15)
                assert child.returncode == 0 and not registered(helper)
                assert not helpers()
                print(f"Formal EXE startup/exit {index+1}/10 ({theme}, pinned={pinned}); icon removed, process ended: PASS", flush=True)
            finally:
                if handle:
                    kernel.CloseHandle(handle)
                if child.poll() is None:
                    # Only the process launched above, with the verified QA EXE.
                    assert process_path(child.pid) == executable
                    child.terminate()
                    child.wait(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="Normal app.py PyInstaller QA executable")
    parser.add_argument("--sqlite", action="store_true", help="Use isolated production SQLite startup")
    parser.add_argument("--badge-screenshots", type=Path, help="Capture the actual HICONs in a Windows preview")
    args = parser.parse_args()
    if args.badge_screenshots:
        badge_screenshots(args.badge_screenshots)
    elif args.exe:
        packaged_checks(args.exe)
    else:
        source_checks(sqlite=args.sqlite)
        repeated_start_stop()
