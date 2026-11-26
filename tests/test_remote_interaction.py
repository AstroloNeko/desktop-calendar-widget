from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from calendar_core import Event
from desktop_calendar.assistant.calendar_chat_protocol import fingerprint
from desktop_calendar.remote_notifications import VerifiedAccount
from desktop_calendar.remote_cloud import remote_scope
from desktop_calendar.remote_service import JOBS, RemoteWorker, message_time
import test_remote_service as fixture


ACCOUNT = 'owner-one'
KEY = 'qq:123456789:synthetic-qq-user'
TIMESTAMP = int(datetime(2025, 12, 31, 16, tzinfo=timezone.utc).timestamp())


class RemoteInteractionTests(unittest.TestCase):
    setUp = fixture.RemoteServiceTests.setUp
    worker_for_steps = fixture.RemoteServiceTests.worker_for_steps
    process = fixture.RemoteServiceTests.process
    events = fixture.RemoteServiceTests.events

    def native(self, identifier, text='', *, key=KEY, channel='qq', app='123456789', context=None):
        job = dict(msg_id=identifier, text=text, owner_id=ACCOUNT, channel=channel,
                   channel_app_id=app, conversation_key=key, created_at_ts=TIMESTAMP)
        if context is not None:
            job['interaction'] = context
        return job

    def enable(self):
        self.worker._notification(0, VerifiedAccount(ACCOUNT, '演示账号', 1,
                                  ('business_receipt', 'action', 'reference', 'quick_query')))

    def quick_query(self, **changes):
        return dict(schema_version=1, kind='quick_query', command='today', start='2026-01-02',
                    end='2026-01-02', ddl_only=False) | changes

    def data(self, job, replies=()):
        return json.loads(self.process(job, replies)['receipt'])

    def event(self, identifier, title, date='2026-01-02'):
        self.executor(lambda: self.runtime.gateway.commands.upsert(Event(identifier, title, date+'T10:00')))

    def create(self, *, preview=False):
        return fixture.tool('propose_events', dict(action='create', preview_only=preview,
            items=[dict(fields=dict(title='组会', date='2026-01-02'))]))

    def click(self, identifier, source, receipt, kind, **route):
        action = next(item for item in receipt['business_receipt']['available_actions'] if item['kind'] == kind)
        return self.native(identifier, context=dict(schema_version=1, kind='action',
            source_msg_id=source, action_id=action['id']), **route)

    def test_quick_queries_use_local_gateway_with_text_receipts_and_revision_targets(self):
        self.enable()
        self.event('first', '事项一')
        self.event('second', '事项二', '2026-01-03')
        self.executor(lambda: self.runtime.gateway.commands.upsert(
            Event('due', '交报告', '2026-01-03T17:00', event_type='ddl')))
        queries = (
            ('today', {}, ['事项一']),
            ('tomorrow', dict(start='2026-01-03', end='2026-01-03'), ['交报告', '事项二']),
            ('week', dict(end='2026-01-05'), ['事项一', '交报告', '事项二']),
            ('ddl', dict(end='2026-01-04', ddl_only=True, done=False), ['交报告']),
            ('tomorrow', dict(start='2026-01-04', end='2026-01-04', done=True), []),
        )
        with patch.object(self.service, 'send_message', side_effect=AssertionError('query went through Agent')):
            for index, (command, filters, titles) in enumerate(queries):
                with self.subTest(command=command, filters=filters):
                    row = self.process(self.native(f'quick-{index}', context=self.quick_query(command=command, **filters)))
                    body = json.loads(row['receipt'])
                    receipt = body['business_receipt']
                    self.assertTrue(body['ok'])
                    self.assertEqual(receipt['result_type'], 'query')
                    self.assertEqual([event['title'] for event in receipt['events']], titles)
                    self.assertEqual([event['index'] for event in receipt['events']], list(range(1, len(titles) + 1)))
                    self.assertTrue(all(title in body['reply_text'] for title in titles))
                    self.assertEqual(bool(receipt.get('reference_until')), bool(titles))
                    self.assertEqual(len(row['interaction_reply']['targets']), len(titles))
                    self.assertTrue(all(target['revision'] for target in row['interaction_reply']['targets']))
        self.assertEqual(self.provider.messages, [])
        self.executor(lambda: self.runtime.gateway.commands.delete('first'))
        stale = self.native('quick-stale-ref', '把第一项改到下午三点',
                            context=dict(schema_version=1, kind='reference', source_msg_id='quick-0'))
        self.assertEqual(self.data(stale)['error'], 'reference_conflict')
        self.assertEqual(self.provider.messages, [])

    def test_invalid_quick_queries_cannot_fall_through_to_model(self):
        self.enable()
        invalid = [self.quick_query(**changes) for changes in (
            dict(command='help'), dict(start='20260102'), dict(start='2026-02-29'),
            dict(end='2026-01-01'), dict(end='2028-01-01'), dict(ddl_only=1),
            dict(done='false'), dict(schema_version=True), dict(source_msg_id='extra'),
        )]
        missing = self.quick_query()
        del missing['end']
        for index, context in enumerate([*invalid, missing]):
            with self.subTest(context=context):
                self.assertEqual(self.data(self.native(f'invalid-quick-{index}', '创建事项', context=context))
                                 ['error'], 'invalid_job')
        self.assertEqual(self.provider.messages, [])
        self.assertFalse(self.events())

    def test_capability_disabled_rejects_structured_jobs_but_accepts_cloud_text_fallback(self):
        self.enable()
        claimed = self.worker._intake(self.native('queued-quick', context=self.quick_query()), fixture.SERVER)
        self.worker._notification(0, VerifiedAccount(ACCOUNT))
        self.worker._handle(claimed, self.client, fixture.CONFIG['remote_token'], fixture.SERVER, 0)
        self.assertEqual(json.loads(self.ledger.get(JOBS, claimed['id'], fixture.SERVER)['receipt'])
                         ['error'], 'action_expired')
        for index, context in enumerate((self.quick_query(), dict(schema_version=2, kind='reference'),
                                         dict(schema_version=1, kind='future'))):
            response = self.data(self.native(f'unsupported-{index}', '创建事项', context=context))
            self.assertEqual(response['error'], 'invalid_job')
            self.assertNotIn('business_receipt', response)
        self.assertEqual(self.provider.messages, [])
        fallback = self.data(self.native('fallback', '查询今天的日程'), [
            fixture.tool('query_events', dict(start='2026-01-01', end='2026-01-01'))])
        self.assertTrue(fallback['ok'])
        self.assertNotIn('business_receipt', fallback)
        self.assertTrue(self.provider.messages)
        self.assertFalse(self.events())

    def test_native_qq_preserves_published_owner_pending_and_old_receipt_bytes(self):
        original = fixture.job('old-preview', '先预览创建组会', sender=KEY)
        saved = self.process(original, [self.create(preview=True)])
        saved.pop('channel')
        saved.pop('route')
        self.worker._save(fixture.SERVER, saved)
        owner = 'wechat-verified-' + fingerprint([fixture.SERVER, KEY])
        self.assertEqual(saved['owner'], owner)
        self.enable()
        native = self.native('new-confirm', '确认')
        self.assertEqual(self.process(native)['session_id'], saved['session_id'])
        self.assertEqual(len(self.service.list_sessions(owner)), 1)
        self.assertEqual(self.events()[0].title, '组会')
        redelivered = {**self.native('old-preview', '先预览创建组会'), 'openid': KEY}
        self.assertEqual(self.process(redelivered)['receipt'], saved['receipt'])
        self.assertEqual(len(self.events()), 1)

    def test_production_native_fields_are_used_without_negotiating_receipts(self):
        self.assertEqual(self.worker.snapshot().account_name, '')
        plain = self.data(self.native('native-plain', '明天开组会'), [self.create()])
        self.assertNotIn('business_receipt', plain)
        self.assertIn('已创建', plain['reply_text'])
        self.assertEqual(message_time(self.native('timestamp')).isoformat(), '2026-01-01T00:00:00+08:00')
        for change in (dict(channel_app_id='999'), dict(openid='different-user'), dict(created_at_ts=True)):
            invalid = self.data({**self.native('bad-'+next(iter(change)), '添加组会'), **change})
            self.assertEqual(invalid['error'], 'invalid_job')
        self.assertEqual(len(self.events()), 1)

    def test_button_confirms_once_and_undo_is_operation_bound(self):
        self.enable()
        pending = self.data(self.native('pending', '先预览创建组会'), [self.create(preview=True)])
        self.assertEqual(pending['business_receipt']['result_type'], 'confirmation')
        self.assertFalse(self.events())
        wrong = self.click('foreign-thread', 'pending', pending, 'confirm', key='qq:123456789:another')
        self.assertEqual(self.data(wrong)['error'], 'reference_conflict')
        clicked = self.click('confirm-new', 'pending', pending, 'confirm')
        done = self.data(clicked)
        self.assertEqual(done['business_receipt']['result_type'], 'create')
        self.assertEqual(done['business_receipt']['events'][0]['title'], self.events()[0].title)
        self.assertEqual(self.data(clicked), done)
        self.assertEqual(self.data({**clicked, 'msg_id': 'duplicate-click'})['error'], 'action_already_used')
        undo = self.click('undo-actual', 'confirm-new', done, 'undo')
        self.assertEqual(self.data(undo)['business_receipt']['result_type'], 'undo')
        self.assertFalse(self.events())
        self.assertEqual(self.data({**undo,'msg_id':'undo-again'})['error'], 'action_already_used')

    def test_echoed_button_expiration_and_cancel_block_confirm(self):
        self.enable()
        self.service.clock = lambda: 2000
        pending = self.data(self.native('cancel-origin', '先预览创建组会'), [self.create(preview=True)])
        cancel = self.click('cancel-button','cancel-origin',pending,'cancel')
        self.assertEqual(self.data(cancel)['business_receipt']['result_type'], 'cancelled')
        self.assertEqual(self.data(self.click('cancel-confirm','cancel-origin',pending,'confirm'))['error'],
                         'operation_conflict')
        second = self.data(self.native('exp-origin','先预览创建组会'), [self.create(preview=True)])
        self.service.clock = lambda: 3000
        self.assertEqual(self.data(self.click('exp-confirm','exp-origin',second,'confirm'))['error'], 'reference_conflict')
        self.assertFalse(self.events())

    def test_separate_app_reference_is_rejected_and_reference_locks_event(self):
        self.enable()
        for identifier, title in (('first', '事项一'), ('second', '事项二')):
            self.event(identifier, title)
        query = fixture.tool('query_events', dict(start='2026-01-02', end='2026-01-02'))
        listed = self.data(self.native('origin-query', '查具体事项'), [query])
        self.assertEqual(listed['business_receipt']['result_type'], 'query')
        target = self.events()[1]
        self.assertEqual(listed['business_receipt']['events'][1]['title'], target.title)
        context = dict(schema_version=1, kind='reference', source_msg_id='origin-query')
        cross = self.native('app-cross','把第二个改到下午三点', key='qq:999:synthetic-qq-user', app='999', context=context)
        self.assertEqual(self.data(cross)['error'], 'reference_conflict')
        other = next(event for event in self.events() if event.id != target.id)
        change = fixture.tool('preview_update', dict(event_id=target.id, changes={'time':'15:00'}))
        fake_plan = fixture.tool('preview_update', dict(event_id=other.id, changes={'time':'15:00'}))
        wrong = self.data(self.native('bad-ref-target','把第二个改到下午三点', context=context), [fake_plan])
        self.assertFalse(wrong['ok'])
        preview = self.data(self.native('reference-change','把第二个改到下午三点', context=context), [change])
        self.assertEqual(preview['business_receipt']['result_type'], 'confirmation')
        self.assertTrue(all(event.due.endswith('10:00') for event in self.events()))
        done = self.data(self.click('reference-confirm','reference-change',preview,'confirm'))
        self.assertEqual(done['business_receipt']['result_type'], 'update')
        self.assertTrue(next(event for event in self.events() if event.id == target.id).due.endswith('15:00'))
        stale = self.data(self.native('stale-ref','把第二个改到下午四点',context=context), [change])
        self.assertEqual(stale['error'], 'reference_conflict')

    def test_reference_to_deleted_or_expired_list_fails_without_calling_model(self):
        self.enable()
        self.service.clock = lambda: 2000
        self.event('target', '原事项')
        query = fixture.tool('query_events', dict(start='2026-01-02', end='2026-01-02'))
        self.data(self.native('old-list', '查询事项'), [query])
        context = dict(schema_version=1, kind='reference', source_msg_id='old-list')
        self.assertEqual(self.data(self.native('bad-reference-number', '把第0个改到下午三点', context=context))
                         ['error'], 'reference_conflict')
        self.executor(lambda: self.runtime.gateway.commands.delete('target'))
        calls = len(self.provider.messages)
        gone = self.data(self.native('deleted-list-target', '改到下午三点', context=context))
        self.assertEqual(gone['error'], 'reference_conflict')
        self.service.clock = lambda: 3000
        stale = self.data(self.native('expired-list-target', '改到下午三点', context=context))
        self.assertEqual(stale['error'], 'reference_conflict')
        self.assertEqual(len(self.provider.messages), calls)
        self.assertFalse(self.events())

    def test_high_risk_buttons_keep_both_confirmation_stages(self):
        self.enable()
        for index in range(6):
            self.event(f'batch-{index}', '批量测试')
        plan = fixture.tool('propose_events', dict(action='delete', target=dict(start='2026-01-02',
            end='2026-01-02', titles=[], quantity='all_matches')))
        pending = self.data(self.native('batch-original', '删除2026-01-02的全部事项'), [plan])
        first = self.click('review-stage','batch-original',pending,'confirm')
        second = self.data(first)
        self.assertEqual(len(self.events()), 6)
        self.assertEqual(second['business_receipt']['pending_confirmation']['stage'], 'awaiting_final_confirmation')
        self.assertFalse(self.data({**first,'msg_id':'replay-first'})['ok'])
        self.assertEqual(self.data(self.click('final-stage','review-stage',second,'confirm'))
                         ['business_receipt']['result_type'], 'delete')
        self.assertFalse(self.events())

    def test_undo_cannot_revert_a_later_local_change(self):
        self.enable()
        created = self.data(self.native('undo-origin','创建组会'), [self.create()])
        self.event('local','本地事项')
        result = self.data(self.click('undo-stale','undo-origin',created,'undo'))
        self.assertEqual(result['error'], 'operation_conflict')
        self.assertEqual({event.title for event in self.events()}, {'组会', '本地事项'})

    def test_exact_natural_undo_requires_latest_same_conversation_receipt(self):
        self.enable()
        self.data(self.native('text-undo-origin','创建组会'), [self.create()])
        result = self.data(self.native('text-undo', '撤销上一步'))
        self.assertEqual(result['business_receipt']['result_type'], 'undo')
        self.assertFalse(self.events())
        self.assertFalse(self.data(self.native('text-undo-again','撤销'))['ok'])

    def test_cross_account_click_cannot_reference_first_accounts_outbox(self):
        self.enable()
        pending = self.data(self.native('first-account','先预览创建组会'), [self.create(preview=True)])
        foreign = self.click('cross-account','first-account',pending,'confirm')
        token = 'second-synthetic-token'
        scope = remote_scope(fixture.URL, token, 'owner-two')
        worker = RemoteWorker(self.service, self.ledger)
        worker.config = fixture.URL, token, scope, 'owner-two'
        worker._notification(0, VerifiedAccount('owner-two', interaction_version=1))
        foreign['owner_id'] = 'owner-two'
        record = worker._intake(foreign, scope)
        self.assertTrue(worker._handle(record, self.client, token, scope, 0))
        receipt = json.loads(self.ledger.get('wechat-job', record['id'], scope)['receipt'])
        self.assertEqual(receipt['error'], 'reference_conflict')
        self.assertFalse(self.events())

    def test_candidate_button_uses_current_numbered_candidate_revision(self):
        self.enable()
        self.event('duplicate-one', '同名事项')
        self.event('duplicate-two', '同名事项')
        plan = fixture.tool('propose_events', dict(action='update', target=dict(start='2026-01-02',
            end='2026-01-02', quantity='one', titles=['同名事项']), fields={'time':'15:00'}))
        card = self.data(self.native('candidate-origin','把同名事项改到下午三点'), [plan])
        self.assertEqual(card['business_receipt']['result_type'], 'candidates')
        action = next(item for item in card['business_receipt']['available_actions'] if item['index'] == 2)
        context = dict(schema_version=1, kind='action', source_msg_id='candidate-origin', action_id=action['id'])
        result = self.data(self.native('choose-two', context=context))
        self.assertEqual(result['business_receipt']['result_type'], 'update')
        self.assertEqual([event.due[-5:] for event in self.events()], ['10:00','15:00'])
        self.assertFalse(self.data(self.native('choose-two-again', context=context))['ok'])

    def test_unknown_version_never_falls_back_to_executing_button_text(self):
        self.enable()
        self.data(self.native('unknown-version-origin','先预览组会'), [self.create(preview=True)])
        context = dict(schema_version=2, kind='action', source_msg_id='unknown-version-origin', action_id='A'*32)
        bad = self.data(self.native('unknown-version', '确认', context=context))
        self.assertEqual(bad['error'], 'invalid_job')
        self.assertFalse(self.events())

    def test_published_config_is_read_in_place_without_losing_credentials(self):
        saved = {**self.configuration.defaults(), **fixture.BOUND_CONFIG, 'token':'synthetic-loopback-token'}
        saved.pop('remote_display_name')
        self.configuration.path.write_bytes(self.protection.encrypt(json.dumps(saved).encode()))
        before = self.configuration.path.read_bytes()
        again = self.configuration.load()
        self.assertEqual(again['remote_account'], ACCOUNT)
        self.assertEqual(again['remote_token'], saved['remote_token'])
        self.assertEqual(again['token'], saved['token'])
        self.assertEqual(again['remote_display_name'], '')
        self.assertEqual(self.configuration.path.read_bytes(), before)
