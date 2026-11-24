from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from desktop_calendar.assistant.calendar_chat_protocol import fingerprint
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.remote_cloud import Binding, FirebaseNotificationContract, NotificationSession, remote_scope
from desktop_calendar.remote_notifications import NotificationListener, sse_events, stream_notifications
from desktop_calendar.remote_service import JOBS, RemoteWorker
from desktop_calendar.remote_transport import CLAIM_PATH, StatusError, Unavailable
import test_remote_service as fixture
from test_calendar_chat import tool


STREAM_URL = 'https://unit-test-default-rtdb.firebaseio.com/notices/test-account.json'
SHORT_TOKEN = 'synthetic-short-lived-id-token'


def credential():
    return NotificationSession(STREAM_URL, SHORT_TOKEN, time.monotonic() + 60)


def sse(name, payload):
    return f'event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode()


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


class SSETests(unittest.TestCase):
    def stream(self, fragments, *, status=200, content_type='text/event-stream; charset=utf-8'):
        class RequestException(Exception):
            pass

        session = Mock()
        response = Mock(status_code=status, headers={'Content-Type': content_type})
        response.iter_content.return_value = iter(fragments)
        session.get.return_value = nullcontext(response)
        module = SimpleNamespace(Session=lambda: nullcontext(session),
                                 exceptions=SimpleNamespace(RequestException=RequestException))
        return session, response, module

    def test_fragmented_sse_preserves_utf8_multiline_bom_and_line_endings(self):
        data = ('\ufeff: comment\r\nevent: put\rdata: {"path":"/",\r\n'
                'data:"data":"测试"}\n\r\n'
                'event: patch\ndata: {"path":"/","data":2}').encode()
        events = list(sse_events([data[:2], data[2:9], *[bytes([x]) for x in data[9:]]]))
        self.assertEqual(events, [(b'put', '{"path":"/",\n"data":"测试"}'.encode())])
        with self.assertRaises(Unavailable):
            list(sse_events([b'data: ' + b'x' * (70 * 1024)]))

    def test_stream_uses_short_auth_verified_tls_and_keepalive_never_wakes(self):
        for end in ('cancel', 'auth_revoked'):
            with self.subTest(end=end):
                initial = sse('put', {'path': '/', 'data': None})
                patch_event = sse('patch', {'path': '/', 'data': {'counter': 1}})
                session, response, requests = self.stream([
                    sse('keep-alive', None), initial[:3], initial[3:] + sse('keep-alive', None),
                    patch_event, sse(end, 'reason'), patch_event,
                ])
                ready, wake = Mock(), Mock()
                stream_notifications(credential(), threading.Event(), ready, wake, requests_module=requests)
                ready.assert_called_once()
                wake.assert_called_once()
                response.iter_content.assert_called_once_with(chunk_size=1)
                args = session.get.call_args
                self.assertEqual(args.args, (STREAM_URL,))
                self.assertEqual(args.kwargs['params'], {'auth': SHORT_TOKEN})
                self.assertTrue(args.kwargs['verify'])
                self.assertTrue(args.kwargs['stream'])
                self.assertFalse(args.kwargs['allow_redirects'])
                self.assertNotIn('X-Worker-Token', args.kwargs['headers'])
                self.assertFalse(session.trust_env)

    def test_bad_stream_or_requests_error_has_no_secret_diagnostic(self):
        for target in ('https://example.invalid/notice.json', 'http://x.firebaseio.com/notice.json',
                       'https://x.firebaseio.com/notice.json?auth=abc', 'https://x.firebaseio.com/.json',
                       'https://x.firebaseio.com:8443/notice.json'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                NotificationSession(target, SHORT_TOKEN, time.monotonic() + 60)
        for status, media in ((307, 'text/event-stream'), (200, 'application/json'), (401, 'text/event-stream')):
            session, response, requests = self.stream([], status=status, content_type=media)
            with self.assertRaises(Unavailable):
                stream_notifications(credential(), threading.Event(), Mock(), Mock(), requests_module=requests)
            response.iter_content.assert_not_called()
        session, _, requests = self.stream([])
        logger = logging.getLogger('urllib3.connectionpool')

        def fail(*args, **kwargs):
            logger.warning('request contained %s', SHORT_TOKEN)
            logger.warning('this simulated request also failed')
            raise requests.exceptions.RequestException(STREAM_URL + '?auth=' + SHORT_TOKEN)

        session.get.side_effect = fail
        with self.assertLogs(logger) as captured:
            with self.assertRaises(Unavailable) as error:
                stream_notifications(credential(), threading.Event(), Mock(), Mock(), requests_module=requests)
            logger.warning('another request remains visible')
        self.assertNotIn(SHORT_TOKEN, repr(error.exception))
        self.assertEqual(len(captured.records), 1)
        self.assertNotIn(SHORT_TOKEN, '\n'.join(captured.output))

    def test_rtdb_redirect_for_same_namespace_reuses_only_original_short_token(self):
        first_url = 'https://unit-test-default-rtdb.firebaseio.com/notices/test-account.json'
        delegated_url = 'https://regional-shard.firebaseio.com/notices/test-account.json'
        for suffix in ('?ns=unit-test-default-rtdb&auth=injected&alt=json', '?ns=another-account', ''):
            with self.subTest(suffix=suffix):
                session, _, requests = self.stream([sse('put', {'path': '/', 'data': 17})])
                connected = session.get.return_value
                redirect = Mock(status_code=307, headers={'Location': delegated_url + suffix})
                session.get.side_effect = [nullcontext(redirect), connected]
                ready = Mock()
                if not suffix.startswith('?ns=unit-test'):
                    with self.assertRaises(Unavailable):
                        stream_notifications(credential(), threading.Event(), ready, Mock(), requests_module=requests)
                    session.get.assert_called_once()
                    continue
                stream_notifications(credential(), threading.Event(), ready, Mock(), requests_module=requests)
                ready.assert_called_once()
                start, routed = session.get.call_args_list
                self.assertEqual(start.args, (first_url,))
                self.assertEqual(routed.args, (delegated_url,))
                self.assertEqual(routed.kwargs['params'], {'auth': SHORT_TOKEN, 'ns': 'unit-test-default-rtdb'})
                self.assertFalse(routed.kwargs['allow_redirects'])

    def test_rtdb_redirect_rejects_foreign_host_or_path(self):
        for target in (
                'https://example.invalid/notices/test-account.json?ns=unit-test-default-rtdb',
                'https://regional.firebaseio.com/notices/not-my-account.json?ns=unit-test-default-rtdb'):
            session, response, requests = self.stream([], status=307)
            response.headers = {'Location': target}
            with self.subTest(target=target), self.assertRaises(Unavailable):
                stream_notifications(credential(), threading.Event(), Mock(), Mock(), requests_module=requests)
            session.get.assert_called_once()

    def test_stream_ends_at_recommended_refresh_before_token_expires(self):
        now = 100
        credentials = NotificationSession(STREAM_URL, SHORT_TOKEN, expires_at=200, refresh_at=150)

        def fragments():
            nonlocal now
            yield sse('put', {'path': '/', 'data': 1})
            now = 150
            yield sse('put', {'path': '/', 'data': 2})

        session, _, requests = self.stream(fragments())
        ready, wake = Mock(), Mock()
        with patch('desktop_calendar.remote_notifications.time.monotonic', side_effect=lambda: now):
            self.assertTrue(stream_notifications(credentials, threading.Event(), ready, wake, requests_module=requests))
        ready.assert_called_once()
        wake.assert_not_called()
        self.assertEqual(session.get.call_args.kwargs['timeout'], (10, 45))

    def test_stop_and_expiry_do_not_admit_stream_requests_or_late_events(self):
        session, response, requests = self.stream([])
        stopped = threading.Event()
        stopped.set()
        stream_notifications(credential(), stopped, Mock(), Mock(), requests_module=requests)
        expired = NotificationSession(STREAM_URL, SHORT_TOKEN, time.monotonic() - 1)
        stream_notifications(expired, threading.Event(), Mock(), Mock(), requests_module=requests)
        session.get.assert_not_called()
        def chunks():
            yield sse('put', {'path': '/', 'data': 1})
            stopped.set()
            yield sse('patch', {'path': '/', 'data': 2})
        stopped.clear()
        response.iter_content.return_value = chunks()
        ready, wake = Mock(), Mock()
        stream_notifications(credential(), stopped, ready, wake, requests_module=requests)
        ready.assert_called_once()
        wake.assert_not_called()

    def test_listener_refetches_credentials_and_reuses_thread_on_reconfigure(self):
        connection = []
        old_blocked = threading.Event()
        release_old = threading.Event()
        changed = Mock()
        contract = Mock()
        contract.notification_session.side_effect = lambda client, token, account: credential()

        def stream(credentials, stop, ready, wake):
            connection.append(stop)
            ready()
            if len(connection) == 1:
                raise Unavailable
            if len(connection) == 2:
                old_blocked.set()
                release_old.wait(3)
                wake()  # stale update on the old stream
            stop.wait(3)

        listener = NotificationListener(contract, lambda url: url, changed, stream=stream, retry=(0.01, 0.03))
        listener.configure(1, fixture.URL, fixture.CONFIG['remote_token'])
        thread = listener.thread
        try:
            self.assertTrue(old_blocked.wait(3))
            self.assertIn(call(1, False), changed.call_args_list)
            listener.configure(2, 'https://next.invalid', 'replacement-worker-token', 'next-account')
            self.assertIs(listener.thread, thread)
            self.assertTrue(connection[1].is_set())
            release_old.set()
            self.assertTrue(until(lambda: changed.call_args == call(2, True)))
            self.assertEqual(changed.call_args_list.count(call(1, True)), 2)
            self.assertEqual(contract.notification_session.call_args.args,
                             ('https://next.invalid', 'replacement-worker-token', 'next-account'))
        finally:
            release_old.set()
            listener.stop()
            thread.join(3)
        self.assertFalse(thread.is_alive())


class PairingAndScopeTests(unittest.TestCase):
    setUp = fixture.RemoteServiceTests.setUp
    worker_for_steps = fixture.RemoteServiceTests.worker_for_steps

    def test_deployed_activation_persists_user_id(self):
        self.assertIsInstance(self.runtime.remote_contract, FirebaseNotificationContract)
        with patch('desktop_calendar.remote_cloud.Client') as transport:
            transport.return_value.request.return_value = {'user_id': 'user-one', 'worker_token': fixture.CONFIG['remote_token']}
            result = self.runtime.request_remote_binding(fixture.URL, 'synthetic-code')
            args = transport.return_value.request.call_args
            self.assertEqual(args.args, ('/api/pairing/activate',))
            self.assertEqual(json.loads(args.kwargs['body']), {'activation_code': 'synthetic-code'})
            self.runtime.save_remote_binding(fixture.URL, result)
            saved = self.configuration.load()
            self.assertFalse(saved['remote_enabled'])
            self.assertEqual(saved['remote_account'], 'user-one')
            self.assertEqual(saved['remote_token'], fixture.CONFIG['remote_token'])
            self.assertNotIn('activation_code', saved)
            self.assertNotIn(fixture.CONFIG['remote_token'].encode(), self.configuration.path.read_bytes())
            self.assertNotIn(fixture.CONFIG['remote_token'], repr(result))
            with self.assertRaises(ValueError):
                self.runtime.save_remote_binding(fixture.URL, Binding('', 'synthetic-token'))

    def test_activation_posts_json_without_worker_auth_and_accepts_code_formatting(self):
        document = b'{"user_id":"user-one","worker_token":"new-synthetic-worker-token"}'
        response = Mock(status=200)
        response.read.return_value = document
        with patch('desktop_calendar.remote_transport.build_opener') as build:
            opener = build.return_value
            opener.open.return_value = nullcontext(response)
            result = self.runtime.request_remote_binding(fixture.URL, '  aBc -123  ')
            self.assertEqual(result.account_id, 'user-one')
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.full_url, fixture.URL + '/api/pairing/activate')
            self.assertEqual(request.get_header('Content-type'), 'application/json')
            self.assertIsNone(request.get_header('X-worker-token'))
            self.assertEqual(json.loads(request.data), {'activation_code': 'aBc -123'})
            for code in ('', ' -- \t ', None, 'a' * 257):
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, '有效的客户端激活码'):
                    self.runtime.request_remote_binding(fixture.URL, code)
            opener.open.assert_called_once()

    def test_activation_http_error_and_malformed_reply_do_not_replace_config(self):
        self.configuration.save({**self.configuration.load(), **fixture.CONFIG})
        before = self.configuration.load()
        cases = (
            (StatusError(400), '无效、已使用或已过期'),
            (StatusError(403), '账户已停用'),
            (StatusError(429), '次数过多'),
            (StatusError(503), 'HTTP 503'),
            (Unavailable(), '可能已使用激活码'),
            ({'account_id': 'draft-field', 'worker_token': 'new-token'}, '响应格式无效'),
            ({'user_id': 'user-one'}, '响应格式无效'),
            ({'user_id': 'user-one', 'worker_token': 'invalid token'}, '响应格式无效'),
        )
        with patch('desktop_calendar.remote_cloud.Client') as transport:
            for response, expected in cases:
                request = transport.return_value.request
                request.reset_mock()
                request.side_effect = response if isinstance(response, Exception) else None
                request.return_value = response
                with self.assertRaisesRegex(ValueError, expected):
                    self.runtime.request_remote_binding(fixture.URL, 'synthetic-code')
                request.assert_called_once()
                self.assertEqual(self.configuration.load(), before)

    def test_successful_activation_can_persist_after_runtime_stops(self):
        self.runtime.stop()
        self.runtime.save_remote_binding(fixture.URL, Binding('user-one', 'synthetic-rotation'))
        saved = self.configuration.load()
        self.assertEqual(saved['remote_token'], 'synthetic-rotation')
        self.assertFalse(saved['remote_enabled'])
        with self.assertRaisesRegex(ValueError, '已停止'):
            self.runtime.request_remote_binding(fixture.URL, 'synthetic-code')

    def test_clear_binding_stops_remote_and_preserves_other_settings_and_ledger(self):
        self.configuration.save({**self.configuration.load(), **fixture.CONFIG, 'remote_account': 'user-one',
                                 'model': 'custom-model', 'api_key': 'synthetic-model-secret',
                                 'consent': True, 'http_enabled': True, 'token': 'synthetic-local-token'})
        self.runtime.remote = Mock()
        ledger_scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'user-one')
        row = self.worker._intake(fixture.job('keep-me', '明天记录测试日程'), ledger_scope, fixture.URL)
        session = self.service.new_session()['session_id']
        self.runtime.clear_remote_binding()
        saved = self.configuration.load()
        self.assertEqual({k: saved[k] for k in ('remote_url', 'remote_token', 'remote_account', 'remote_enabled')},
                         dict(remote_url='', remote_token='', remote_account='', remote_enabled=False))
        self.assertEqual(saved['model'], 'custom-model')
        self.assertEqual(saved['api_key'], 'synthetic-model-secret')
        self.assertTrue(saved['consent'])
        self.assertTrue(saved['http_enabled'])
        self.assertEqual(saved['token'], 'synthetic-local-token')
        self.runtime.remote.configure.assert_called_once_with(saved)
        self.assertEqual(self.ledger.get(JOBS, row['id'], ledger_scope), row)
        self.assertEqual(self.service.get_session(session)['session_id'], session)

    def test_clear_ai_rotates_loopback_token_and_preserves_remote_binding(self):
        self.configuration.save({**self.configuration.load(), **fixture.CONFIG, 'remote_account': 'user-one',
                                 'endpoint': 'https://model.example.invalid', 'model': 'custom-model',
                                 'api_key': 'synthetic-model-secret', 'timeout': 15,
                                 'consent': True, 'http_enabled': True, 'port': 22000, 'token': 'synthetic-local-token'})
        self.runtime.remote = Mock()
        local_http = self.runtime.http = Mock()
        session = self.service.new_session()['session_id']
        saved = self.runtime.clear_ai_settings()
        defaults = self.configuration.defaults()
        for field in ('endpoint', 'model', 'api_key', 'timeout', 'temperature', 'consent', 'http_enabled', 'port'):
            self.assertEqual(saved[field], defaults[field])
        self.assertNotEqual(saved['token'], 'synthetic-local-token')
        self.assertEqual(saved['remote_url'], fixture.URL)
        self.assertEqual(saved['remote_token'], fixture.CONFIG['remote_token'])
        self.assertEqual(saved['remote_account'], 'user-one')
        self.assertFalse(saved['remote_enabled'])
        local_http.stop.assert_called_once()
        self.assertIsNone(self.runtime.http)
        self.assertFalse(self.runtime.remote.configure.call_args.args[0]['remote_enabled'])
        self.assertEqual(self.service.get_session(session)['session_id'], session)

    def test_b3_pinned_credential_preserves_original_namespace(self):
        legacy = {**self.configuration.load(), **fixture.CONFIG}
        legacy.pop('remote_legacy_credential')
        legacy.pop('remote_account')
        legacy.pop('remote_safety_check_minutes')
        self.configuration.path.write_bytes(self.protection.encrypt(json.dumps(legacy).encode()))
        migrated = self.configuration.load()
        self.assertEqual(migrated['remote_safety_check_minutes'], 0)
        pinned = migrated['remote_legacy_credential']
        old = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], legacy_credential=pinned)
        self.assertEqual(old, fingerprint(fixture.URL))
        replacement = {**self.configuration.load(), 'remote_token': 'synthetic-replacement'}
        self.configuration.save(replacement)
        after = self.configuration.load()
        self.assertEqual(after['remote_legacy_credential'], pinned)
        self.assertNotEqual(remote_scope(fixture.URL, after['remote_token'], legacy_credential=pinned), old)
        bound = remote_scope(fixture.URL, 'token-one', 'bound-one')
        self.assertEqual(bound, remote_scope(fixture.URL, 'token-two', 'bound-one'))
        self.assertNotEqual(bound, remote_scope(fixture.URL, 'token-one', 'bound-two'))
        self.assertNotEqual(bound, remote_scope('https://next.invalid', 'token-one', 'bound-one'))

    def test_safety_check_choices_persist_and_reject_invalid_values(self):
        config = self.configuration.load()
        self.assertEqual(config['remote_safety_check_minutes'], 0)
        self.configuration.save({**config, 'remote_safety_check_minutes': 30})
        self.assertEqual(self.configuration.load()['remote_safety_check_minutes'], 30)
        for minutes in (True, '30', 5):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.configuration.save({**config, 'remote_safety_check_minutes': minutes})
        self.assertEqual(self.configuration.load()['remote_safety_check_minutes'], 30)

    def test_history_is_local_limited_scoped_and_uses_task_time_result(self):
        journal = ChatJournal(self.folder / 'wechat-jobs.sqlite3', self.protection)
        config = {**fixture.CONFIG, 'remote_account': 'owner-one'}
        scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'owner-one')
        other = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'owner-two')
        different_url = 'https://other.invalid'
        different_server = remote_scope(different_url, fixture.CONFIG['remote_token'], 'owner-one')
        for number in (23, *range(23)):
            journal.put(JOBS, f'test-{number}', scope, dict(id=f'test-{number}', received_at=number,
                        state='submitted', receipt='{"ok":true,"operation_id":"opaque","reply_text":"synthetic-private"}',
                        notify_status='sent'))
        journal.put(JOBS, 'different-owner', other, dict(id='different-owner', received_at=100,
                    state='uncertain'))
        journal.put(JOBS, 'different-server', different_server, dict(id='different-server', received_at=101,
                    state='received'))
        owner, request_id = 'synthetic-owner', 'synthetic-request'
        self.runtime.journal.put('request', fingerprint([owner, request_id]), owner,
                                {'response': {'status': 'confirmation_required'}})
        journal.put(JOBS, 'test-23', scope, dict(id='test-23', received_at=23, state='submitted',
                    owner=owner, request_id=request_id, agent_journal=str(self.runtime.journal.path.resolve()),
                    notify_status='failed'))
        with patch('desktop_calendar.remote_service.Client') as network:
            rows = self.runtime.recent_remote_history(config)
            self.assertEqual(len(rows), 20)
            self.assertEqual([row.received_at for row in rows], list(range(23, 3, -1)))
            self.assertEqual(rows[0].result, '当时等待确认')
            self.assertEqual(rows[0].notification, '微信通知失败')
            self.assertEqual(rows[0].upload, '回执已上传')
            self.assertIn('写入状态未记录', rows[1].result)
            self.assertNotIn('synthetic-private', repr(rows))
            self.assertEqual(self.runtime.recent_remote_history(
                {**config, 'remote_account': 'owner-two'})[0].result, '执行结果不确定')
            self.assertEqual(self.runtime.recent_remote_history(
                {**config, 'remote_url': different_url})[0].result, '已接收，等待处理')
            network.assert_not_called()

    def test_recent_result_distinguishes_queries_writes_upload_and_model_failure(self):
        config = {**fixture.CONFIG, 'remote_account': 'owner-one'}
        scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'owner-one')
        journal = ChatJournal(self.folder / 'wechat-jobs.sqlite3', self.protection)
        cases = (
            ({'status': 'ok', 'candidates': [{}]}, '查询完成'),
            ({'status': 'succeeded', 'operation_result': {'action': 'create', 'committed': True,
                                                          'goal_satisfied': True, 'event_ids': ['opaque']}}, '已创建 1 项'),
            ({'status': 'clarification_required'}, '当时等待补充信息'),
            ({'status': 'failed', 'error_code': 'model_unavailable'}, '处理失败'),
        )
        for number, (response, expected) in enumerate(cases, 1):
            record = {'id': str(number), 'received_at': number}
            self.worker._set_receipt(record, response)
            journal.put(JOBS, record['id'], scope, record)
            row = self.runtime.recent_remote_history(config)[0]
            self.assertEqual(row.result, expected)
            self.assertEqual(row.upload, '回执待上传')
            self.assertFalse(row.notification)
            self.assertEqual(row.model_unavailable, number == 4)


