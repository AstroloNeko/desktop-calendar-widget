"""Native Shell icon and menu, isolated from Tk and user data.

One hidden Win32 helper window runs on one message-loop thread. Its callback
only submits strings to a command sink; the application supplies Queue.put.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import threading
import uuid

IS_WINDOWS = os.name == "nt"
WPARAM_T, LPARAM_T = ctypes.c_size_t, ctypes.c_ssize_t

if IS_WINDOWS:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uTimeoutOrVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64),
                    ("dwInfoFlags", w.DWORD), ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    class NOTIFYICONIDENTIFIER(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("guidItem", GUID)]

    WNDPROC = ctypes.WINFUNCTYPE(LPARAM_T, w.HWND, w.UINT, WPARAM_T, LPARAM_T)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class WindowsTrayAPI:
    """Private caches and explicit pointer-wide prototypes; no global mutation."""
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for dll, name, args, result in (
            (self.kernel, "GetModuleHandleW", [w.LPCWSTR], w.HINSTANCE),
            (self.shell, "Shell_NotifyIconW", [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], w.BOOL),
            (self.shell, "Shell_NotifyIconGetRect", [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(w.RECT)], ctypes.c_long),
            (self.user, "RegisterClassW", [ctypes.POINTER(WNDCLASSW)], w.ATOM),
            (self.user, "UnregisterClassW", [w.LPCWSTR, w.HINSTANCE], w.BOOL),
            (self.user, "RegisterWindowMessageW", [w.LPCWSTR], w.UINT),
            (self.user, "CreateWindowExW", [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
            (self.user, "DefWindowProcW", [w.HWND, w.UINT, WPARAM_T, LPARAM_T], LPARAM_T),
            (self.user, "DestroyWindow", [w.HWND], w.BOOL),
            (self.user, "IsWindow", [w.HWND], w.BOOL),
            (self.user, "PostMessageW", [w.HWND, w.UINT, WPARAM_T, LPARAM_T], w.BOOL),
            (self.user, "PostQuitMessage", [ctypes.c_int], None),
            (self.user, "GetMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int),
            (self.user, "TranslateMessage", [ctypes.POINTER(w.MSG)], w.BOOL),
            (self.user, "DispatchMessageW", [ctypes.POINTER(w.MSG)], LPARAM_T),
            (self.user, "LoadImageW", [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT], w.HANDLE),
            (self.user, "LoadIconW", [w.HINSTANCE, w.LPCWSTR], w.HICON),
            (self.user, "DestroyIcon", [w.HICON], w.BOOL),
            (self.user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self.user, "CreatePopupMenu", [], w.HMENU),
            (self.user, "AppendMenuW", [w.HMENU, w.UINT, WPARAM_T, w.LPCWSTR], w.BOOL),
            (self.user, "TrackPopupMenu", [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, w.HWND, w.LPVOID], w.UINT),
            (self.user, "DestroyMenu", [w.HMENU], w.BOOL),
            (self.user, "EndMenu", [], w.BOOL),
            (self.user, "SetForegroundWindow", [w.HWND], w.BOOL),
            (self.user, "GetCursorPos", [ctypes.POINTER(w.POINT)], w.BOOL),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result


class TrayIcon:
    CALLBACK_MESSAGE = 0x0400 + 27
    WM_COMMAND, WM_DESTROY, WM_CLOSE, WM_NULL = 0x111, 2, 0x10, 0
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x202, 0x203, 0x205, 0x7B
    NIN_KEYSELECT = 0x401
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 1, 2, 4, 0x80
    NOTIFYICON_VERSION_4 = 4
    MENU = ((1001, "显示桌面月历", "show"), (1002, "打开全局视图", "global"),
            (1003, "隐藏到托盘", "hide"), (0, None, None), (1005, "退出桌面月历", "exit"))
    COMMANDS = {identifier: command for identifier, _label, command in MENU if command}

    def __init__(self, tooltip, icon_path: Path, on_action, *, startup_message=None):
        self.tooltip, self.icon_path, self.on_action = tooltip[:127], Path(icon_path), on_action
        # Compatibility only: Phase 1A does not introduce balloon notifications.
        self.startup_message = startup_message
        self._thread = self._hwnd = self._wndproc = self._nid = self._icon = self._api = None
        self._ready, self._available, self._stopping = threading.Event(), threading.Event(), threading.Event()
        self._class_name = f"DesktopCalendarTray_{os.getpid()}_{uuid.uuid4().hex}"
        self._taskbar_created = 0
        self.error = None

    @property
    def is_available(self):
        return bool(self._available.is_set() and self._hwnd and self._thread
                    and self._thread.is_alive() and not self._stopping.is_set())

    def start(self):
        if not IS_WINDOWS or self._stopping.is_set():
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="calendar-tray", daemon=True)
            try:
                self._thread.start()
            except (RuntimeError, OSError) as exc:
                self.error = f"无法启动托盘线程：{exc}"
                self._ready.set()
                return False
        self._ready.wait(2)
        if not self._ready.is_set():
            self.error = "系统托盘初始化超时"
            self.stop()
        return self.is_available

    def probe_available(self):
        """Read-only Shell check before hiding, or at a slow hidden health interval.

        Explorer can disappear before it broadcasts TaskbarCreated. A live
        helper HWND alone must not strand a hidden application in that gap.
        """
        if not self.is_available:
            return False
        identifier = NOTIFYICONIDENTIFIER()
        identifier.cbSize, identifier.hWnd, identifier.uID = ctypes.sizeof(identifier), self._hwnd, 1
        rectangle = w.RECT()
        if self._api.shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rectangle)) != 0:
            self._available.clear()
            self.error = "Windows 通知区域图标已失效"
            self._emit("tray_unavailable")
            return False
        return True

    def stop(self):
        self._stopping.set()
        self._available.clear()
        if self._api and self._hwnd:
            self._api.user.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        if self._thread and self._thread is not threading.current_thread() and self._thread.is_alive():
            self._thread.join(timeout=2)
        return not self._thread or not self._thread.is_alive()

    def _emit(self, action):
        if not self._stopping.is_set():
            try:
                self.on_action(action)
            except Exception as exc:
                self.error = f"托盘命令投递失败：{exc}"

    def _register_icon(self):
        self._available.clear()
        if self._stopping.is_set():
            return False
        self._nid.uTimeoutOrVersion = 0
        if not self._api.shell.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self._nid)):
            self.error = "Windows 通知区域暂时不可用"
            self._emit("tray_unavailable")
            return False
        self._nid.uTimeoutOrVersion = self.NOTIFYICON_VERSION_4
        if not self._api.shell.Shell_NotifyIconW(self.NIM_SETVERSION, ctypes.byref(self._nid)):
            self._api.shell.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid))
            self.error = "Windows 通知区域回调注册失败"
            self._emit("tray_unavailable")
            return False
        self.error = None
        self._available.set()
        return True

    def _handle_message(self, hwnd, message, wparam, lparam):
        user = self._api.user
        if message == self._taskbar_created and self._taskbar_created:
            self._register_icon()
            return 0
        if message == self.CALLBACK_MESSAGE:
            event = int(lparam) & 0xFFFF
            if event in (self.WM_LBUTTONDBLCLK, self.NIN_KEYSELECT):
                self._emit("show")
            elif event == self.WM_CONTEXTMENU:
                self._show_menu(hwnd)
            return 0
        if message == self.WM_COMMAND:
            action = self.COMMANDS.get(int(wparam) & 0xFFFF)
            if action:
                self._emit(action)
            return 0
        if message == self.WM_CLOSE:
            user.EndMenu()
            user.DestroyWindow(hwnd)
            return 0
        if message == self.WM_DESTROY:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        instance = None
        registered = owns_icon = False
        try:
            self._api = WindowsTrayAPI()
            user = self._api.user
            instance = self._api.kernel.GetModuleHandleW(None)
            self._taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
            if not self._taskbar_created:
                raise OSError("RegisterWindowMessageW failed")

            def dispatch(hwnd, message, wparam, lparam):
                try:
                    return self._handle_message(hwnd, message, wparam, lparam)
                except Exception as exc:
                    self.error = f"托盘消息处理失败：{exc}"
                    self._available.clear()
                    self._emit("tray_unavailable")
                    user.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
                    return 0

            self._wndproc = WNDPROC(dispatch)
            window_class = WNDCLASSW(0, self._wndproc, 0, 0, instance, None, None, None, None, self._class_name)
            if not user.RegisterClassW(ctypes.byref(window_class)):
                raise OSError("RegisterClassW failed")
            registered = True
            self._hwnd = user.CreateWindowExW(0, self._class_name, self.tooltip, 0,
                                             0, 0, 0, 0, None, None, instance, None)
            if not self._hwnd:
                raise OSError("CreateWindowExW failed")
            size = max(16, user.GetSystemMetrics(49))
            self._icon = user.LoadImageW(None, str(self.icon_path), 1, size, size, 0x10)
            owns_icon = bool(self._icon)
            if not self._icon:
                self._icon = user.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), w.LPCWSTR))
            if not self._icon:
                raise OSError("Unable to load tray icon")
            self._nid = NOTIFYICONDATAW()
            self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._nid.hWnd, self._nid.uID = self._hwnd, 1
            self._nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP | self.NIF_SHOWTIP
            self._nid.uCallbackMessage, self._nid.hIcon = self.CALLBACK_MESSAGE, self._icon
            self._nid.szTip = self.tooltip
            if not self._register_icon():
                return
            self._ready.set()
            message = w.MSG()
            while not self._stopping.is_set():
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise OSError("GetMessageW failed")
                if result == 0:
                    break
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = f"系统托盘不可用：{exc}"
        finally:
            self._available.clear()
            if self._api:
                if self._nid is not None:
                    self._api.shell.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid))
                if self._hwnd and self._api.user.IsWindow(self._hwnd):
                    self._api.user.DestroyWindow(self._hwnd)
                if self._icon and owns_icon:
                    self._api.user.DestroyIcon(self._icon)
                if registered:
                    self._api.user.UnregisterClassW(self._class_name, instance)
            self._hwnd = None
            self._ready.set()
            if not self._stopping.is_set():
                self.error = self.error or "托盘事件线程意外结束"
                self._emit("tray_unavailable")

    def _show_menu(self, hwnd):
        user = self._api.user
        menu = user.CreatePopupMenu()
        if not menu:
            return
        try:
            for identifier, label, _command in self.MENU:
                if not user.AppendMenuW(menu, 0 if label else 0x800, identifier, label):
                    raise OSError("AppendMenuW failed")
            point = w.POINT()
            user.GetCursorPos(ctypes.byref(point))
            user.SetForegroundWindow(hwnd)
            command = user.TrackPopupMenu(menu, 0x102, point.x, point.y, 0, hwnd, None)
        finally:
            user.DestroyMenu(menu)
            user.PostMessageW(hwnd, self.WM_NULL, 0, 0)
        action = self.COMMANDS.get(int(command))
        if action:
            self._emit(action)
