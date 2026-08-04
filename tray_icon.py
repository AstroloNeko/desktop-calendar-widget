from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional


IS_WINDOWS = os.name == "nt"
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t


if IS_WINDOWS:
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]


    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]


class TrayIcon:
    """Small native Windows tray icon with no third-party runtime dependency."""

    WM_USER = 0x0400
    CALLBACK_MESSAGE = WM_USER + 27
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_NULL = 0x0000
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2
    NIM_SETVERSION = 4
    NIF_MESSAGE = 0x1
    NIF_ICON = 0x2
    NIF_TIP = 0x4
    NIF_INFO = 0x10
    NOTIFYICON_VERSION_4 = 4
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x10
    LR_DEFAULTSIZE = 0x40
    MF_STRING = 0x0
    MF_SEPARATOR = 0x800
    TPM_RIGHTBUTTON = 0x2
    TPM_RETURNCMD = 0x100
    NIIF_INFO = 0x1

    COMMANDS = {
        1001: "show",
        1002: "new",
        1003: "today",
        1004: "update",
        1005: "exit",
    }

    def __init__(
        self,
        tooltip: str,
        icon_path: Path,
        on_action: Callable[[str], None],
        *,
        startup_message: Optional[str] = None,
    ) -> None:
        self.tooltip = tooltip[:127]
        self.icon_path = Path(icon_path)
        self.on_action = on_action
        self.startup_message = startup_message
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._ready = threading.Event()
        self._wndproc = None
        self._icon = None
        self._nid = None
        self.error: Optional[str] = None
        self._class_name = f"DesktopCalendarTray_{os.getpid()}"

    def start(self) -> bool:
        if not IS_WINDOWS:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="calendar-tray", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return bool(self._hwnd)

    def stop(self) -> None:
        if IS_WINDOWS and self._hwnd:
            post_message = ctypes.windll.user32.PostMessageW
            post_message.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
            post_message(wintypes.HWND(self._hwnd), self.WM_CLOSE, 0, 0)
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        self._hwnd = None

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T)

        def wndproc(hwnd, message, wparam, lparam):
            if message == self.CALLBACK_MESSAGE:
                event = int(lparam) & 0xFFFF
                if event in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                    self._emit("show")
                elif event in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                return 0
            if message == self.WM_COMMAND:
                action = self.COMMANDS.get(int(wparam) & 0xFFFF)
                if action:
                    self._emit(action)
                return 0
            if message == self.WM_DESTROY:
                if self._nid is not None:
                    shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = wndproc_type(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(0, self._wndproc, 0, 0, instance, None, None, None, None, self._class_name)
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self.error = "RegisterClassW failed"
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(0, self._class_name, self.tooltip, 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            self.error = "CreateWindowExW failed"
            user32.UnregisterClassW(self._class_name, instance)
            self._ready.set()
            return
        self._hwnd = int(hwnd)
        self._icon = user32.LoadImageW(None, str(self.icon_path), self.IMAGE_ICON, 0, 0, self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE)
        if not self._icon:
            self._icon = user32.LoadIconW(None, ctypes.c_void_p(32512))

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.CALLBACK_MESSAGE
        nid.hIcon = self._icon
        nid.szTip = self.tooltip
        if self.startup_message:
            nid.uFlags |= self.NIF_INFO
            nid.szInfo = self.startup_message[:255]
            nid.szInfoTitle = "桌面月历正在运行"
            nid.dwInfoFlags = self.NIIF_INFO
        self._nid = nid
        if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
            self.error = f"Shell_NotifyIconW(NIM_ADD) failed (IsWindow={bool(user32.IsWindow(hwnd))}, hwnd={int(hwnd)})"
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(self._class_name, instance)
            self._hwnd = None
            self._ready.set()
            return
        nid.uTimeoutOrVersion = self.NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(self.NIM_SETVERSION, ctypes.byref(nid))
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        if self._icon:
            user32.DestroyIcon(self._icon)
        user32.UnregisterClassW(self._class_name, instance)
        self._hwnd = None

    def _emit(self, action: str) -> None:
        try:
            self.on_action(action)
        except Exception:
            pass

    def _show_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        ]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, self.MF_STRING, 1001, "显示月历")
        user32.AppendMenuW(menu, self.MF_STRING, 1002, "新建日程")
        user32.AppendMenuW(menu, self.MF_STRING, 1003, "回到今天")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, 1004, "检查更新")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, 1005, "退出桌面月历")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        user32.PostMessageW(hwnd, self.WM_NULL, 0, 0)
        action = self.COMMANDS.get(int(command))
        if action:
            self._emit(action)