class NotificationContractTests(unittest.TestCase):
    @staticmethod
    def document(**changes):
        return {**dict(provider='firebase_rtdb_sse', user_id='owner-one',
                       database_url='https://unit-test.asia-southeast1.firebasedatabase.app/',
                       wakeup_path='/wakeups/owner-one/version', id_token=SHORT_TOKEN, expires_in='3600',
                       expires_at_utc=(datetime.now(timezone.utc) + timedelta(seconds=3500)).isoformat(),
                       recommended_refresh_before_seconds=300), **changes}

    def test_deployed_contract_uses_account_path_auth_and_refresh_cutoff(self):
        client = Mock()
        client.request.return_value = self.document()
        before = time.monotonic()
        credentials = FirebaseNotificationContract().notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
        client.request.assert_called_once_with('/api/notifications/session', fixture.CONFIG['remote_token'], b'{}')
        self.assertEqual(credentials.stream_url,
                         'https://unit-test.asia-southeast1.firebasedatabase.app/wakeups/owner-one/version.json')
        self.assertLessEqual(credentials.expires_at - before, 3501)
        self.assertAlmostEqual(credentials.expires_at - credentials.refresh_at, 300)
        self.assertNotIn(SHORT_TOKEN, repr(credentials))
        self.assertEqual(FirebaseNotificationContract().notification_session(
            client, fixture.CONFIG['remote_token']).stream_url, credentials.stream_url)

    def test_rejects_mismatched_account_paths_domains_and_credentials(self):
        invalid = (
            dict(provider='other'),
            dict(user_id='another-user', wakeup_path='/wakeups/another-user/version'),
            dict(wakeup_path='/wakeups/owner-one'),
            dict(wakeup_path='/wakeups/other/version'),
            dict(database_url='https://example.invalid'),
            dict(database_url='https://unit-test.firebaseio.com?auth=not-allowed'),
            dict(id_token=fixture.CONFIG['remote_token']),
            dict(expires_in=True),
            dict(expires_at_utc='2026-11-24T00:00:00'),
            dict(expires_at_utc='2020-01-01T00:00:00Z'),
            dict(recommended_refresh_before_seconds=None),
        )
        client = Mock()
        contract = FirebaseNotificationContract()
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                client.request.return_value = self.document(**change)
                contract.notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
        for status in (401, 502, 503):
            client.request.side_effect = StatusError(status)
            with self.subTest(status=status), self.assertRaises(StatusError) as error:
                contract.notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
            self.assertEqual(error.exception.status, status)


