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
from desktop_calendar.remote_cloud import AccountMismatch, Binding, FirebaseNotificationContract, NotificationSession, remote_scope
from desktop_calendar.remote_notifications import (
    ACCOUNT_MISMATCH, AUTH_REJECTED, NotificationListener, VerifiedAccount, sse_events, stream_notifications,
)
from desktop_calendar.remote_service import JOBS, RemoteWorker, claim_checked_at
from desktop_calendar.remote_transport import CLAIM_PATH, StatusError, Unavailable
from version import __version__
import test_remote_service as fixture
from test_calendar_chat import tool


STREAM_URL = 'https://unit-test-default-rtdb.firebaseio.com/notices/test-account.json'
SHORT_TOKEN = 'synthetic-short-lived-id-token'


def credential(account='owner-one'):
    return NotificationSession(STREAM_URL, SHORT_TOKEN, time.monotonic() + 60, account_id=account)


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
        contract.notification_session.side_effect = lambda client, token, account: credential(account or 'owner-one')

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

    def test_manual_token_rotation_keeps_expected_account_until_explicit_rebinding(self):
        original = {**self.configuration.load(), **fixture.BOUND_CONFIG, 'remote_enabled': False}
        self.configuration.save(original)
        self.runtime.save_settings({**original, 'remote_token': 'rotated-manual-secret', 'remote_account': ''})
        self.assertEqual(self.configuration.load()['remote_account'], 'owner-one')
        self.runtime.save_remote_binding(fixture.URL, Binding('owner-two', 'explicit-activation-secret'))
        self.assertEqual(self.configuration.load()['remote_account'], 'owner-two')

    def test_failed_apply_after_saving_replacement_revokes_previous_worker(self):
        original = {**self.configuration.load(), **fixture.BOUND_CONFIG}
        self.configuration.save(original)
        self.runtime.remote = self.worker
        self.assertTrue(self.worker.request_check())
        with patch.object(self.runtime, 'apply_remote', side_effect=OSError('cannot read saved config')):
            with self.assertRaises(OSError):
                self.runtime.save_settings({**original, 'remote_token': 'replacement-manual-token'})
        self.assertEqual(self.configuration.load()['remote_token'], 'replacement-manual-token')
        self.assertFalse(self.worker.request_check())
        self.assertEqual(self.worker.snapshot().connection, 'disabled')

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
        self.runtime.remote = Mock(condition=threading.Condition())
        ledger_scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'user-one')
        row = self.worker._intake(fixture.job('keep-me', '明天记录测试日程'), ledger_scope)
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
        self.runtime.remote = Mock(condition=threading.Condition())
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
                        notify_status='sent', verified_scope=scope))
        journal.put(JOBS, 'different-owner', other, dict(id='different-owner', received_at=100,
                    state='uncertain', verified_scope=other))
        journal.put(JOBS, 'different-server', different_server, dict(id='different-server', received_at=101,
                    state='received', verified_scope=different_server))
        owner, request_id = 'synthetic-owner', 'synthetic-request'
        self.runtime.journal.put('request', fingerprint([owner, request_id]), owner,
                                {'response': {'status': 'confirmation_required'}})
        journal.put(JOBS, 'test-23', scope, dict(id='test-23', received_at=23, state='submitted',
                    owner=owner, request_id=request_id, agent_journal=str(self.runtime.journal.path.resolve()),
                    notify_status='failed', verified_scope=scope))
        with patch('desktop_calendar.remote_service.Client') as network:
            rows = self.runtime.recent_remote_history(config)
            self.assertEqual(len(rows), 20)
            self.assertEqual([row.received_at for row in rows], list(range(23, 3, -1)))
            self.assertEqual(rows[0].result, '当时等待确认')
            self.assertEqual(rows[0].notification, '通知失败')
            self.assertEqual(rows[0].upload, '回执已上传')
            self.assertIn('写入状态未记录', rows[1].result)
            self.assertNotIn('synthetic-private', repr(rows))
            self.assertEqual(self.runtime.recent_remote_history(
                {**config, 'remote_account': 'owner-two'})[0].result, '执行结果不确定')
            self.assertEqual(self.runtime.recent_remote_history(
                {**config, 'remote_url': different_url})[0].result, '已接收，等待处理')
            network.assert_not_called()

    def test_history_does_not_expose_result_of_unverified_legacy_record(self):
        config = {**fixture.CONFIG, 'remote_account': 'owner-one'}
        journal = ChatJournal(self.folder / 'wechat-jobs.sqlite3', self.protection)
        for number, state in enumerate(('ready', 'submitted')):
            journal.put(JOBS, state, fixture.SERVER, dict(id=state, state=state, received_at=number,
                        processed_at=99, history_result='其他账号的旧结果', notify_status='sent'))
        rows = self.runtime.recent_remote_history(config)
        self.assertEqual([row.result for row in rows], ['旧任务缺少账号核验记录'] * 2)
        self.assertEqual([row.notification for row in rows], ['', ''])
        self.assertEqual(rows[1].upload, '回执已保留，暂停上传')

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
            record = {'id': str(number), 'received_at': number, 'verified_scope': scope}
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
        client.request.assert_called_once()
        path, token, body = client.request.call_args.args
        self.assertEqual((path, token), ('/api/notifications/session', fixture.CONFIG['remote_token']))
        self.assertEqual(json.loads(body), {
            'interaction_schema_version': 2, 'client_version': __version__,
            'capabilities': ['business_receipt', 'action', 'reference', 'quick_query',
                             'proactive_action','reminder_quick_query','reminder_receipt'],
        })
        self.assertEqual(credentials.interaction_capabilities, ())
        self.assertEqual(credentials.stream_url,
                         'https://unit-test.asia-southeast1.firebasedatabase.app/wakeups/owner-one/version.json')
        self.assertLessEqual(credentials.expires_at - before, 3501)
        self.assertAlmostEqual(credentials.expires_at - credentials.refresh_at, 300)
        self.assertNotIn(SHORT_TOKEN, repr(credentials))
        self.assertEqual(FirebaseNotificationContract().notification_session(
            client, fixture.CONFIG['remote_token']).stream_url, credentials.stream_url)

    def test_display_name_and_negotiated_capabilities_are_optional(self):
        client = Mock()
        contract = FirebaseNotificationContract()
        client.request.return_value = self.document(display_name=' 演示账号 ', interaction_schema_version=1)
        verified = contract.notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
        self.assertEqual(verified.display_name, '演示账号')
        self.assertEqual(verified.interaction_version, 1)
        self.assertEqual(verified.interaction_capabilities, ('business_receipt', 'action', 'reference'))
        self.assertNotIn('演示账号', repr(verified))
        for advertised, expected in ((['business_receipt', 'quick_query', 'future'], ('business_receipt', 'quick_query')),
                                     ([], ()), ('quick_query', ()), (None, ())):
            client.request.return_value = self.document(interaction_schema_version=1,
                                                        interaction_capabilities=advertised)
            verified = contract.notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
            self.assertEqual(verified.interaction_capabilities, expected)
        for value in (None, '1', True, 99):
            client.request.return_value = self.document(display_name='无效\n名称', interaction_schema_version=value)
            verified = contract.notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')
            self.assertEqual((verified.display_name, verified.interaction_version), ('', 0))
            self.assertEqual(verified.interaction_capabilities, ())

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

    def test_successful_session_response_without_bound_identity_is_authorization_failure(self):
        client = Mock()
        for malformed in (None, '', {'unexpected': 'owner-one'}):
            with self.subTest(account=malformed):
                client.request.return_value = self.document(user_id=malformed)
                with self.assertRaises(AccountMismatch):
                    FirebaseNotificationContract().notification_session(client, fixture.CONFIG['remote_token'], 'owner-one')


