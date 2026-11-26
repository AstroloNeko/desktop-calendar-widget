"""Opt-in real Tk/Win32 and frozen QA; temp data, mock LLM, real loopback HTTP.

Run as source or freeze this entrypoint for the exact same CalendarApp classes.
No real user profile, credentials, calendar or paid model API is used.
"""
from datetime import date
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import sys
import socket
import subprocess
import tempfile
import time
import urllib.request
from unittest.mock import Mock, patch

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from desktop_preferences import ACTIVE_THEME_NAMES
from calendar_core import Store
from desktop_calendar.assistant.calendar_chat_ui import open_ai_settings, open_chat, AISettings
from desktop_calendar.assistant.calendar_chat_http import LocalChatHTTP
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from tools.check_habit_schedules import capture_own_window
from desktop_calendar.assistant.llm_provider import AIConfiguration
from desktop_calendar.remote_cloud import Binding
from desktop_calendar.remote_history import HistoryRow
from desktop_calendar.remote_service import RemoteStatus
from win_integration import SingleInstance


def check_product(executable):
    guard = SingleInstance()
    try:
        if guard.already_running:
            raise RuntimeError('Existing calendar is running; do not stop it automatically.')
    finally:
        guard.close()
    executable = executable.resolve()
    user = ctypes.WinDLL('user32')
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    with tempfile.TemporaryDirectory(prefix='calendar-chat-product-') as folder:
        root = Path(folder)
        data = root/'DesktopCalendar/calendar_data.json'
        Store(data).save()
        config = AIConfiguration(data.parent/'ai-assistant')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config.save({**config.load(), 'http_enabled': True, 'port': port})
        token = config.load()['token']
        def request(path, body=None):
            req = urllib.request.Request(f'http://127.0.0.1:{port}'+path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.loads(response.read())
        process = subprocess.Popen([str(executable)], cwd=executable.parent, env={**os.environ, 'APPDATA': folder})
        def tray_handle():
            handles = []
            @callback_type
            def collect(hwnd, _param):
                pid, name = w.DWORD(), ctypes.create_unicode_buffer(256)
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                user.GetClassNameW(hwnd, name, 256)
                if pid.value == process.pid and name.value.startswith('DesktopCalendarTray_'):
                    handles.append(hwnd)
                return True
            user.EnumWindows(collect, 0)
            return handles[0] if handles else None
        try:
            deadline = time.monotonic()+20
            while True:
                try:
                    assert request('/v1/health')['status'] == 'ok'
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError('Normal entry EXE did not start the isolated API')
                    time.sleep(.1)
            sid = request('/v1/sessions', {})['session_id']
            response = request('/v1/chat/messages', {'session_id': sid, 'message': '今天有什么安排', 'request_id': 'normal-exe-no-key-test'})
            assert response['error_code'] == 'model_unavailable'
            assert request('/v1/sessions/'+sid+'/messages')['messages']
            assert json.loads(data.read_bytes())['events'] == []
            helper = tray_handle()
            assert helper
            assert user.PostMessageW(helper, 0x111, 1005, 0)
            process.wait(15)
            assert process.returncode == 0
            try:
                request('/v1/health')
            except OSError:
                pass
            else:
                raise AssertionError('HTTP remained live after application exit')
            return dict(normal_entry=True, api_startup='passed', missing_key='safe refusal',
                        data_isolated=True, http_exit='passed')
        finally:
            if process.poll() is None:
                helper = tray_handle()
                if helper:
                    user.PostMessageW(helper, 0x111, 1005, 0)
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    process.terminate()  # Only this explicitly launched isolated test child.
                    process.wait(5)


class MockProvider:
    def complete(self, messages, tools):
        return {'tool_calls': [{'id': 'qa-read', 'type': 'function', 'function': {'name': 'list_categories', 'arguments': '{}'}},
                              {'id': 'qa-tool', 'type': 'function', 'function': {'name': 'preview_create',
            'arguments': json.dumps({'title': '隔离AI测试事项', 'date': date.today().isoformat(), 'event_type': 'ddl'})}}]}


class InvalidBatchProvider:
    def complete(self, messages, tools):
        return {'tool_calls': [{'id': 'invalid-'+str(index), 'type': 'function', 'function': {'name': 'preview_create',
            'arguments': json.dumps({'title': '不应创建', 'date': date.today().isoformat()})}} for index in range(2)]}


class HabitProvider:
    def complete(self, messages, tools):
        return {'tool_calls': [{'id': 'qa-habit', 'type': 'function', 'function': {
            'name': 'preview_create_habit', 'arguments': json.dumps(dict(title='隔离组会习惯', frequency='weekly', weekdays=[0]))}}]}


class HabitEditProvider:
    def complete(self, messages, tools):
        state = json.loads(messages[0]['content'].split('\n', 1)[1])
        return {'tool_calls': [{'id': 'qa-habit-edit', 'type': 'function', 'function': {
            'name': 'preview_update_habit', 'arguments': json.dumps(dict(habit_id=state['selected_object']['id'],
                changes=dict(frequency='weekly', weekdays=[1, 3], color='#ffffff')))}}]}


def wait(app, condition, message, timeout=12):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        pump(app, .03)
        if condition():
            return
    raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--product-exe', type=Path)
    parser.add_argument('--sqlite', action='store_true', help='Use production SQLite startup factory in a temporary profile')
    parser.add_argument('--themes', nargs='+', choices=ACTIVE_THEME_NAMES,
                        default=ACTIVE_THEME_NAMES)
    args = parser.parse_args()
    if args.product_exe:
        result = check_product(args.product_exe)
        if args.report:
            args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
        return
    errors, cases = [], []
    with tempfile.TemporaryDirectory(prefix='calendar-ai-qa-') as folder:
        if args.sqlite:
            from desktop_storage_startup import StartupOptions, create_desktop_app
            fixture = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
            (Path(folder)/'calendar_data.json').write_text(json.dumps(fixture), encoding='utf8')
        else:
            store = Store(Path(folder)/'calendar.json')
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = (create_desktop_app(StartupOptions(Path(folder), True), CalendarApp, confirm=lambda: True)
                   if args.sqlite else CalendarApp(store))
            store = app.store
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                pump(app, .2)
                direct = open_ai_settings(app)
                pump(app, .08)
                assert direct.tabs.select() == str(direct.remote_tab)
                assert app.chat_runtime.window is None
                assert app.chat_runtime.core.remote is None
                open_ai_settings(app)
                assert app._active_modal_window() is direct
                direct.close()
                for theme in args.themes:
                    app.set_theme(theme)
                    for view in ('compact', 'global'):
                        (app.enter_global_view if view == 'global' else app.return_to_compact_view)()
                        open_chat(app)
                        runtime, chat = app.chat_runtime, app.chat_runtime.window
                        runtime.service.provider = MockProvider()
                        wait(app, lambda: not chat.busy, 'session creation')
                        assert app.grab_current() is chat
                        assert chat.send_button.winfo_rooty()+chat.send_button.winfo_height() <= chat.winfo_rooty()+chat.winfo_height()
                        settings = AISettings(chat)
                        pump(app, .08)
                        assert app.grab_current() is settings
                        assert settings.inputs['api_key'].get() == ''
                        assert not settings.consent.get()
                        assert settings.title() == 'AI 设置与隐私'
                        assert tuple(settings.tabs.tab(page, 'text') for page in settings.tabs.tabs()) == ('远程模型', '本机 API', '远程服务')
                        assert settings.tabs.select() == str(settings.ai_tab)
                        assert settings.fields['model'].winfo_ismapped()
                        assert str(settings.fields['port']).startswith(str(settings.api_tab) + '.')
                        settings.tabs.select(settings.api_tab)
                        pump(app, .04)
                        assert settings.fields['port'].winfo_ismapped()
                        assert settings.copy_button.winfo_ismapped()
                        settings.inputs['model'].set('provisional-model')
                        settings.inputs['port'].set('22000')
                        settings.tabs.select(settings.remote_tab)
                        pump(app, .06)
                        settings.tabs.select(settings.ai_tab)
                        pump(app, .04)
                        assert settings.inputs['model'].get() == 'provisional-model'
                        assert settings.inputs['port'].get() == '22000'
                        settings.tabs.select(settings.remote_tab)
                        pump(app, .04)
                        assert runtime.core.remote is None
                        assert not settings.remote_enabled.get()
                        assert settings.inputs['remote_url'].get() == ''
                        assert settings.fields['remote_token'].cget('show') == '*'
                        assert '已关闭' in settings.remote_status.get()
                        assert settings.binding_state.get() == '尚未绑定'
                        assert settings.last_check_time.get() == settings.check_result.get() == '暂无'
                        assert settings.safety_check.get() == '关闭'
                        assert settings.manual_check_button.cget('state') == 'disabled'
                        assert runtime.gateway.writable()
                        settings.remote_mode.set('auto')
                        settings.show_remote_mode()
                        pump(app, .04)
                        assert settings.auto_remote.winfo_ismapped()
                        assert settings.fields['activation_code'].cget('show') == '*'
                        assert settings.fields['activation_code'].cget('state') == 'normal'
                        assert settings.bind_button.cget('text') == '绑定'
                        assert settings.bind_button.cget('state') == 'normal'
                        if theme == 'modern' and view == 'compact':
                            saved = settings.old
                            worker = Mock()
                            worker.snapshot.return_value = RemoteStatus('live')

                            def request_check():
                                worker.snapshot.return_value = RemoteStatus('live', check_result='checking')
                                return True

                            worker.request_check.side_effect = request_check
                            with patch.object(runtime.core, 'remote', worker):
                                settings.old = {**saved, 'remote_enabled': True}
                                wait(app, lambda: settings.manual_check_button.cget('state') == 'normal',
                                     'manual remote check available')
                                settings.manual_check_button.invoke()
                                worker.request_check.assert_called_once()
                                assert settings.check_result.get() == '正在检查……'
                                assert settings.manual_check_button.cget('state') == 'disabled'
                            settings.old = saved
                            settings.inputs['remote_url'].set('https://example.invalid')
                            settings.inputs['activation_code'].set('synthetic-code')
                            save_binding = runtime.core.save_remote_binding
                            binding = Binding('synthetic-user', 'synthetic-worker-token')
                            with patch.object(runtime.core, 'request_remote_binding', return_value=binding) as activate, \
                                    patch.object(runtime.core, 'save_remote_binding', side_effect=OSError('fixture')):
                                settings.bind_button.invoke()
                                settings.close()
                                assert settings.winfo_exists()
                                wait(app, lambda: settings.pending_binding is not None, 'activation local save failure')
                                assert settings.bind_button.cget('text') == '重试本地保存'
                                activate.assert_called_once_with('https://example.invalid', 'synthetic-code')
                            with patch.object(runtime.core, 'request_remote_binding') as activate, \
                                    patch.object(runtime.core, 'save_remote_binding', wraps=save_binding):
                                settings.bind_button.invoke()
                                wait(app, lambda: '已保存绑定' in settings.status.get(), 'save activated credential')
                                activate.assert_not_called()
                            assert settings.binding_state.get() == '已绑定到本机'
                            assert not settings.auto_remote.winfo_ismapped()
                            assert not settings.remote_enabled.get()
                            assert settings.inputs['activation_code'].get() == ''
                            bound = settings.old
                            timestamp = time.time()
                            rows = (
                                HistoryRow(timestamp, timestamp, '已创建 1 项', '回执已上传', '通知已发送', channel='qq'),
                                HistoryRow(timestamp - 120, timestamp - 119, '查询完成', '回执已上传', '通知已发送', channel='wechat'),
                                HistoryRow(timestamp - 240, timestamp - 237, '当时等待确认', '回执已上传', '通知失败'),
                                HistoryRow(timestamp - 360, timestamp - 359, '已修改 1 项', '回执待上传', ''),
                            )
                            with patch.object(runtime.core, 'remote', worker), \
                                    patch.object(runtime.core, 'recent_remote_history', return_value=rows):
                                settings.old = {**bound, 'remote_enabled': True, 'api_key': 'synthetic-model-key', 'consent': True}
                                settings.remote_enabled.set(True)
                                worker.snapshot.return_value = RemoteStatus('live', history_revision=10,
                                    last_checked_at=timestamp, check_result='empty', account_name='演示日历账号')
                                settings.refresh_remote_panel()
                                wait(app, lambda: '当时等待确认' in settings.history_view.get('1.0', 'end'), 'recent history visible')
                                settings.refresh_remote_panel()
                                assert settings.remote_status.get() == '实时在线'
                                assert settings.check_result.get() == '无事发生，一切正常'
                                assert settings.account_name.get() == '演示日历账号'
                                assert all(name in settings.history_view.get('1.0', 'end') for name in ('QQ', '微信', '未知渠道'))
                                assert settings.last_check_time.get() != '暂无'
                                assert '已创建 1 项' in settings.latest_result.get()
                                assert '无事发生' not in settings.history_view.get('1.0', 'end')
                                if not getattr(sys, 'frozen', False):
                                    destination = Path(__file__).resolve().parents[1]/'build/calendar-chat-qa'
                                    destination.mkdir(parents=True, exist_ok=True)
                                    settings.status.set('')
                                    pump(app, .08)
                                    capture_own_window(settings, destination/'remote-settings.png')
                                last_success = settings.last_check_time.get()
                                worker.snapshot.return_value = RemoteStatus('live', history_revision=10, request_failed=True,
                                                                           last_checked_at=timestamp, check_result='failed')
                                settings.refresh_remote_panel()
                                assert settings.check_result.get() == '本次检查失败'
                                assert settings.last_check_time.get() == last_success
                                assert '已创建 1 项' in settings.latest_result.get()
                                worker.snapshot.return_value = RemoteStatus('verifying', history_revision=10)
                                settings.refresh_remote_panel()
                                assert settings.remote_status.get() == '正在核验绑定账号'
                                assert settings.manual_check_button.cget('state') == 'disabled'
                                worker.snapshot.return_value = RemoteStatus('mismatch', history_revision=10)
                                settings.refresh_remote_panel()
                                assert settings.binding_state.get() == '账号不符'
                                assert settings.manual_check_button.cget('state') == 'disabled'
                                assert settings.rebind_button.cget('state') == 'normal'
                                assert settings.copy_rebinding_control.winfo_manager()
                                worker.snapshot.return_value = RemoteStatus('invalid', history_revision=10)
                                settings.refresh_remote_panel()
                                assert '已失效' in settings.remote_status.get()
                                assert settings.binding_state.get() == '已失效'
                                assert settings.manual_check_button.cget('state') == 'disabled'
                                settings.copy_rebinding_button.invoke()
                                assert settings.clipboard_get() == '重新获取激活码'
                                settings.inputs['remote_url'].set('https://unsaved.invalid')
                                settings.rebind_button.invoke()
                                assert settings.inputs['remote_url'].get() == bound['remote_url']
                                assert not settings.inputs['activation_code'].get()
                                pump(app, .1)
                                assert settings.auto_remote.winfo_ismapped()
                                if not getattr(sys, 'frozen', False):
                                    capture_own_window(settings, destination/'remote-rebind.png')
                            settings.old = bound
                            confirm = 'desktop_calendar.assistant.calendar_chat_ui.messagebox.askyesno'
                            with patch(confirm, side_effect=(False, True, True)):
                                settings.clear_remote_binding()
                                assert runtime.configuration.load()['remote_account'] == 'synthetic-user'
                                runtime.configuration.save({**runtime.configuration.load(), 'api_key': 'synthetic-model-key'})
                                settings.clear_ai_settings()
                                assert runtime.configuration.load()['api_key'] == ''
                                assert runtime.configuration.load()['remote_account'] == 'synthetic-user'
                                settings.clear_remote_binding()
                                assert settings.inputs['remote_url'].get() == ''
                                assert runtime.configuration.load()['remote_token'] == ''
                        settings.remote_mode.set('manual')
                        settings.show_remote_mode()
                        settings.advanced_toggle.invoke()
                        pump(app, .04)
                        assert settings.manual_remote.winfo_ismapped()
                        with patch('desktop_calendar.assistant.calendar_chat_ui.test_connection', return_value=True) as health:
                            settings.inputs['remote_url'].set('https://example.invalid')
                            settings.check_remote_connection()
                            wait(app, lambda: '本次检查未验证' in settings.status.get(), 'remote health-only check')
                            health.assert_called_once_with('https://example.invalid')
                            assert runtime.core.remote is None
                        settings.close()
                        assert app.grab_current() is chat
                        before = len(store.events)
                        chat.input.insert('1.0', '新建今天DDL，先预览不要执行')
                        chat.send()
                        wait(app, lambda: not chat.busy, 'LLM preview')
                        assert len(store.events) == before and chat.operation['status'] == 'pending'
                        original_operation = chat.operation
                        runtime.service.provider = InvalidBatchProvider()
                        chat.input.insert('1.0', '继续处理单事项')
                        chat.send()
                        wait(app, lambda: not chat.busy, 'safe invalid-batch feedback')
                        assert chat.operation == original_operation
                        assert chat.confirm_button.cget('state') == 'normal'
                        assert '详细提示见对话区' in chat.status.get()
                        assert '第一阶段' not in chat.status.get()
                        runtime.service.provider = MockProvider()
                        chat.confirm()
                        wait(app, lambda: not chat.busy, 'confirmation')
                        assert len(store.events) == before+1
                        assert chat.operation['status'] == 'succeeded'
                        assert app.event_commands.can_undo
                        if theme == 'modern' and view == 'global' and not getattr(sys, 'frozen', False):
                            destination = Path(__file__).resolve().parents[1]/'build/calendar-chat-qa'
                            destination.mkdir(parents=True, exist_ok=True)
                            capture_own_window(chat, destination/'chat.png')
                        chat.close()
                        pump(app, .1)
                        assert app.grab_current() is None
                        app.open_search()
                        pump(app, .08)
                        assert app.search_window.winfo_exists()
                        app.search_window.close()
                        pump(app, .1)
                        app.undo_event()
                        assert len(store.events) == before
                        runtime.service.provider = HabitProvider()
                        open_chat(app)
                        chat = runtime.window
                        wait(app, lambda: not chat.busy and chat.session_id is not None, 'habit session ready')
                        habit_count = len(store.routines)
                        chat.input.insert('1.0', '新增习惯，每周一开组会，紫色')
                        chat.send()
                        wait(app, lambda: not chat.busy, 'habit automatic save')
                        assert len(store.routines) == habit_count+1 and len(store.events) == before
                        assert store.routines[-1].kind == 'habit' and store.routines[-1].weekdays == [0]
                        assert chat.operation['status'] == 'succeeded'
                        assert '已创建习惯' in chat.transcript.get('1.0', 'end')
                        identifier = store.routines[-1].id
                        runtime.service.provider = HabitEditProvider()
                        chat.input.insert('1.0', '改成每周二四，颜色绿色')
                        chat.send()
                        wait(app, lambda: not chat.busy, 'habit edit persisted')
                        assert store.routines[-1].id == identifier and store.routines[-1].weekdays == [1, 3]
                        assert '已更新习惯' in chat.transcript.get('1.0', 'end')
                        chat.close()
                        pump(app, .08)
                        assert app.grab_current() is None
                        app.undo_event()
                        assert store.routines[-1].weekdays == [0]
                        app.redo_event()
                        assert store.routines[-1].weekdays == [1, 3]
                        cases.append((theme, view))
                # HTTP worker requests same service; UI pumps the only Store dispatcher.
                runtime.http = LocalChatHTTP(runtime.service, 'Q'*48, port=0)
                runtime.service.provider = HabitProvider()
                runtime.http.start()
                results = []
                import threading
                def http_check():
                    def post(path, body):
                        request = urllib.request.Request(f'http://127.0.0.1:{runtime.http.port}'+path,
                            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+'Q'*48})
                        with urllib.request.urlopen(request, timeout=15) as response:
                            return json.loads(response.read())
                    try:
                        sid = post('/v1/sessions', {})['session_id']
                        response = post('/v1/chat/messages', dict(session_id=sid, message='新增习惯，每周一开组会，紫色', request_id='frozen-http-qa-request'))
                        assert response['operation_result']['object_type'] == 'habit'
                        runtime.service.provider = HabitEditProvider()
                        changed = post('/v1/chat/messages', dict(session_id=sid, message='改成每周二四，颜色绿色', request_id='frozen-http-edit-request'))
                        assert changed['operation_result']['action'] == 'updated'
                        assert changed['operation_result']['habit_id'] == response['operation_result']['habit_id']
                        preview = response['pending_operation']
                        op = preview['operation_id']
                        confirmed = post('/v1/operations/'+op+'/confirm', {'session_id': sid, 'revision': preview['revision']})
                        assert confirmed['status'] == 'succeeded'
                        results.append('passed')
                    except Exception as error:
                        results.append(str(error))
                thread = threading.Thread(target=http_check, daemon=True)
                thread.start()
                wait(app, lambda: bool(results), 'HTTP main-thread dispatch', timeout=20)
                assert results == ['passed'], results
                assert not errors, errors
                result = dict(frozen=bool(getattr(sys, 'frozen', False)), actual_dpi=dpi_for_window(app),
                    theme_view_cases=len(cases), model='mock-only', loopback_http='passed', modal_search_undo='passed', errors=errors)
            finally:
                runtime = getattr(app, 'chat_runtime', None)
                if runtime and runtime.window:
                    runtime.window.close()
                if app.search_window and app.search_window.winfo_exists():
                    app.search_window.close()
                cleanup(app)
                if runtime:
                    assert runtime.http is None and runtime.service.stopped
        if args.report:
            args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