class MockNotifications:
    def __init__(self, contract, client_factory, changed):
        self.changed = changed
        self.generation = None

    def configure(self, generation, *address):
        self.generation = generation if address else None

    def stop(self):
        self.generation = None

    def signal(self, online=True):
        self.changed(self.generation, online)


class WorkerTriggerTests(unittest.TestCase):
    setUp = fixture.RemoteServiceTests.setUp
    events = fixture.RemoteServiceTests.events
    worker_for_steps = fixture.RemoteServiceTests.worker_for_steps

    def start_worker(self, request, *, grace=0.05, interval=0.05, safety=1, minutes=10):
        client = Mock()
        client.request.side_effect = request
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client,
                              contract=Mock(), notification_factory=MockNotifications, interval=interval,
                              fallback_grace=grace, safety_interval=safety)
        worker.configure({**fixture.CONFIG, 'remote_safety_check_minutes': minutes})
        self.addCleanup(lambda: self.stop_worker(worker))
        return worker

    def stop_worker(self, worker):
        worker.stop()
        worker.thread.join(3)
        self.assertFalse(worker.thread.is_alive())

    def test_idle_stream_claims_only_at_start_reconnect_and_safety_deadline(self):
        claims = []
        worker = self.start_worker(lambda path, token: claims.append(path) or {'job': None}, grace=1, safety=0.28)
        self.assertTrue(until(lambda: len(claims) == 1))
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) == 2))
        time.sleep(0.15)
        self.assertEqual(claims, [CLAIM_PATH] * 2)
        self.assertTrue(until(lambda: len(claims) == 3))
        self.assertTrue(worker.notification_online)

    def test_online_default_off_manual_check_and_live_interval_changes(self):
        claims, queued, uploaded = [], [], []
        self.provider.replies = [tool('preview_create', {'title': '手动检查测试', 'date': '2026-01-01'})]

        def request(path, token, body=None):
            if path == CLAIM_PATH:
                claims.append(path)
                return {'job': queued.pop(0) if queued else None}
            uploaded.append(body)
            return fixture.completion(body)

        worker = self.start_worker(request, grace=1, safety=0.15, minutes=0)
        self.assertTrue(until(lambda: len(claims) == 1))
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) == 2))
        time.sleep(0.2)
        self.assertEqual(len(claims), 2)
        self.assertIsNone(worker.safety_interval)

        queued.append(fixture.job('manual', '明天记录手动检查测试'))
        self.assertTrue(worker.request_check())
        self.assertTrue(until(lambda: len(claims) == 4 and len(uploaded) == 1))
        self.assertEqual(len(self.events()), 1)

        generation = worker.generation
        worker.configure({**fixture.CONFIG, 'remote_safety_check_minutes': 30})
        self.assertEqual(worker.generation, generation)
        self.assertTrue(until(lambda: len(claims) == 5))
        worker.configure(fixture.CONFIG)
        time.sleep(0.2)
        self.assertEqual(len(claims), 5)
        worker.configure({'remote_enabled': False})
        self.assertFalse(worker.request_check())
        self.assertEqual(len(claims), 5)

    def test_disconnect_falls_back_then_reconnect_checks_and_stops_frequent_claims(self):
        claims = []
        worker = self.start_worker(lambda path, token: claims.append(path) or {'job': None}, grace=0.06)
        self.assertTrue(until(lambda: len(claims) >= 1))
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) >= 2))
        self.assertEqual(worker.snapshot().connection, 'live')
        worker.notifications.signal(False)
        self.assertEqual(worker.snapshot().connection, 'reconnecting')
        self.assertTrue(until(lambda: len(claims) >= 4))
        self.assertTrue(until(lambda: worker.snapshot().connection == 'backup'))
        self.assertIn('备用', worker.notification_status)
        old = len(claims)
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) > old))
        time.sleep(0.025)
        resumed = len(claims)
        time.sleep(0.18)
        self.assertEqual(len(claims), resumed)
        worker.configure({'remote_enabled': False})
        generation = worker.generation - 1
        worker.notifications.changed(generation, True)
        time.sleep(0.15)
        self.assertEqual(len(claims), resumed)
        self.assertIsNone(worker.notifications.generation)

    def test_auth_rejection_pauses_claims_until_credentials_change(self):
        claims = []

        def request(path, token):
            claims.append(token)
            if token == fixture.CONFIG['remote_token']:
                raise StatusError(401)
            return {'job': None}

        worker = self.start_worker(request)
        self.assertTrue(until(lambda: worker.snapshot().connection == 'invalid'))
        self.assertFalse(worker.request_check())
        self.assertIsNone(worker.notifications.generation)
        time.sleep(0.12)
        self.assertEqual(claims, [fixture.CONFIG['remote_token']])
        worker.configure({**fixture.CONFIG, 'remote_token': 'replacement-synthetic-token'})
        self.assertTrue(until(lambda: len(claims) == 2))
        worker.notifications.signal()
        self.assertTrue(until(lambda: worker.snapshot().connection == 'live'))

    def test_backup_requires_successful_authenticated_request(self):
        claims = []
        succeed = threading.Event()

        def request(path, token=None):
            if path == '/':
                return {'status': 'ok'}
            claims.append(path)
            if not succeed.is_set():
                raise Unavailable
            return {'job': None}

        worker = self.start_worker(request, grace=0.03)
        self.assertTrue(until(lambda: len(claims) >= 2))
        self.assertEqual(worker.snapshot().connection, 'unavailable')
        succeed.set()
        self.assertTrue(until(lambda: worker.snapshot().connection == 'backup'))

    def test_firebase_disconnect_and_session_auth_have_distinct_actions(self):
        from desktop_calendar.remote_notifications import AUTH_REJECTED
        events = []
        contract = Mock()
        contract.notification_session.side_effect = [
            credential(), credential(), StatusError(403),
        ]

        def stream(_credentials, _stop, ready, _wake):
            ready()
            return True  # Planned refresh, equivalent to proactively rotating the ID token.

        listener = NotificationListener(contract, lambda url: url,
                                        lambda _generation, event: events.append(event), stream=stream, retry=(0.02, 0.05))
        listener.configure(1, fixture.URL, fixture.CONFIG['remote_token'])
        try:
            self.assertTrue(until(lambda: AUTH_REJECTED in events))
            self.assertEqual(events, [True, False, True, False, AUTH_REJECTED])
            time.sleep(0.1)
            self.assertEqual(contract.notification_session.call_count, 3)
        finally:
            listener.stop()
            listener.thread.join(3)

    def test_racing_empty_notification_and_duplicate_jobs_drained_once(self):
        claims, uploads, queued = [], [], []
        arm_empty = threading.Event()
        queue_ready = threading.Event()
        self.provider.replies = [tool('preview_create', {'title': '测试事项', 'date': '2026-01-01'})]
        duplicate = fixture.job('racing', '明天创建测试事项')

        def request(path, token, body=None):
            if path == CLAIM_PATH:
                index = len(claims)
                if queued:
                    result = {'job': queued.pop(0)}
                else:
                    result = {'job': None}
                    if arm_empty.is_set():
                        arm_empty.clear()
                        queued.extend([duplicate, duplicate])
                        for _ in range(4):
                            worker.notifications.signal()
                        queue_ready.set()
                claims.append(index)
                return result
            uploads.append(body)
            return fixture.completion(body, duplicate=len(uploads) > 1)

        worker = self.start_worker(request, grace=1)
        self.assertTrue(until(lambda: len(claims) == 1))
        arm_empty.set()
        worker.notifications.signal()
        self.assertTrue(queue_ready.wait(3))
        self.assertTrue(until(lambda: len(uploads) == 2 and len(claims) == 5))
        time.sleep(0.1)
        self.assertEqual(len(claims), 5)
        self.assertEqual(len(self.provider.messages), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(uploads[0], uploads[1])

    def test_notification_during_agent_turn_keeps_second_job_in_same_worker(self):
        queued = [fixture.job('first', '明天创建第一项')]
        uploads = []
        in_agent = threading.Event()
        release_agent = threading.Event()
        self.provider.replies = [tool('preview_create', {'title': name, 'date': '2026-01-01'})
                                 for name in ('第一项', '第二项')]
        complete = self.provider.complete

        def provider(*args):
            if not in_agent.is_set():
                in_agent.set()
                release_agent.wait(3)
            return complete(*args)

        def request(path, token, body=None):
            if path == CLAIM_PATH:
                return {'job': queued.pop(0) if queued else None}
            uploads.append(body)
            return fixture.completion(body)

        with patch.object(self.provider, 'complete', side_effect=provider):
            worker = self.start_worker(request, grace=1)
            thread = worker.thread
            try:
                self.assertTrue(in_agent.wait(3))
                queued.append(fixture.job('second', '明天创建第二项'))
                for _ in range(5):
                    worker.notifications.signal()
                self.assertIs(worker.thread, thread)
                self.assertEqual(self.events(), [])
                release_agent.set()
                self.assertTrue(until(lambda: len(uploads) == 2))
            finally:
                release_agent.set()
        self.assertEqual(len(self.provider.messages), 2)
        self.assertEqual({event.title for event in self.events()}, {'第一项', '第二项'})

    def test_account_or_host_switch_cannot_upload_old_receipt(self):
        first = {**fixture.CONFIG, 'remote_account': 'first'}
        old_scope = remote_scope(first['remote_url'], first['remote_token'], first['remote_account'])
        row = self.worker._intake(fixture.job('old', '明天添加测试事项'), old_scope, fixture.URL)
        self.worker._set_receipt(row, {'status': 'ok', 'assistant_message': '原回执'})
        self.worker._save(old_scope, row)
        requests = []
        client = Mock()

        def request(path, token, body=None):
            requests.append((path, token, body))
            return {'job': None}

        client.request.side_effect = request
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client, interval=0.04)
        worker.configure({**first, 'remote_account': 'second'})
        self.addCleanup(lambda: self.stop_worker(worker))
        self.assertTrue(until(lambda: bool(requests)))
        worker.configure({**first, 'remote_url': 'https://next.invalid', 'remote_token': 'new-token'})
        self.assertTrue(until(lambda: any(token == 'new-token' for _, token, _ in requests)))
        self.assertTrue(all(path == CLAIM_PATH for path, _, _ in requests))
        self.assertEqual(self.ledger.get(JOBS, row['id'], old_scope)['state'], 'ready')

    def test_original_b3_token_resumes_saved_receipt_without_rebinding(self):
        legacy_scope = fingerprint(fixture.URL)
        config = {**fixture.CONFIG, 'remote_legacy_credential': fingerprint(
            [fixture.URL, fixture.CONFIG['remote_token']],
        )}
        row = self.worker._intake(fixture.job('legacy', '明天添加测试事项'), legacy_scope, fixture.URL)
        self.worker._set_receipt(row, {'status': 'ok', 'assistant_message': '原回执'})
        self.worker._save(legacy_scope, row)
        client = Mock()
        client.request.side_effect = lambda path, token, body=None: (
            {'job': None} if path == CLAIM_PATH else fixture.completion(body, duplicate=True)
        )
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client, interval=0.04)
        worker.configure(config)
        self.addCleanup(lambda: self.stop_worker(worker))
        self.assertTrue(until(lambda: self.ledger.get(JOBS, row['id'], legacy_scope)['state'] == 'submitted'))
        self.assertEqual(client.request.call_args_list[0].args,
                         ('/api/jobs/legacy/complete', fixture.CONFIG['remote_token'], row['receipt'].encode()))
        self.assertEqual(self.provider.messages, [])

    def test_disable_while_agent_busy_keeps_claim_durable_without_starting_remote_turn(self):
        message = fixture.job('stop-before-agent', '明天添加测试事项')
        scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'])
        claims = []

        def request(path, token):
            claims.append(path)
            return {'job': message}

        with self.service.lock:
            worker = self.start_worker(request, grace=1)
            self.assertTrue(until(lambda: bool(self.ledger.all(JOBS, scope))))
            worker.configure({'remote_enabled': False})
        self.stop_worker(worker)
        self.assertEqual(claims, [CLAIM_PATH])
        self.assertEqual(self.ledger.all(JOBS, scope)[0]['state'], 'received')
        self.assertEqual(self.provider.messages, [])
