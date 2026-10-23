"""Transport contract tests: no system registration or real notification sends."""
import ctypes
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from native_notifications import NativeNotificationError, WinRTTransport, toast_xml


class Function:
    def __init__(self, result=0, action=None):
        self.result, self.action, self.calls = result, action, []

    def __call__(self, *args):
        self.calls.append(args)
        if self.action:
            self.action(*args)
        return self.result


class Library:
    def __init__(self):
        def opened(identity, activator, callback, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
            self.callback = callback
        self.dc_open = Function(action=opened)
        self.dc_shortcut = Function()
        self.dc_setting = Function(action=lambda handle, value: setattr(value._obj, "value", 0))
        self.dc_count = Function(action=lambda handle, value: setattr(value._obj, "value", 1))
        self.dc_show = Function()
        self.dc_remove = Function()
        self.dc_close = Function()
        self.dc_silence = Function()
        self.dc_error = Function("synthetic failure")


class NativeNotificationTests(unittest.TestCase):
    def transport(self, identity="DesktopCalendar.IsolatedTest"):
        library = Library()
        with patch("native_notifications.ctypes.WinDLL", return_value=library):
            transport = WinRTTransport(Path("test-only.dll"), identity)
        return transport, library

    def test_create_does_not_register_or_open(self):
        transport, library = self.transport()
        self.assertFalse(library.dc_open.calls)
        self.assertFalse(library.dc_shortcut.calls)
        self.assertFalse(library.dc_show.calls)
        transport.close()

    def test_identity_has_stable_isolated_activator(self):
        first, _ = self.transport()
        same, _ = self.transport()
        other, _ = self.transport("DesktopCalendar.AnotherTest")
        self.assertEqual(first.activator, same.activator)
        self.assertNotEqual(first.activator, other.activator)

    def test_explicit_open_only_once(self):
        transport, library = self.transport()
        transport.open()
        transport.open()
        self.assertEqual(len(library.dc_open.calls), 1)
        self.assertEqual(library.dc_open.argtypes[-1], ctypes.POINTER(ctypes.c_void_p))
        transport.close()

    def test_shortcut_can_be_registered_before_open(self):
        transport, library = self.transport()
        transport.shortcut(Path("unique.lnk"), Path("test.exe"), "", Path("test.ico"), "QA")
        self.assertEqual(len(library.dc_shortcut.calls), 1)
        self.assertFalse(library.dc_open.calls)

    def test_closed_send_is_rejected(self):
        transport, library = self.transport()
        with self.assertRaises(NativeNotificationError):
            transport.show("tag", "xml")
        self.assertFalse(library.dc_show.calls)

    def test_close_is_idempotent(self):
        transport, library = self.transport()
        transport.open()
        transport.close()
        transport.close()
        self.assertEqual(len(library.dc_close.calls), 1)

    def test_failure_reports_hresult_without_success(self):
        transport, library = self.transport()
        library.dc_open.result = -2147024891
        with self.assertRaisesRegex(NativeNotificationError, "80070005"):
            transport.open()

    def test_failed_send_does_not_exit_python(self):
        transport, library = self.transport()
        transport.open()
        library.dc_show.result = -2147024891
        with self.assertRaises(NativeNotificationError):
            transport.show("tag", "xml")
        self.assertEqual(transport.history_count(), 1)
        transport.close()

    def test_query_failure_does_not_claim_enabled(self):
        transport, library = self.transport()
        transport.open()
        library.dc_setting.result = -2147023728
        with self.assertRaisesRegex(NativeNotificationError, "80070490"):
            transport.setting()
        transport.close()

    def test_notification_callbacks_copy_to_queue(self):
        transport, library = self.transport()
        transport.open()
        thread = threading.Thread(target=lambda: library.callback(1, "qa:opaque-id"))
        thread.start()
        thread.join()
        self.assertEqual(transport.events.get_nowait(), (1, "qa:opaque-id"))
        with self.assertRaises(queue.Empty):
            transport.events.get_nowait()
        transport.close()

    def test_transport_operations_are_thread_affine(self):
        transport, library = self.transport()
        transport.open()
        errors = []
        def other_thread():
            for operation in (transport.setting, transport.close, transport.open):
                try:
                    operation()
                except NativeNotificationError as error:
                    errors.append(str(error))
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 3)
        self.assertFalse(library.dc_close.calls)
        transport.close()

    def test_xml_round_trips_unicode_and_literals(self):
        title, body = "中文 English 😊", '<script>& "正文"\n最后一行'
        document = ElementTree.fromstring(toast_xml(title, body, 'qa:"<>&'))
        self.assertEqual([node.text for node in document.findall(".//text")], [title, body])
        self.assertEqual(document.attrib["launch"], 'qa:"<>&')
        self.assertIsNone(document.find(".//script"))

    def test_logo_is_explicit_local_file(self):
        image = Path("assets/calendar.png")
        document = ElementTree.fromstring(toast_xml("test", "synthetic", "qa:1", image))
        self.assertEqual(document.find(".//image").attrib["src"], image.resolve().as_uri())

    def test_privacy_payload_contains_only_supplied_text(self):
        xml = toast_xml("桌面月历 · 日程提醒", "你有一项日程需要处理", "opaque-id")
        self.assertNotIn("calendar_data", xml)
        self.assertNotIn("notes", xml)


if __name__ == "__main__":
    unittest.main()