class MockNotifications:
    def __init__(self, contract, client_factory, changed):
        self.changed = changed
        self.generation = None

    def configure(self, generation, *address):
        self.generation = generation if address else None
        if address and address[2]:
            self.changed(generation, VerifiedAccount(address[2]))

    def stop(self):
        self.generation = None

    def signal(self, online=True):
        self.changed(self.generation, online)


class WorkerIdentityTests(unittest.TestCase):
    setUp = fixture.RemoteServiceTests.setUp
    worker_for_steps = fixture.RemoteServiceTests.worker_for_steps
    events = fixture.RemoteServiceTests.events

    def start(self, request, *, stream=None, account='owner-one', token=None):
        client = Mock()
        client.request.side_effect = request
        stream = stream or Mock(return_value=False)
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client,
                              contract=FirebaseNotificationContract(), interval=.02, fallback_grace=.04,
                              notification_factory=lambda *args: NotificationListener(*args, stream=stream, retry=(.02,.02)))
        def stop():
            worker.stop()
            for thread in (worker.thread, worker.notifications.thread):
                if thread:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        worker.configure({**fixture.CONFIG, 'remote_account': account,
                          'remote_token': token or fixture.CONFIG['remote_token']})
        return worker, client, stream

    def test_claim_with_different_internal_owner_is_blocked_before_intake(self):
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                return NotificationContractTests.document()
            self.assertEqual(path, CLAIM_PATH)
            return {'job': {**fixture.job('other-owner', '新建事项'), 'owner_id': 'owner-two'}}
        worker, client, _ = self.start(request)
        self.assertTrue(until(lambda: worker.snapshot().connection == 'mismatch'))
        self.assertEqual(self.ledger.all(JOBS, fixture.SERVER), [])
        self.assertFalse(worker.request_check())
        self.assertEqual(self.provider.messages, [])
        self.assertEqual(self.events(), [])
        self.assertTrue(all(not call.args[0].endswith('/complete') for call in client.request.call_args_list))

    def test_delayed_identity_holds_first_claim_and_then_allows_it(self):
        waiting, resume, claim = (threading.Event() for _ in range(3))
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                waiting.set()
                resume.wait(3)
                return NotificationContractTests.document()
            self.assertEqual(path, CLAIM_PATH)
            claim.set()
            return {'job': None}
        worker, _, _ = self.start(request)
        self.addCleanup(resume.set)
        self.assertTrue(waiting.wait(1))
        self.assertFalse(claim.wait(.14))
        self.assertFalse(worker.request_check())
        self.assertEqual(self.provider.messages, [])
        self.assertEqual(self.events(), [])
        resume.set()
        self.assertTrue(claim.wait(2))

    def test_mismatched_account_never_starts_claim_agent_or_upload(self):
        scope = remote_scope(fixture.URL, fixture.CONFIG['remote_token'], 'owner-one')
        record = self.worker._intake(fixture.job('old-receipt', '添加之前的事项'), scope)
        self.worker._set_receipt(record, {'status': 'ok', 'assistant_message': '旧账号回执'})
        self.worker._save(scope, record)
        received = self.worker._intake(fixture.job('old-received', '明天记录测试事项'), scope)
        self.provider.replies = [tool('preview_create', {'title':'测试事项','date':'2026-01-01'})]
        requested = threading.Event()
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                requested.set()
                return NotificationContractTests.document(user_id='owner-two', wakeup_path='/wakeups/owner-two/version')
            return {'job': None} if path == CLAIM_PATH else fixture.completion(body)
        worker, client, stream = self.start(request)
        self.assertTrue(requested.wait(1))
        self.assertTrue(until(lambda: worker.snapshot().connection == 'mismatch'))
        with worker.condition:
            worker.offline_since -= 46
            worker.condition.notify_all()
        time.sleep(.10)
        self.assertEqual([call.args[0] for call in client.request.call_args_list], ['/api/notifications/session'])
        self.assertFalse(worker.request_check())
        stream.assert_not_called()
        self.assertEqual(self.provider.messages, [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.ledger.get(JOBS, record['id'], scope)['state'], 'ready')
        self.assertEqual(self.ledger.get(JOBS, received['id'], scope)['state'], 'received')
        sid = self.service.new_session()['session_id']
        local = self.service.send_message(sid, '添加测试事项', 'local-after-mismatch')
        self.assertEqual(local['status'], 'succeeded')
        self.assertEqual([event.title for event in self.events()], ['测试事项'])

    def test_old_asynchronous_session_and_callbacks_cannot_change_new_rejection(self):
        waiting, release = threading.Event(), threading.Event()
        def request(path, token, body=None):
            if path != '/api/notifications/session':
                self.fail(f'Unexpected Worker request: {path}')
            if token == fixture.CONFIG['remote_token']:
                waiting.set()
                release.wait(3)
            return NotificationContractTests.document()
        worker, client, stream = self.start(request)
        self.addCleanup(release.set)
        self.assertTrue(waiting.wait(1))
        old = worker.generation
        worker.configure({**fixture.CONFIG, 'remote_account': 'owner-two', 'remote_token': 'other-synthetic-token'})
        worker._notification(old, VerifiedAccount('owner-one'))
        worker._notification(old, True)
        self.assertFalse(worker.request_check())
        release.set()
        self.assertTrue(until(lambda: worker.snapshot().connection == 'mismatch'))
        for generation in (old, worker.generation):
            worker._notification(generation, VerifiedAccount('owner-two'))
            worker._notification(generation, True)
        self.assertEqual(worker.snapshot().connection, 'mismatch')
        self.assertFalse(worker.request_check())
        self.assertEqual(len(client.request.call_args_list), 2)
        stream.assert_not_called()

    def test_same_account_rotation_uploads_only_previously_verified_receipt(self):
        verified = self.worker._intake(fixture.job('verified-old','添加测试事项'), fixture.SERVER)
        unknown = self.worker._intake(fixture.job('unproven-old','添加测试事项'), fixture.SERVER)
        unknown.pop('verified_scope')
        for record in (verified, unknown):
            self.worker._set_receipt(record, {'status': 'ok', 'assistant_message': '已记录'})
            self.worker._save(fixture.SERVER, record)
        waiting, release = threading.Event(), threading.Event()
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                waiting.set()
                release.wait(3)
                return NotificationContractTests.document()
            if path == CLAIM_PATH:
                return {'job': None}
            return fixture.completion(body, duplicate=True)
        worker, client, _ = self.start(request, token='new-same-account-token')
        self.addCleanup(release.set)
        self.assertTrue(waiting.wait(1))
        self.assertFalse(worker.request_check())
        self.assertEqual(len(client.request.call_args_list), 1)
        release.set()
        self.assertTrue(until(lambda: self.ledger.get(JOBS, verified['id'], fixture.SERVER)['state'] == 'submitted'))
        completions = [call.args for call in client.request.call_args_list if call.args[0].endswith('/complete')]
        self.assertEqual(completions, [('/api/jobs/verified-old/complete', 'new-same-account-token', verified['receipt'].encode())])
        self.assertEqual(self.ledger.get(JOBS, unknown['id'], fixture.SERVER)['state'], 'ready')

    def test_revocation_during_planning_prevents_calendar_write(self):
        self.check_revoked_write('planning')

    def test_revocation_after_journal_reservation_prevents_calendar_write(self):
        self.check_revoked_write('reserved')

    def test_revocation_inside_sqlite_commit_keeps_original_account_receipt(self):
        self.check_revoked_write('committing')

    def test_claim_returned_after_revocation_is_not_admitted_to_agent(self):
        entered, resume, revoke = (threading.Event() for _ in range(3))
        def stream(_credentials, stopped, ready, _wake):
            ready()
            return revoke.wait(3) and not stopped.is_set()
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                return (NotificationContractTests.document(user_id='owner-two', wakeup_path='/wakeups/owner-two/version')
                        if revoke.is_set() else NotificationContractTests.document())
            self.assertEqual(path, CLAIM_PATH)
            entered.set()
            resume.wait(3)
            return {'job': fixture.job('late-claim', '明天增加测试事项')}
        worker, client, _ = self.start(request, stream=stream)
        self.addCleanup(resume.set)
        self.addCleanup(revoke.set)
        self.assertTrue(entered.wait(1))
        revoke.set()
        self.assertTrue(until(lambda: worker.snapshot().connection == 'mismatch'))
        resume.set()
        from desktop_calendar.remote_service import CLAIM_INCIDENT
        self.assertTrue(until(lambda: bool(self.ledger.all(CLAIM_INCIDENT, fixture.SERVER))))
        self.assertEqual(self.ledger.all(JOBS, fixture.SERVER), [])
        self.assertEqual(self.provider.messages, [])
        self.assertEqual(self.events(), [])
        self.assertFalse(any(call.args[0].endswith('/complete') for call in client.request.call_args_list))

    def test_late_complete_cannot_mark_original_receipt_submitted(self):
        row = self.worker._intake(fixture.job('late-complete', '测试事项'), fixture.SERVER)
        self.worker._set_receipt(row, {'status':'ok', 'assistant_message':'测试回执'})
        self.worker._save(fixture.SERVER, row)
        entered, resume, finished = (threading.Event() for _ in range(3))
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                return NotificationContractTests.document()
            self.assertTrue(path.endswith('/complete'))
            entered.set()
            resume.wait(3)
            return fixture.completion(body)
        upload = RemoteWorker._upload
        def observe_upload(*args):
            try:
                return upload(*args)
            finally:
                finished.set()
        with patch.object(RemoteWorker, '_upload', new=observe_upload):
            worker, _, _ = self.start(request)
            self.addCleanup(resume.set)
            self.assertTrue(entered.wait(1))
            worker._notification(worker.generation, ACCOUNT_MISMATCH)
            resume.set()
            self.assertTrue(finished.wait(1))
            self.assertEqual(worker.snapshot().connection, 'mismatch')
            self.assertEqual(self.ledger.get(JOBS, row['id'], fixture.SERVER), row)

    def check_revoked_write(self, phase):
        entered, resume, revoke = (threading.Event() for _ in range(3))
        msg_id = 'change-auth-' + phase
        message = fixture.job(msg_id, '明天记录测试事项')
        record_id = fingerprint([fixture.SERVER, msg_id])
        operation = tool('preview_create', {'title':'测试事项','date':'2026-01-01'})
        def stream(_credentials, stopped, ready, _wake):
            ready()
            return revoke.wait(3) and not stopped.is_set()
        def request(path, token, body=None):
            if path == '/api/notifications/session':
                if revoke.is_set():
                    return NotificationContractTests.document(user_id='owner-two', wakeup_path='/wakeups/owner-two/version')
                return NotificationContractTests.document()
            if path == CLAIM_PATH:
                return {'job': message}
            self.fail(f'Unexpected receipt upload: {path}')
        def block(original):
            def run(*args, **kwargs):
                if phase == 'reserved':
                    result = original(*args, **kwargs)
                    if args[-1].get('status') != 'executing':
                        return result
                entered.set()
                if not resume.wait(3):
                    raise RuntimeError('timed out waiting for test authorization result')
                return result if phase == 'reserved' else original(*args, **kwargs)
            return run
        self.provider.replies = [operation]
        target, name = {'committing': (self.service.gateway.events, 'commit'),
                        'reserved': (self.service.journal, 'transition'),
                        'planning': (self.provider, 'complete')}[phase]
        original = getattr(target, name)
        with patch.object(target, name, side_effect=block(original)) as blocked:
            worker, client, _ = self.start(request, stream=stream)
            try:
                self.assertTrue(entered.wait(2))
                revoke.set()
                self.assertTrue(until(lambda: worker.snapshot().connection == 'mismatch'))
                resume.set()
                self.assertTrue(until(lambda: any(
                    row['id'] == record_id and row['state'] == 'ready' for row in self.ledger.all(JOBS, fixture.SERVER))))
                row = self.ledger.get(JOBS, record_id, fixture.SERVER)
                receipt = json.loads(row['receipt'])
                self.assertFalse(any(call.args[0].endswith('/complete') for call in client.request.call_args_list))
                self.assertEqual(len(self.events()), int(phase == 'committing'))
                self.assertEqual(receipt['ok'], phase == 'committing')
                if phase == 'committing':
                    self.assertTrue(self.executor(lambda: self.store.repository.receipt(receipt['operation_id'])))
                else:
                    self.assertEqual(receipt['error'], 'remote_authorization_changed')
                self.assertTrue(blocked.called)
            finally:
                revoke.set()
                resume.set()
                worker.stop()
                worker.thread.join(3)


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
        worker.configure({**fixture.BOUND_CONFIG, 'remote_safety_check_minutes': minutes})
        self.addCleanup(lambda: self.stop_worker(worker))
        return worker

    def stop_worker(self, worker):
        worker.stop()
        worker.thread.join(3)
        self.assertFalse(worker.thread.is_alive())

    def test_claim_check_time_accepts_offsets_and_falls_back_on_invalid_values(self):
        expected = datetime(2026, 11, 24, 8, 22, 9, tzinfo=timezone.utc).timestamp()
        for response in ({'checked_at': '2026-11-24T08:22:09Z'}, {'checked_at': '2026-11-24T16:22:09+08:00'}):
            self.assertEqual(claim_checked_at(response, 42), expected)
        for response in ({}, {'checked_at': None}, {'checked_at': 'invalid'}, {'checked_at': '2026-11-24T08:22:09'}):
            self.assertEqual(claim_checked_at(response, 42), 42)

    def test_manual_empty_check_and_failure_keep_separate_success_time(self):
        calls = []
        release = threading.Event()
        before = time.time()

        def request(path, token, body=None):
            calls.append(path)
            if len(calls) == 1:
                return {'job': None, 'status': 'empty', 'checked_at': '2026-11-24T08:22:09Z', 'message': 'empty'}
            if len(calls) == 2:
                if not release.wait(3):
                    raise Unavailable
                return {'job': None}
            raise Unavailable

        worker = self.start_worker(request, grace=10, interval=10, minutes=0)
        self.addCleanup(release.set)
        expected = datetime(2026, 11, 24, 8, 22, 9, tzinfo=timezone.utc).timestamp()
        self.assertTrue(until(lambda: worker.snapshot().last_checked_at == expected))
        self.assertEqual(worker.snapshot().check_result, 'empty')
        self.assertTrue(worker.request_check())
        self.assertTrue(until(lambda: len(calls) == 2))
        self.assertEqual(worker.snapshot().check_result, 'checking')
        self.assertEqual(worker.snapshot().last_checked_at, expected)
        release.set()
        self.assertTrue(until(lambda: worker.snapshot().check_result == 'empty'))
        latest = worker.snapshot().last_checked_at
        self.assertLessEqual(before, latest)
        self.assertLessEqual(latest, time.time())
        self.assertFalse(worker.notification_online)
        self.assertEqual(self.ledger.all(JOBS, worker.config[2]), [])
        self.assertIsNone(worker.snapshot().completion)
        self.assertTrue(worker.request_check())
        self.assertTrue(until(lambda: worker.snapshot().check_result == 'failed'))
        failed = worker.snapshot()
        self.assertEqual(failed.last_checked_at, latest)
        self.assertEqual(failed.connection, 'unavailable')
        self.assertEqual(failed.history_revision, 0)
        self.assertEqual(calls, [CLAIM_PATH] * 3)

    def test_idle_stream_claims_only_at_start_reconnect_and_safety_deadline(self):
        claims = []
        def request(path, token, body=None):
            claims.append((path, json.loads(body)))
            return {'job': None}

        worker = self.start_worker(request, grace=1, safety=0.28)
        self.assertTrue(until(lambda: len(claims) == 1))
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) == 2))
        time.sleep(0.15)
        self.assertEqual(len(claims), 2)
        self.assertTrue(until(lambda: len(claims) == 3))
        for path, declaration in claims[:3]:
            self.assertEqual(path, CLAIM_PATH)
            self.assertEqual(declaration, {
                'interaction_schema_version': 0, 'client_version': __version__,
                'capabilities': [],
            })
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
        self.assertTrue(until(lambda: worker.snapshot().check_result == 'empty'))
        drained = worker.snapshot()
        self.assertEqual(drained.completion.result, '已创建 1 项')
        self.assertGreaterEqual(drained.last_checked_at, drained.completion.processed_at)
        self.assertEqual(len(self.ledger.all(JOBS, worker.config[2])), 1)

        generation = worker.generation
        worker.configure({**fixture.BOUND_CONFIG, 'remote_safety_check_minutes': 30})
        self.assertEqual(worker.generation, generation)
        self.assertTrue(until(lambda: len(claims) == 5))
        worker.configure(fixture.BOUND_CONFIG)
        time.sleep(0.2)
        self.assertEqual(len(claims), 5)
        worker.configure({'remote_enabled': False})
        self.assertFalse(worker.request_check())
        self.assertEqual(len(claims), 5)

    def test_disconnect_falls_back_then_reconnect_checks_and_stops_frequent_claims(self):
        claims = []
        worker = self.start_worker(lambda path, token, body=None: claims.append(path) or {'job': None}, grace=0.06)
        self.assertTrue(until(lambda: len(claims) >= 1))
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) >= 2))
        self.assertEqual(worker.snapshot().connection, 'live')
        connected = worker.snapshot().last_checked_at
        worker.notifications.signal(False)
        self.assertEqual(worker.snapshot().connection, 'reconnecting')
        self.assertTrue(until(lambda: len(claims) >= 4))
        self.assertTrue(until(lambda: worker.snapshot().connection == 'backup'))
        backup = worker.snapshot()
        self.assertGreater(backup.last_checked_at, connected)
        self.assertEqual(backup.check_result, 'empty')
        self.assertFalse(worker.notification_online)
        self.assertIn('备用', worker.notification_status)
        old = len(claims)
        worker.notifications.signal()
        self.assertTrue(until(lambda: len(claims) > old))
        self.assertTrue(until(lambda: worker.snapshot().last_checked_at > backup.last_checked_at))
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

        def request(path, token, body=None):
            claims.append(token)
            if token == fixture.CONFIG['remote_token']:
                raise StatusError(401)
            return {'job': None}

        worker = self.start_worker(request)
        self.assertTrue(until(lambda: worker.snapshot().connection == 'invalid'))
        self.assertEqual(worker.snapshot().check_result, 'failed')
        self.assertIsNone(worker.snapshot().last_checked_at)
        self.assertFalse(worker.request_check())
        self.assertIsNone(worker.notifications.generation)
        time.sleep(0.12)
        self.assertEqual(claims, [fixture.CONFIG['remote_token']])
        worker.configure({**fixture.BOUND_CONFIG, 'remote_token': 'replacement-synthetic-token'})
        self.assertTrue(until(lambda: len(claims) == 2))
        worker.notifications.signal()
        self.assertTrue(until(lambda: worker.snapshot().connection == 'live'))

    def test_backup_requires_successful_authenticated_request(self):
        claims = []
        succeed = threading.Event()

        def request(path, token=None, body=None):
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
            self.assertEqual(events, [VerifiedAccount('owner-one'), True, False,
                                      VerifiedAccount('owner-one'), True, False, AUTH_REJECTED])
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
        row = self.worker._intake(fixture.job('old', '明天添加测试事项'), old_scope)
        row['verified_scope'] = old_scope
        self.worker._set_receipt(row, {'status': 'ok', 'assistant_message': '原回执'})
        self.worker._save(old_scope, row)
        requests = []
        client = Mock()

        def request(path, token, body=None):
            requests.append((path, token, body))
            return {'job': None}

        client.request.side_effect = request
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client, interval=0.04,
                              notification_factory=MockNotifications)
        worker.configure({**first, 'remote_account': 'second'})
        self.addCleanup(lambda: self.stop_worker(worker))
        self.assertTrue(until(lambda: bool(requests)))
        worker.configure({**first, 'remote_url': 'https://next.invalid', 'remote_token': 'new-token'})
        self.assertTrue(until(lambda: any(token == 'new-token' for _, token, _ in requests)))
        self.assertTrue(all(path == CLAIM_PATH for path, _, _ in requests))
        self.assertEqual(self.ledger.get(JOBS, row['id'], old_scope)['state'], 'ready')

    def test_manual_bootstrap_pins_account_and_keeps_unverified_legacy_receipt(self):
        legacy_scope = fingerprint(fixture.URL)
        config = {**self.configuration.load(), **fixture.CONFIG}
        config.pop('remote_legacy_credential')
        self.configuration.path.write_bytes(self.protection.encrypt(json.dumps(config).encode()))
        row = self.worker._intake(fixture.job('legacy', '明天添加测试事项'), legacy_scope)
        self.worker._set_receipt(row, {'status': 'ok', 'assistant_message': '原回执'})
        self.worker._save(legacy_scope, row)
        client = Mock()
        client.request.side_effect = lambda path, token, body=None: (
            NotificationContractTests.document() if path == '/api/notifications/session' else {'job': None})
        # Use the real runtime callback to pin and rotate the Worker generation.
        with patch('calendar_runtime.RemoteWorker', side_effect=lambda service, journal, **kwargs: RemoteWorker(
                service, self.ledger, **kwargs, client_factory=lambda _url: client, interval=.04,
                notification_factory=lambda *args: NotificationListener(*args, stream=Mock(return_value=False)))):
            self.runtime.apply_remote()
        worker = self.runtime.remote
        self.addCleanup(lambda: self.stop_worker(worker))
        self.assertTrue(until(lambda: any(call.args[0] == CLAIM_PATH for call in client.request.call_args_list)))
        self.assertEqual([call.args[0] for call in client.request.call_args_list][:3],
                         ['/api/notifications/session', '/api/notifications/session', CLAIM_PATH])
        self.assertEqual(self.configuration.load()['remote_account'], 'owner-one')
        self.assertEqual(self.ledger.get(JOBS, row['id'], legacy_scope)['state'], 'ready')
        self.assertFalse(any(call.args[0].endswith('/complete') for call in client.request.call_args_list))
        self.configuration.save({**self.configuration.load(), 'remote_account': ''})
        self.assertEqual(self.configuration.load()['remote_account'], 'owner-one')
        self.assertEqual(self.provider.messages, [])

    def test_disable_while_agent_busy_keeps_claim_durable_without_starting_remote_turn(self):
        message = fixture.job('stop-before-agent', '明天添加测试事项')
        scope = fixture.SERVER
        claims = []

        def request(path, token, body=None):
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
