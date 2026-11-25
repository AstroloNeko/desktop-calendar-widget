from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop_calendar.remote_presentation import RemoteTrayConfig
from desktop_calendar.remote_service import RemoteCompletion, RemoteStatus
from tray_badge import BADGE_COLORS, overlay_dot
from tray_icon import NOTIFYICONDATAW, TrayIcon
import test_remote_service as fixture


class TrayBadgePresentationTests(unittest.TestCase):
    def test_connection_colors_and_processing_preserves_badge(self):
        configured = RemoteTrayConfig(True, True, True, True, True)
        for connection, color in (
                ('live', 'green'), ('verifying', 'yellow'), ('connecting', 'yellow'), ('reconnecting', 'yellow'),
                ('backup', 'yellow'), ('invalid', 'red'), ('mismatch', 'red'), ('unavailable', 'red'), ('disabled', 'gray')):
            with self.subTest(connection=connection):
                status = configured.tray_status(RemoteStatus(connection))
                self.assertEqual(status[0], color)
                self.assertEqual(configured.tray_status(RemoteStatus(connection, processing_id='in-flight')), status)
        self.assertEqual(configured.tray_status(RemoteStatus('live', request_failed=True))[0], 'red')
        self.assertIn('绑定账号不符，请重新绑定', configured.tray_status(RemoteStatus('mismatch'))[1])

    def test_model_configuration_and_saved_binding_precedence(self):
        for config, color in (
                (RemoteTrayConfig(), None),
                (RemoteTrayConfig(configured=True), 'gray'),
                (RemoteTrayConfig(True, True, True, False, True), 'red'),
                (RemoteTrayConfig(True, True, True, True, False), 'red')):
            self.assertEqual(config.tray_status(RemoteStatus('live'))[0], color)
        configured = RemoteTrayConfig(True, True, True, True, True)
        status = RemoteStatus('live', completion=RemoteCompletion(1795500000, '处理失败', True))
        badge, tooltip = configured.tray_status(status)
        self.assertEqual(badge, 'red')
        self.assertIn('最近一次模型请求失败', tooltip)
        self.assertIn('最近处理', tooltip)

    def test_successful_check_adds_tooltip_time_without_changing_color_or_completion(self):
        configured = RemoteTrayConfig(True, True, True, True, True)
        completion = RemoteCompletion(1795500000, '已创建 1 项', False)
        badge, tooltip = configured.tray_status(RemoteStatus(
            'live', completion=completion, last_checked_at=1795500010, check_result='empty'))
        self.assertEqual(badge, 'green')
        self.assertIn('最近成功检查', tooltip)
        self.assertIn('已创建 1 项', tooltip)

    def test_dot_preserves_artwork_and_uses_white_outline_at_small_sizes(self):
        for size in (16, 20, 24, 32, 48):
            for color in BADGE_COLORS.values():
                with self.subTest(size=size, color=color):
                    pixels = [0xFF0F172A] * size ** 2
                    overlay_dot(pixels, size, color)
                    self.assertTrue(all(pixel == 0xFF0F172A for pixel in pixels[:size * (size // 2)]))
                    self.assertTrue((0xFF000000 | color) in pixels)
                    self.assertTrue(0xFFFFFFFF in pixels)


class TrayHistorySettingTests(unittest.TestCase):
    setUp = fixture.RemoteServiceTests.setUp
    worker_for_steps = fixture.RemoteServiceTests.worker_for_steps

    def test_previous_binding_leaves_sticky_config_flag_after_clear(self):
        config = self.configuration.load()
        self.assertFalse(config['remote_was_configured'])
        self.configuration.save({**config, **fixture.CONFIG})
        self.runtime.clear_remote_binding()
        saved = self.configuration.load()
        self.assertEqual(saved['remote_url'], '')
        self.assertTrue(saved['remote_was_configured'])
        self.assertEqual(self.runtime.remote_tray_status()[0], 'gray')


class TrayBadgeUpdateTests(unittest.TestCase):
    def test_coalesced_updates_modify_icon_only_for_color_changes(self):
        tray = TrayIcon('桌面月历', Path('calendar.ico'), Mock())
        tray._api = SimpleNamespace(user=Mock(), shell=Mock())
        tray._thread = Mock()
        tray._hwnd = 51
        tray._available.set()
        tray._nid = NOTIFYICONDATAW()
        tray._nid.hWnd, tray._nid.uID, tray._nid.hIcon = 51, 1, 10
        tray._badge_renderer = Mock()
        tray._badge_renderer.create.side_effect = (20, 21)
        tray._icon = 10
        payloads = []

        def notify(message, data):
            payloads.append((message, data._obj.uFlags, data._obj.hIcon, data._obj.szTip))
            return True

        tray._api.shell.Shell_NotifyIconW.side_effect = notify
        tray.update_status('green', '实时在线')
        tray.update_status('green', '实时在线')
        tray.update_status('green', '实时在线\n最近处理：15:32')
        tray._api.user.PostMessageW.assert_called_once_with(51, tray.STATUS_UPDATE, 0, 0)
        tray._handle_message(51, tray.STATUS_UPDATE, 0, 0)
        tray.update_status('green', '实时在线\n最近处理：15:33')
        tray._apply_status()
        tray.update_status('yellow', '正在重新连接')
        tray._apply_status()
        tray.update_status('green', '实时在线')
        tray._apply_status()
        tray.update_status(None, '')
        tray._apply_status()
        self.assertEqual([item[2] for item in payloads], [20, 20, 21, 20, 10])
        self.assertEqual(payloads[1][1], tray.NIF_TIP | tray.NIF_SHOWTIP)
        self.assertEqual(tray._badge_renderer.create.call_count, 2)
        self.assertTrue(all(item[0] == tray.NIM_MODIFY for item in payloads))

    def test_shutdown_releases_base_and_composited_icons(self):
        tray = TrayIcon('桌面月历', Path('calendar.ico'), Mock())
        api = SimpleNamespace(user=Mock(), shell=Mock(), kernel=Mock())
        api.kernel.GetModuleHandleW.return_value = 1
        api.user.CreateWindowExW.return_value = 11
        api.user.LoadImageW.return_value = 12
        api.user.GetSystemMetrics.return_value = 16
        api.user.GetMessageW.return_value = 0
        tray.update_status('green', '实时在线')
        with patch('tray_icon.WindowsTrayAPI', return_value=api), patch('tray_icon.GlobalHotkeys'):
            with patch('tray_icon.TrayBadgeRenderer') as renderer:
                renderer.return_value.create.return_value = 13
                tray._run()
        self.assertEqual([call.args[0] for call in api.user.DestroyIcon.call_args_list], [12, 13])
        self.assertFalse(tray._badge_icons)
