"""Opt-in Windows transport service. One MTA owner; all outputs are Queues.

Source/frozen builds and isolated data paths have distinct deterministic AUMIDs.
Only this installation's owned registration is updated/removed. Cold activation
is deliberately a no-op CLI path, never a second calendar instance.
"""
from __future__ import annotations

import hashlib
import ctypes
import os
from pathlib import Path
import queue
import sys
import threading

from native_notifications import NativeNotificationError, WinRTTransport, toast_xml
from reminder_engine import notification_arguments


def notification_identity(executable, data_file, *, frozen):
    digest = hashlib.sha256(f"{Path(executable).resolve()}|{Path(data_file).resolve()}".encode()).hexdigest()[:24]
    return "AstroloNeko.DesktopCalendar." + ("" if frozen else "Development.") + digest


def programs_directory():
    # Windows known folder, not APPDATA: QA can redirect calendar APPDATA without
    # installing a shortcut in a fake Start Menu that Explorer cannot see.
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(260)
    result = shell.SHGetFolderPathW(None, 2, None, 0, buffer)  # CSIDL_PROGRAMS
    if result < 0:
        raise OSError("无法定位当前用户开始菜单；未注册通知。")
    return Path(buffer.value)


class NotificationRegistration:
    def __init__(self, identity, root, executable, arguments):
        self.identity, self.root, self.executable, self.arguments = identity, Path(root), Path(executable), arguments
        self.shortcut = programs_directory() / ("桌面月历提醒-" + identity.rsplit(".", 1)[-1] + ".lnk")
        self.keys = []
        self.registered = False

    def register(self, transport):
        import winreg
        app_key = "Software\\Classes\\AppUserModelId\\" + self.identity
        com_key = "Software\\Classes\\CLSID\\" + transport.activator
        # AUMID/CLSID include this installation + data-path hash; do not overwrite
        # any unrelated registration if those keys were unexpectedly occupied.
        for path in (app_key, com_key + "\\LocalServer32"):
            try:
                existing = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                continue
            with existing as key:
                try:
                    owner = winreg.QueryValueEx(key, "DesktopCalendarOwner")[0]
                except FileNotFoundError:
                    raise RuntimeError("通知注册的来源无法确认，未覆盖。") from None
                if owner != self.identity:
                    raise RuntimeError("通知注册被其他程序占用，未覆盖。")
        transport.shortcut(self.shortcut, self.executable, self.arguments, self.root / "assets/calendar.ico", self.identity)
        self.registered = True
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app_key) as key:
            self.keys.append(app_key)
            for name, value in {"DesktopCalendarOwner": self.identity, "DisplayName": "桌面月历",
                                "IconUri": str(self.root / "assets/calendar.ico"),
                                "CustomActivator": transport.activator}.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, com_key + "\\LocalServer32") as key:
            self.keys.extend([com_key + "\\LocalServer32", com_key])
            winreg.SetValueEx(key, "DesktopCalendarOwner", 0, winreg.REG_SZ, self.identity)
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ,
                              f'"{self.executable}" {self.arguments} --native-notification-activation')

    def close(self):
        import winreg
        errors = []
        if self.registered and self.shortcut.exists():
            try:
                self.shortcut.unlink()
            except OSError as error:
                errors.append(str(error))
        for path in self.keys:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(str(error))
        self.keys.clear()
        if errors:
            raise OSError("本应用通知注册清理未完成：" + "; ".join(errors))


class WindowsNotificationBackend:
    def __init__(self, root, data_file, *, identity=None):
        self.root = Path(root)
        self.identity = identity or notification_identity(sys.executable if getattr(sys, "frozen", False) else Path(__file__),
                                                          data_file, frozen=bool(getattr(sys, "frozen", False)))
        self.events = queue.Queue()
        self._jobs = queue.Queue()
        self._thread = None
        self._transport = None
        self._transport_lock = threading.Lock()
        self._cancelled_tags = set()
        self._stopped = False
        self.status = "尚未启用"

    def start(self):
        if self._stopped:
            raise RuntimeError("通知服务已关闭")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="calendar-notifications", daemon=True)
            self._thread.start()

    def send(self, plan, title, text, actions):
        self.start()
        self._cancelled_tags.discard(plan.tag)
        self._jobs.put(("show", (plan, title, text, actions)))

    def remove(self, tag):
        self._cancelled_tags.add(tag)
        if self._thread and not self._stopped:
            self._jobs.put(("remove", tag))

    def close(self):
        if self._stopped:
            return
        self._stopped = True
        with self._transport_lock:
            if self._transport:
                self._transport.silence()
        if self._thread:
            self._jobs.put(("stop", None))
            self._thread.join(2)

    def _run(self):
        transport = registration = None
        initialization_error = None
        try:
            executable = Path(sys.executable)
            arguments = ""
            if not getattr(sys, "frozen", False):
                candidate = executable.with_name("pythonw.exe")
                executable = candidate if candidate.exists() else executable
                arguments = f'"{Path(__file__).resolve().parent / "app.py"}"'
            registration = NotificationRegistration(self.identity, self.root, executable, arguments)
            dll = self.root / ("DesktopCalendarNotifications.dll" if getattr(sys, "frozen", False)
                               else "build/native-notifications/DesktopCalendarNotifications.dll")
            transport = WinRTTransport(dll, self.identity, event_sink=lambda event: self.events.put(("native", event)))
            registration.register(transport)
            transport.open()
            with self._transport_lock:
                self._transport = transport
                if self._stopped:
                    transport.silence()
            try:
                setting = transport.setting()
                self.status = "Windows 通知可用" if setting == 0 else f"Windows 通知受限（状态 {setting}）"
            except Exception:
                self.status = "Windows 权限状态未知；系统仍会执行通知/专注策略"
            self.events.put(("status", self.status))
        except Exception as error:
            initialization_error = str(error)
            self.status = "Windows 通知不可用：" + initialization_error
            self.events.put(("status", self.status))
        try:
            while True:
                action, value = self._jobs.get()
                if action == "stop":
                    break
                if self._stopped:
                    continue
                if action == "show":
                    plan, title, text, actions = value
                    try:
                        if plan.tag in self._cancelled_tags:
                            continue
                        if initialization_error:
                            raise RuntimeError(initialization_error)
                        try:
                            permission = transport.setting()
                        except NativeNotificationError as error:
                            if error.hresult != 0x80070490:
                                raise
                            permission = None  # Unknown, not Enabled; Windows still applies policy.
                        if permission is not None and permission != 0:
                            raise RuntimeError(f"Windows 已限制通知（状态 {permission}）；未派发。")
                        transport.show(plan.tag, toast_xml(title, text, notification_arguments(plan),
                                                           self.root / "assets/calendar.png", actions))
                        self.events.put(("delivery", (plan.key, True)))
                    except Exception as error:
                        self.events.put(("error", str(error)))
                        self.events.put(("delivery", (plan.key, False)))
                elif action == "remove":
                    try:
                        if not initialization_error:
                            transport.remove(value)
                    except Exception as error:
                        self.events.put(("error", str(error)))
                    finally:
                        self._cancelled_tags.discard(value)
        finally:
            with self._transport_lock:
                self._transport = None
            if transport:
                try:
                    transport.close()
                except Exception as error:
                    self.events.put(("error", str(error)))
            if registration:
                try:
                    registration.close()
                except Exception as error:
                    self.events.put(("error", str(error)))
