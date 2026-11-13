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
from unittest.mock import patch

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from calendar_chat_ui import open_chat, AISettings
from calendar_chat_http import LocalChatHTTP
from dpi_utils import dpi_for_window
from tools.check_interactions import cleanup, pump
from tools.check_habit_schedules import capture_own_window
from llm_provider import AIConfiguration
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
    args = parser.parse_args()
    if args.product_exe:
        result = check_product(args.product_exe)
        if args.report:
            args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
        return
    errors, cases = [], []
    with tempfile.TemporaryDirectory(prefix='calendar-ai-qa-') as folder:
        store = Store(Path(folder)/'calendar.json')
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                pump(app, .2)
                for theme in ('modern', 'aero', 'paper', 'frutiger'):
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
                        cases.append((theme, view))
                # HTTP worker requests same service; UI pumps the only Store dispatcher.
                runtime.http = LocalChatHTTP(runtime.service, 'Q'*48, port=0)
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
                        preview = post('/v1/chat/messages', dict(session_id=sid, message='QA', request_id='frozen-http-qa-request'))['pending_operation']
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
