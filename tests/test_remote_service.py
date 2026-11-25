"""Remote intake and fault recovery with a temporary SQLite calendar and fake cloud."""
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from calendar_core import Store
from calendar_executor import SerialExecutor
from calendar_runtime import CalendarRuntime
from desktop_calendar.assistant.calendar_chat_protocol import OWNER, fingerprint
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.assistant.llm_provider import AIConfiguration
from desktop_calendar.remote_service import JOBS, RemoteWorker, make_receipt, message_time
from desktop_calendar.remote_cloud import remote_scope
from desktop_calendar.remote_notifications import VerifiedAccount
from desktop_calendar.remote_transport import CLAIM_PATH, Unavailable
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import EventCommandHistory
from test_calendar_chat import ScriptedProvider, tool
from test_calendar_runtime import EphemeralProtection


URL = 'https://example.invalid'
CONFIG = dict(remote_enabled=True, remote_url=URL, remote_token='synthetic-worker-token')
BOUND_CONFIG = {**CONFIG, 'remote_account': 'owner-one'}
SERVER = remote_scope(URL, CONFIG['remote_token'], BOUND_CONFIG['remote_account'])
ARRIVAL = '2025-12-31T23:55:00+08:00'


def job(identifier, text, sender='test-sender', when=ARRIVAL):
    return dict(msg_id=identifier, openid=sender, text=text, message_time_local=when,
                received_at_utc='2026-01-04T03:00:00+00:00')


def completion(body, *, notify='sent', duplicate=False):
    receipt = json.loads(body)
    return dict(status='done' if receipt['ok'] else 'failed', exec_ok=receipt['ok'],
                operation_id=receipt['operation_id'], notify_status=notify, already_completed=duplicate)


class RemoteServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.protection = EphemeralProtection()
        self.configuration = AIConfiguration(self.folder, self.protection)
        self.executor = SerialExecutor()
        self.addCleanup(self.executor.stop)
        self.store = self.executor(lambda: Store(self.folder / 'calendar.sqlite3',
                                                repository_factory=SqliteCalendarRepository))
        self.addCleanup(lambda: self.executor(self.store.repository.close))
        self.provider = ScriptedProvider([])
        self.refresh = Mock(side_effect=lambda change: self.assertEqual(threading.get_ident(), self.executor.owner))
        self.runtime = CalendarRuntime(self.store, EventCommandHistory(self.store), self.executor,
                                       configuration=self.configuration, provider=self.provider, refresh=self.refresh)
        self.addCleanup(self.runtime.stop)
        self.service = self.runtime.service
        self.ledger = ChatJournal(self.folder / 'remote.sqlite3', self.protection)
        self.client = Mock()
        self.client.request.side_effect = lambda path, token, body: completion(body)
        self.worker = self.worker_for_steps()

    def worker_for_steps(self):
        worker = RemoteWorker(self.service, self.ledger)
        worker.config = URL, CONFIG['remote_token'], SERVER, BOUND_CONFIG['remote_account']
        worker._notification(0, VerifiedAccount(BOUND_CONFIG['remote_account']))
        return worker

    def process(self, message, replies=()):
        self.provider.replies = list(replies)
        record = self.worker._intake(message, SERVER)
        self.worker._handle(record, self.client, CONFIG['remote_token'], SERVER, 0)
        return self.ledger.get(JOBS, record['id'], SERVER)

    def events(self):
        return self.executor(lambda: self.store.repository.list_events())

    def test_disabled_default_and_encrypted_worker_token(self):
        defaults = self.configuration.load()
        self.assertFalse(defaults['remote_enabled'])
        with patch('calendar_runtime.RemoteWorker') as worker:
            self.runtime.apply_remote()
            worker.assert_not_called()
        for change in (dict(remote_url='http://invalid'), dict(remote_token=''), dict(remote_token='bad\nheader')):
            with self.assertRaises(ValueError):
                self.configuration.save({**defaults, **CONFIG, **change})
        self.configuration.save({**defaults, **CONFIG})
        self.assertNotIn(CONFIG['remote_token'].encode(), self.configuration.path.read_bytes())
        self.assertEqual(self.configuration.load()['remote_token'], CONFIG['remote_token'])
        with patch('calendar_runtime.RemoteWorker') as worker:
            self.runtime.apply_remote()
            worker.return_value.configure.assert_called_once()

    def test_public_health_check_does_not_send_token(self):
        from desktop_calendar.remote_service import test_connection
        with patch('desktop_calendar.remote_service.Client') as client:
            client.return_value.request.return_value = {'status': 'ok'}
            self.assertTrue(test_connection(URL))
            client.return_value.request.assert_called_once_with('/')

    def test_date_anchor_and_duplicate_msg_id(self):
        message = job('date-1', '明天开组会')
        first = self.process(message, [tool('preview_create', dict(title='组会', date='2026-01-01'))])
        duplicate = self.process(message)
        self.assertEqual(first['receipt'], duplicate['receipt'])
        self.assertEqual(len(self.provider.messages), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertIn('2026-01-01', self.events()[0].due)
        prompt = json.dumps(self.provider.messages[0], ensure_ascii=False)
        self.assertIn('2025-12-31', prompt)
        self.assertIn('2026-01-01', prompt)
        self.assertIsNotNone(json.loads(first['receipt'])['operation_id'])
        self.assertNotIn('text', first)
        self.refresh.assert_called_once()

    def test_claim_time_fallback_and_no_received_at_substitution(self):
        ts = int(datetime(2026, 1, 1, 16, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(message_time(dict(wechat_create_time=ts)).isoformat(), '2026-01-02T00:01:00+08:00')
        record = self.process(job('invalid-time', '私人测试', when=None))
        self.assertEqual(json.loads(record['receipt'])['error'], 'invalid_job')
        self.assertEqual(self.provider.messages, [])

    def test_one_batch_receipt_covers_multiple_events_and_ddl_type(self):
        plan = tool('propose_events', dict(action='create', items=[
            dict(fields=dict(title='组会', date='2026-01-01')),
            dict(fields=dict(title='交报告', date='2026-01-02', event_type='ddl')),
        ]))
        record = self.process(job('batch', '创建两个独立事项，元旦组会，1月2日交报告，DDL'), [plan])
        receipt = json.loads(record['receipt'])
        self.assertTrue(receipt['ok'])
        self.assertEqual(record['operation_ids'], [receipt['operation_id']])
        self.assertEqual({event.title: event.event_type for event in self.events()}, {'组会': 'general', '交报告': 'ddl'})
        self.assertIn('DDL', receipt['reply_text'])
        self.assertNotIn('#', receipt['reply_text'])
        self.assertNotIn('不提醒', receipt['reply_text'])
        self.assertTrue(self.executor(lambda: self.store.repository.receipt(receipt['operation_id'])))
        self.refresh.assert_called_once()

    def test_delete_transcript_one_confirmation_isolation_and_redelivery(self):
        self.process(job('seed-delete', '明天上午10点开组会'),
                     [tool('preview_create', dict(title='组会', date='2026-01-01', time='10:00'))])
        event_id = self.events()[0].id
        plan = tool('propose_events', dict(action='delete', target=dict(
            start='2026-01-01', end='2026-01-01', titles=['组会'], quantity='one')))
        pending = self.process(job('delete-review', '明天组会删了'), [plan])
        reply = json.loads(pending['receipt'])
        self.assertIsNone(reply['operation_id'])
        self.assertIn('组会 · 10:00', reply['reply_text'])
        self.assertIn('确认删除', reply['reply_text'])
        self.assertNotIn(event_id, reply['reply_text'])
        foreign = self.process(job('foreign-confirm', '确认删除', sender='another-sender'))
        self.assertNotEqual(foreign['session_id'], pending['session_id'])
        self.assertEqual(len(self.events()), 1)
        confirmation = job('delete-confirm', '删除')
        deleted = self.process(confirmation)
        duplicate = self.process(confirmation)
        result = json.loads(deleted['receipt'])
        self.assertEqual(deleted['receipt'], duplicate['receipt'])
        self.assertIn('已删除 1 项', result['reply_text'])
        self.assertNotIn(event_id, result['reply_text'])
        self.assertTrue(self.executor(lambda: self.store.repository.receipt(result['operation_id'])))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.refresh.call_count, 2)

    def test_wechat_keeps_explicit_fields_and_reports_unverified_effects(self):
        plan = tool('propose_events', dict(action='create', items=[dict(fields=dict(
            title='色彩验收', date='2026-01-01', time='10:00', color='#223344', reminder=15))]))
        record = self.process(job('explicit-options', '明天上午10点创建色彩验收，颜色#223344，提前15分钟提醒'), [plan])
        reply = json.loads(record['receipt'])['reply_text']
        self.assertIn('#223344', reply)
        self.assertIn('提前 15 分钟提醒', reply)
        result = dict(action='delete', deleted=False, committed=True, goal_satisfied=False)
        body, _ = make_receipt(dict(status='committed_unverified', operation_result=result,
                                    error_message='删除已提交，但目标效果核验未通过。'))
        self.assertNotIn('已删除', json.loads(body)['reply_text'])
        self.assertFalse(json.loads(body)['ok'])

    def test_pending_and_confirmation_isolated_and_keep_original_dates(self):
        plan = tool('propose_events', {'action': 'create', 'items': [
            {'fields': {'title': 'WB实验', 'date': '2026-01-01', 'end_date': '2026-01-02'}},
        ]})
        pending = self.process(job('first', '明后天做WB实验'), [plan])
        receipt = json.loads(pending['receipt'])
        self.assertIsNone(receipt['operation_id'])
        self.assertIn('2026-01-01', receipt['reply_text'])
        self.assertIn('2026-01-02', receipt['reply_text'])
        self.assertEqual(self.events(), [])
        second = self.process(job('second', '确认', sender='another-sender'))
        self.assertNotEqual(pending['session_id'], second['session_id'])
        self.assertEqual(self.service.list_sessions(OWNER), [])
        self.assertEqual(self.events(), [])
        confirmed = self.process(job('third', '可以', when='2026-01-02T12:00:00+08:00'))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0].duration_days, 2)
        self.assertIn('2026-01-01', self.events()[0].due)
        self.assertEqual(confirmed['session_id'], pending['session_id'])
        self.assertEqual(len(self.provider.messages), 1)

    def test_query_clarification_and_numbered_candidates_do_not_claim_write(self):
        for identifier, day in [('a', '2026-01-01'), ('b', '2026-01-03')]:
            self.process(job(identifier, '记一项组会'), [tool('preview_create', dict(title='组会', date=day))])
        before = len(self.events())
        query = self.process(job('query', '查询组会'), [tool('query_events', {'keyword': '组会'})])
        receipt = json.loads(query['receipt'])
        self.assertIn('1. 2026-01-01', receipt['reply_text'])
        self.assertIn('2. 2026-01-03', receipt['reply_text'])
        self.assertIsNone(receipt['operation_id'])
        clarify = self.process(job('clarify', '请问'), [tool('clarify', {'question': '请明确日期。'})])
        self.assertEqual(json.loads(clarify['receipt'])['reply_text'], '请明确日期。')
        self.assertEqual(len(self.events()), before)

    def test_upload_timeout_restart_reuses_exact_receipt_and_notification_failure_keeps_write(self):
        self.client.request.side_effect = Unavailable
        record = self.process(job('timeout', '添加组会'), [tool('preview_create', dict(title='组会', date='2026-01-01'))])
        self.assertEqual(record['state'], 'ready')
        sent = self.client.request.call_args
        self.ledger = ChatJournal(self.ledger.path, self.protection)
        restarted = self.worker_for_steps()
        self.client.request.side_effect = lambda path, token, body: completion(body, notify='failed', duplicate=True)
        self.assertTrue(restarted._handle(record, self.client, CONFIG['remote_token'], SERVER, 0))
        self.assertEqual(self.client.request.call_args, sent)
        final = self.ledger.get(JOBS, record['id'], SERVER)
        self.assertEqual((final['state'], final['notify_status']), ('submitted', 'failed'))
        self.assertEqual(len(self.provider.messages), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertIn('通知', restarted.notice)

    def test_crash_after_intake_can_enter_agent_on_restart(self):
        record = self.worker._intake(job('received', '添加待办'), SERVER)
        self.provider.replies = [tool('preview_create', dict(title='待办', date='2026-01-01'))]
        self.assertTrue(self.worker_for_steps()._handle(record, self.client, CONFIG['remote_token'], SERVER, 0))
        self.assertEqual(len(self.events()), 1)

    def test_unknown_execution_is_not_replayed_on_restart(self):
        record = self.worker._intake(job('interrupted', '添加待办'), SERVER)
        record.update(state='executing', session_id=self.service.new_session(record['owner'])['session_id'])
        self.ledger.put(JOBS, record['id'], SERVER, record)
        self.assertTrue(self.worker_for_steps()._handle(record, self.client, CONFIG['remote_token'], SERVER, 0))
        self.assertEqual(self.ledger.get(JOBS, record['id'], SERVER)['state'], 'uncertain')
        self.client.request.assert_not_called()
        self.assertEqual(self.provider.messages, [])

    def test_recovers_commit_when_agent_response_and_remote_receipt_missing(self):
        plan = tool('propose_events', {'action': 'create', 'items': [
            {'fields': {'title': 'WB', 'date': '2026-01-01', 'end_date': '2026-01-02'}},
        ]})
        self.process(job('plan', '明后天做WB'), [plan])
        confirm = self.worker._intake(job('confirm', '可以'), SERVER)
        original_write = self.service.journal.put

        def interrupt(kind, identifier, owner, payload):
            if kind == 'operation' and payload.get('status') == 'succeeded':
                raise OSError('simulated loss before publishing Agent response')
            return original_write(kind, identifier, owner, payload)

        original_transition = self.service.journal.transition

        def interrupt_recovery(kind, identifier, owner, before, after):
            if kind == 'operation' and before.get('status') == 'executing' and after['status'] == 'succeeded':
                raise OSError('simulated loss before publishing recovery')
            return original_transition(kind, identifier, owner, before, after)

        with patch.object(self.service.journal, 'put', side_effect=interrupt), \
                patch.object(self.service.journal, 'transition', side_effect=interrupt_recovery):
            with self.assertRaises(OSError):
                self.worker._handle(confirm, self.client, CONFIG['remote_token'], SERVER, 0)
        self.assertEqual(len(self.events()), 1)
        operation = self.service.journal.all('operation', confirm['owner'])[0]
        self.assertEqual(operation['status'], 'executing')
        self.assertEqual(operation['execution_request_id'], confirm['request_id'])
        self.assertTrue(self.executor(lambda: self.store.repository.receipt(operation['operation_id'])))
        self.assertTrue(self.worker_for_steps()._handle(confirm, self.client, CONFIG['remote_token'], SERVER, 0))
        receipt = json.loads(self.ledger.get(JOBS, confirm['id'], SERVER)['receipt'])
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['operation_id'], operation['operation_id'])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.provider.messages), 1)

    def test_ready_without_commit_is_never_resumed_by_remote_recovery(self):
        record = self.worker._intake(job('ready-operation', '添加待办'), SERVER)
        self.provider.replies = [tool('preview_create', dict(title='待办', date='2026-01-01'))]
        with patch.object(self.service.gateway, 'execute', side_effect=OSError('before owner-thread dispatch')), \
                patch.object(self.service, 'recover_message', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                self.worker._handle(record, self.client, CONFIG['remote_token'], SERVER, 0)
        self.assertTrue(self.worker_for_steps()._handle(record, self.client, CONFIG['remote_token'], SERVER, 0))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.ledger.get(JOBS, record['id'], SERVER)['state'], 'uncertain')

    def test_runtime_polls_error_empty_and_stops_without_duplicate_threads(self):
        calls = []
        claim_entered = threading.Event()
        release_claim = threading.Event()
        claim_count = 0

        def request(path, token=None):
            nonlocal claim_count
            calls.append(path)
            if path == '/':
                return {'status': 'ok'}
            claim_count += 1
            if claim_count == 1:
                raise Unavailable
            if claim_count == 2:
                return {}
            if claim_count == 4:
                claim_entered.set()
                release_claim.wait(3)
            return {'job': None}

        client = Mock()
        client.request.side_effect = request
        from test_remote_notifications import MockNotifications
        worker = RemoteWorker(self.service, self.ledger, client_factory=lambda _url: client, interval=0.02,
                              fallback_grace=0, notification_factory=MockNotifications)
        worker.configure({'remote_enabled': False})
        self.assertIsNone(worker.thread)
        worker.configure(BOUND_CONFIG)
        thread = worker.thread
        worker.configure(BOUND_CONFIG)
        self.assertIs(worker.thread, thread)
        try:
            self.assertTrue(claim_entered.wait(3))
            worker.configure({'remote_enabled': False})
            release_claim.set()
            time.sleep(0.07)
            self.assertEqual(calls, [CLAIM_PATH, '/', CLAIM_PATH, '/', CLAIM_PATH, CLAIM_PATH])
        finally:
            release_claim.set()
            worker.stop()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_operation_ids_and_reply_length(self):
        response = dict(status='confirmation_required', assistant_message='请核对', pending_operation=dict(
            status='pending', operation_id='pending-id', preview={'summaries': [{'title': '事项' * 900}]}))
        body, ids = make_receipt(response)
        receipt = json.loads(body)
        self.assertEqual(ids, ['pending-id'])
        self.assertIsNone(receipt['operation_id'])
        self.assertLessEqual(len(receipt['reply_text'].encode()), 1900)
        self.assertIn('确认', receipt['reply_text'])
