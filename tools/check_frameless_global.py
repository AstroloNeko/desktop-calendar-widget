"""Opt-in real Windows chrome smoke; temporary data only, no tray registration.

Run: py -3 tools/check_frameless_global.py [--taskbar] [--screenshots]
--taskbar clicks only this process's uniquely identified taskbar button, 20 times.
The 96..192 checks simulate DPI transitions; the native monitor DPI is reported.
"""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import CalendarApp, messagebox
from calendar_core import Store
from dpi_utils import dpi_for_window, scaled_geometry
from global_header import header_layout
from tools.check_interactions import cleanup, pump
from win_integration import is_window_topmost, window_handle


def powershell(script):
    return subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)


def wait_process(app, process, timeout=15):
    end = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < end:
        pump(app)
    if process.poll() is None:
        process.kill()
        raise AssertionError("Native smoke helper timed out")
    out, error = process.communicate()
    assert process.returncode == 0, (out, error)
    return out.strip()


def taskbar_restore(app, app_id):
    # UIAutomation identifies only this isolated AppUserModelID group.
    # Clicking its actual taskbar button tests the shell route, not deiconify().
    script = r"""Add-Type -AssemblyName UIAutomationClient
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class SmokeMouse {
 [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr c);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint x,uint y,uint d,UIntPtr e);
 [DllImport("user32.dll")] public static extern void keybd_event(byte a,byte b,uint c,UIntPtr d);
 [StructLayout(LayoutKind.Sequential)] public struct Point { public int X; public int Y; }
 [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point p);
 [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h,uint f);
}'
[SmokeMouse]::SetThreadDpiAwarenessContext([IntPtr](-4)) | Out-Null
# Win+T reveals an auto-hidden taskbar even above a fullscreen application.
[SmokeMouse]::keybd_event(0x5B,0,0,[UIntPtr]::Zero)
[SmokeMouse]::keybd_event(0x54,0,0,[UIntPtr]::Zero)
[SmokeMouse]::keybd_event(0x54,0,2,[UIntPtr]::Zero)
[SmokeMouse]::keybd_event(0x5B,0,2,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 400
$bar = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
 [System.Windows.Automation.TreeScope]::Children,
 (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty,'Shell_TrayWnd')))
$buttons = $bar.FindAll([System.Windows.Automation.TreeScope]::Descendants,
 (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)))
$button = $buttons | Where-Object { $_.Current.AutomationId -eq 'Appid: QA_APP_ID' } | Select-Object -First 1
if ($null -eq $button) { throw 'Isolated QA taskbar button not found' }
$r=$button.Current.BoundingRectangle
$invoke = $button.GetSupportedPatterns() | Where-Object { $_.Id -eq [System.Windows.Automation.InvokePattern]::Pattern.Id }
if ($null -ne $invoke) {
 $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
} else {
$p = New-Object SmokeMouse+Point
$p.X=[int]($r.X+$r.Width/2); $p.Y=[int]($r.Y+$r.Height/2)
$target=[SmokeMouse]::GetAncestor([SmokeMouse]::WindowFromPoint($p),2)
if ($target -ne [IntPtr]$bar.Current.NativeWindowHandle) { throw 'Taskbar is covered; refusing to click another application' }
[SmokeMouse]::SetCursorPos($p.X,$p.Y) | Out-Null
[SmokeMouse]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 60
[SmokeMouse]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
}
$button.Current.Name
""".replace("QA_APP_ID", app_id)
    return wait_process(app, powershell(script))


def capture(app, path):
    x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
    script = f"""Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap({w},{h})
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen({x},{y},0,0,$bitmap.Size)
$bitmap.Save('{str(path)}',[System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose(); $bitmap.Dispose()
"""
    wait_process(app, powershell(script))


