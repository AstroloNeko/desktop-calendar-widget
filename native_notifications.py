"""Small in-box Windows WinRT transport. No Tk calls or automatic registration.

All methods (including close) belong to one dedicated MTA worker thread. Native
callbacks copy strings to a Queue only. Show success means accepted by Windows,
not that a banner was visible (Focus Assist and OS notification settings win).
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
import uuid
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


def toast_xml(title: str, text: str, arguments: str, image: Path | None = None, actions=()) -> str:
    logo = (f'<image placement="appLogoOverride" src={quoteattr(image.resolve().as_uri())}/>'
            if image else "")
    controls = "".join(f'<action content={quoteattr(label)} arguments={quoteattr(value)} activationType="foreground"/>'
                       for label, value in actions)
    controls = f"<actions>{controls}</actions>" if controls else ""
    return (f'<toast launch={quoteattr(arguments)} duration="long"><visual>'
            f'<binding template="ToastGeneric">{logo}<text>{escape(title)}</text>'
            f'<text>{escape(text)}</text></binding></visual>{controls}</toast>')


class NativeNotificationError(RuntimeError):
    def __init__(self, message, *, hresult=None):
        super().__init__(message)
        self.hresult = hresult


class WinRTTransport:
    """Owned by the creating thread; callback events never contain widget refs."""

    def __init__(self, dll_path: Path, identity: str, *, event_sink=None):
        if os.name != "nt":
            raise NativeNotificationError("Windows notifications require Windows")
        self._owner = threading.get_ident()
        self._identity = identity
        self.activator = "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, identity)) + "}"
        self.events: queue.Queue[tuple[int, str]] = queue.Queue()
        self._context = ctypes.c_void_p()
        self._dll = ctypes.WinDLL(str(dll_path.resolve()))
        self._callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_wchar_p)
        self._callback = self._callback_type(lambda kind, text: (event_sink or self.events.put)((kind, text or "")))
        pointer = ctypes.c_void_p
        wide = ctypes.c_wchar_p
        integer = ctypes.POINTER(ctypes.c_int)
        signatures = {
            "dc_open": ([wide, wide, self._callback_type, ctypes.POINTER(pointer)], ctypes.c_long),
            "dc_shortcut": ([wide, wide, wide, wide, wide, wide], ctypes.c_long),
            "dc_setting": ([pointer, integer], ctypes.c_long),
            "dc_show": ([pointer, wide, wide], ctypes.c_long),
            "dc_remove": ([pointer, wide], ctypes.c_long),
            "dc_count": ([pointer, integer], ctypes.c_long),
            "dc_close": ([pointer], ctypes.c_long),
            "dc_silence": ([pointer], None),
            "dc_error": ([], wide),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._dll, name)
            function.argtypes, function.restype = arguments, result

    def open(self):
        self._assert_owner(require_open=False)
        if not self._context:
            self._check(self._dll.dc_open(self._identity, self.activator, self._callback, ctypes.byref(self._context)))

    def _check(self, result: int):
        if result < 0:
            raise NativeNotificationError(f"WinRT 0x{result & 0xffffffff:08X}: {self._dll.dc_error()}",
                                          hresult=result & 0xffffffff)

    def _assert_owner(self, require_open=True):
        if threading.get_ident() != self._owner:
            raise NativeNotificationError("Notification transport accessed outside its MTA thread")
        if require_open and not self._context:
            raise NativeNotificationError("Notification transport is closed")

    def shortcut(self, path: Path, executable: Path, arguments: str, icon: Path, identity: str):
        self._assert_owner(require_open=False)
        self._check(self._dll.dc_shortcut(str(path), str(executable), arguments, str(icon), identity, self.activator))

    def setting(self) -> int:
        self._assert_owner()
        value = ctypes.c_int()
        self._check(self._dll.dc_setting(self._context, ctypes.byref(value)))
        return value.value

    def show(self, tag: str, xml: str):
        self._assert_owner()
        self._check(self._dll.dc_show(self._context, tag, xml))

    def history_count(self) -> int:
        self._assert_owner()
        value = ctypes.c_int()
        self._check(self._dll.dc_count(self._context, ctypes.byref(value)))
        return value.value

    def remove(self, tag: str):
        self._assert_owner()
        self._check(self._dll.dc_remove(self._context, tag))

    def close(self):
        if not self._context:
            return
        self._assert_owner()
        context, self._context = self._context, ctypes.c_void_p()
        self._check(self._dll.dc_close(context))

    def silence(self):
        # Caller coordinates lifetime with the MTA owner. This exported gate
        # uses a C++ mutex only and deliberately makes no COM/Tk calls.
        if self._context:
            self._dll.dc_silence(self._context)