def assert_header(app):
    header = app.global_header
    controls = sorted(header.controls.values(), key=lambda control: control.winfo_x())
    for control in controls:
        assert control.winfo_ismapped()
        assert control.winfo_x() >= 0
        assert control.winfo_x() + control.winfo_width() <= header.winfo_width()
        # All labels fit, including the primary action and changing pin text.
        for item in control.find_all():
            if control.type(item) == "text":
                box = control.bbox(item)
                assert 0 <= box[0] and box[2] <= control.winfo_width(), (control.text, box)
    for left, right in zip(controls, controls[1:]):
        assert left.winfo_x() + left.winfo_width() < right.winfo_x()
    assert header.winfo_rooty() - app.winfo_rooty() <= app.dpi.px(5)


def owned_messagebox_handle(user):
    """Never inspect or dismiss another running calendar's native dialog."""
    user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowExW.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    candidate = None
    while True:
        candidate = user.FindWindowExW(None, candidate, "#32770", "桌面月历")
        if not candidate:
            raise AssertionError("This QA process's messagebox not found")
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(candidate, ctypes.byref(pid))
        if pid.value == os.getpid():
            return candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--taskbar", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--shell-only", action="store_true")
    options = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Windows GUI session required")
    shell = ctypes.windll.shell32
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    app_id = f"DesktopCalendar.FramelessQA.{os.getpid()}"
    shell.SetCurrentProcessExplicitAppUserModelID(app_id)
    user = ctypes.windll.user32
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user.SendMessageW.restype = ctypes.c_ssize_t
    errors = []
    with tempfile.TemporaryDirectory() as temporary:
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(Store(Path(temporary) / "data.json"))
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                app.enter_global_view()
                pump(app, .4)
                actual_dpi = dpi_for_window(app)
                print("Native monitor DPI:", actual_dpi, flush=True)
                compact = app.store.settings["compact_geometry"].copy()
                if options.taskbar:
                    # Give the QA process real user-input activation before modal tests;
                    # Windows correctly denies background processes arbitrary focus theft.
                    app.minimize_global()
                    pump(app, .12)
                    taskbar_restore(app, app_id)
                    pump(app, .2)
                    assert app.state() == "normal"
                for theme in (() if options.shell_only else ("modern", "aero", "paper", "frutiger")):
                    app.set_theme(theme)
                    pump(app, .2)
                    for dpi in (96, 120, 144, 168, 192):
                        with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                            app._apply_dpi_change(dpi)
                            for width in (720, 1240):
                                app.geometry(scaled_geometry(width, 600, 80, 50, dpi))
                                pump(app, .22)
                                assert_header(app)
                    # Restore real monitor DPI for native Z-order and screenshots.
                    app._apply_dpi_change(actual_dpi)
                    pump(app, .25)
                    app.window_mode = "desktop"
                    app.apply_window_mode()
                    assert not is_window_topmost(app)
                    from tools.check_window_drag import drag_header
                    drag_header(app, 30, 10)
                    assert not is_window_topmost(app)
                    focus = app.focus_get()
                    foreground = user.GetForegroundWindow()
                    tip = app.global_header.tooltips[-2]
                    tip.show()
                    pump(app)
                    assert tip.window is not None and app.grab_current() is None
                    assert app.focus_get() == focus and user.GetForegroundWindow() == foreground
                    assert not is_window_topmost(tip.window)
                    tip.hide()
                    app.window_mode = "pinned"
                    app.apply_window_mode()
                    assert is_window_topmost(app)
                    drag_header(app, 30, 10)
                    assert is_window_topmost(app)
                    assert app.global_header.tooltips[6].text() == "取消置顶"
                    # A release alone cannot activate the toolbar after moving.
                    create = app.global_header.controls["create"]
                    create.event_generate("<ButtonRelease-1>", x=10, y=10)
                    pump(app)
                    assert app.editor_window is None
                    point = wintypes.POINT(create.winfo_rootx()+create.winfo_width()//2,
                                           create.winfo_rooty()+create.winfo_height()//2)
                    assert user.GetAncestor(user.WindowFromPoint(point), 2) == window_handle(app)
                    user.SetCursorPos(point.x, point.y)
                    user.mouse_event(2, 0, 0, 0, 0)
                    pump(app, .03)
                    user.mouse_event(4, 0, 0, 0, 0)
                    pump(app, .15)
                    assert app.grab_current() is app.editor_window
                    assert str(app.editor_window.transient()) == str(app)
                    assert not is_window_topmost(app)
                    app.editor_window.close()
                    pump(app, .25)
                    assert app.grab_current() is None
                    assert is_window_topmost(app)
                    app.window_mode = "desktop"
                    app.apply_window_mode()
                    for _ in range(2):
                        app.open_category_editor()
                        pump(app, .1)
                        editor = app.category_editor
                        assert app.grab_current() is editor
                        editor.name_entry.insert(0, "QA")
                        assert editor.name_var.get() == "QA"
                        # Real native messagebox owned by the editor. Close with WM_CLOSE.
                        def dismiss(attempt=0):
                            hwnd = owned_messagebox_handle(user)
                            visible = user.IsWindowVisible(hwnd)
                            bounds = wintypes.RECT()
                            user.GetWindowRect(hwnd, ctypes.byref(bounds))
                            point = wintypes.POINT((bounds.left + bounds.right)//2,
                                                  (bounds.top + bounds.bottom)//2)
                            hit = user.WindowFromPoint(point)
                            visible_root = user.GetAncestor(hit, 2)
                            # Verify real Z-order/clickability, not the foreground
                            # lock policy (Windows can transiently report NULL).
                            if (not visible or visible_root != hwnd) and attempt < 10:
                                app.after(100, lambda: dismiss(attempt + 1))
                                return
                            user.PostMessageW(hwnd, 0x10, 0, 0)
                            assert visible and visible_root == hwnd, (hwnd, visible, visible_root)
                        editor.name_var.set("")
                        app.after(150, dismiss)
                        with patch.object(messagebox, "showinfo", wraps=messagebox.showinfo) as dialog:
                            editor.save()
                            assert dialog.call_args.kwargs["parent"] is editor
                        assert app.grab_current() is editor
                        editor.close()
                        pump(app, .16)
                        assert app.grab_current() is None
                    app.open_search()
                    pump(app)
                    assert app.grab_current() is app.search_window
                    app.search_window.close()
                    pump(app)
                    if options.screenshots:
                        capture(app, Path.cwd() / f"qa_frameless_{theme}.png")
                    app.return_to_compact_view()
                    pump(app, .2)
                    assert app.winfo_width() == app.dpi.px(compact["width"])
                    assert abs(app.winfo_x() - compact["x"]) <= 2
                    app.open_day_detail()
                    pump(app, .12)
                    assert app.grab_current() is app.day_detail_window
                    app.day_detail_window.close()
                    pump(app, .15)
                    app.enter_global_view()
                    pump(app, .2)
                    assert_header(app)
                    assert not errors, errors
                    print(theme, "96/120/144/168/192 layout; pin/modal/category/Compact/search: passed", flush=True)

                # Header whitespace drags the window, controls don't bind that gesture.
                app.geometry(scaled_geometry(1000, 600, 80, 50, actual_dpi))
                pump(app, .2)
                from tools.check_window_drag import drag_header
                old = app.winfo_x(), app.winfo_y()
                drag_header(app, 60, 30)
                assert (app.winfo_x(), app.winfo_y()) == (old[0]+60, old[1]+30)

                # Native non-client hit testing still provides a right resize edge.
                rect = wintypes.RECT()
                user.GetWindowRect(window_handle(app), ctypes.byref(rect))
                edge = ((rect.top + (rect.bottom-rect.top)//2) & 0xffff) << 16 | ((rect.right-1) & 0xffff)
                assert user.SendMessageW(window_handle(app), 0x84, 0, edge) == 11  # WM_NCHITTEST / HTRIGHT

                # Verify shell restore keeps both renderer objects and HWND unchanged.
                name = f"DC-Frameless-QA-{os.getpid()}"
                app.title(name)
                hwnd = window_handle(app)
                canvases = app.global_flow_canvas, app.global_timeline_canvas
                for cycle in range(20):
                    app.minimize_global()
                    pump(app, .12)
                    assert app.state() == "iconic" and user.IsIconic(hwnd)
                    if options.taskbar:
                        label = taskbar_restore(app, app_id)
                    else:
                        app.deiconify()
                    deadline = time.monotonic() + 3
                    while app.state() != "normal" and time.monotonic() < deadline:
                        pump(app)
                    pump(app, .15)
                    assert app.state() == "normal" and not user.IsIconic(hwnd), (cycle, app.state(), label if options.taskbar else "Tk", user.GetForegroundWindow(), hwnd)
                    assert window_handle(app) == hwnd
                    assert (app.global_flow_canvas, app.global_timeline_canvas) == canvases
                    style = user.GetWindowLongPtrW(hwnd, -16)
                    extended = user.GetWindowLongPtrW(hwnd, -20)
                    assert style & 0x00C00000 == 0
                    assert style & 0x40000  # native sizing border
                    assert extended & 0x40000 and not extended & 0x80  # taskbar / Alt+Tab
                    print("Restore", cycle+1, "shell taskbar" if options.taskbar else "Tk", flush=True)

                # Restore, then drag again and reach the work-area edges without
                # putting the pointer on a Snap hot zone.
                drag_header(app, 24, 12)
                work = app.dpi.work_area()
                drag_header(app, work.left+4-app.winfo_x(), work.top+4-app.winfo_y())
                edge_rect = wintypes.RECT()
                user.GetWindowRect(hwnd, ctypes.byref(edge_rect))
                assert edge_rect.left >= work.left and edge_rect.top >= work.top
                assert edge_rect.right <= work.right and edge_rect.bottom <= work.bottom
                # Actual Alt+Tab away and back, then the native system-close path.
                user.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
                for returning in (False, True):
                    user.keybd_event(0x12, 0, 0, 0)
                    user.keybd_event(0x09, 0, 0, 0)
                    user.keybd_event(0x09, 0, 2, 0)
                    user.keybd_event(0x12, 0, 2, 0)
                    pump(app, .4)
                    assert (user.GetForegroundWindow() == hwnd) == returning
                closed = []
                app.protocol("WM_DELETE_WINDOW", lambda: closed.append(True))
                assert user.GetForegroundWindow() == hwnd  # never send close to another app
                user.keybd_event(0x12, 0, 0, 0)
                user.keybd_event(0x73, 0, 0, 0)
                user.keybd_event(0x73, 0, 2, 0)
                user.keybd_event(0x12, 0, 2, 0)
                pump(app, .15)
                assert closed == [True]
                assert not errors, errors
                print("Native shell/Alt+F4/drag/no-rebuild checks: passed", flush=True)
                # Save away from work-area margins; restart fitting must not
                # legitimately clamp the position used for this exact check.
                app.geometry(scaled_geometry(1000, 600, 80, 50, actual_dpi))
                pump(app, .3)
                app._save_window_settings()
            finally:
                cleanup(app)
            saved = Store(Path(temporary) / "data.json").settings["global_geometry"].copy()
            for restart in range(3):
                app = CalendarApp(Store(Path(temporary) / "data.json"))
                app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
                try:
                    pump(app, .6)
                    app.enter_global_view()
                    pump(app, .3)
                    assert app._capture_window_geometry().as_dict() == saved
                    assert not errors, errors
                    print("Geometry reload", restart+1, "exact client size/position: passed", flush=True)
                finally:
                    cleanup(app)


if __name__ == "__main__":
    main()
