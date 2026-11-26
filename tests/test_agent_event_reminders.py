"""Phase B through the actual Agent, command history, and SQLite."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from calendar_core import Event, Store
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.reminders.reminder_engine import ReminderEngine
from desktop_calendar.reminders.reminder_rules import stored_event_rule
from desktop_calendar.remote_service import make_receipt
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import CommandConflictError, EventCommandHistory
from test_calendar_chat import ScriptedProvider, TestProtection, tool


class AgentEventReminderTests(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = Store(root/'calendar.sqlite3', repository_factory=SqliteCalendarRepository)
        self.addCleanup(self.store.repository.close)
        self.history = EventCommandHistory(self.store)
        self.backend = Mock()
        self.engine = ReminderEngine(self.store, self.backend)
        self.engine.configure({'enabled': True, 'time': '08:45'})
        self.now = datetime(2026,11,26,10)
        self.gateway = CalendarActionGateway(self.store, self.history, reminders=self.engine,
                                             reminder_now=lambda: self.now)
        self.provider = ScriptedProvider([])
        self.service = CalendarChatService(self.gateway, ChatJournal(root/'journal.sqlite3',TestProtection()),
                                           self.provider,clock=lambda: self.now.timestamp())
        self.session = self.service.new_session()['session_id']
        self.turn = 0

    def send(self, message, *tools):
        self.turn += 1
        self.provider.replies = list(tools)
        return self.service.send_message(self.session,message,f'reminder-request-{self.turn}')

    def create(self, message, *fields, **options):
        return self.send(message,tool('propose_events',dict(action='create',
                         items=[{'fields':item} for item in fields], **options)))

    def update(self, message, title, fields, **options):
        target = dict(start='2026-11-27',end='2026-11-27',titles=[title],quantity='one')
        return self.send(message,tool('propose_events',dict(action='update',target=target,fields=fields,**options)))

    def seed(self, *events):
        for event in events:
            self.gateway.events.upsert(event)
        self.history.clear()

    def result(self, response, mode, at):
        self.assertEqual(response['status'],'succeeded',response.get('error_message'))
        event = response['operation_result']['event']
        plan = event['reminder_plan']
        self.assertEqual((plan['mode'],plan['planned_at'],plan['schedule_status']),(mode,at,'recalculated'))
        self.assertEqual(self.gateway.detail(event['id']),event)
        return event

    def test_timed_meeting_and_timed_ddl_use_the_actual_anchor_clock(self):
        future = '2026-11-27'
        meeting = self.result(self.create('明天下午三点开组会，提前15分钟提醒我。',
                              dict(title='组会',date=future,time='15:00',reminder_rule=dict(mode='relative',minutes=15))),
                              'relative',future+'T14:45')
        self.assertEqual(meeting['time'],'15:00')
        ddl = self.result(self.create('周五下午五点交报告，提前两天提醒。',
                          dict(title='报告',date=future,time='17:00',event_type='ddl',
                               reminder_rule=dict(mode='relative',minutes=2880))),
                          'relative','2026-11-25T17:00')
        self.assertEqual((ddl['deadline'],ddl['time']),(future,'17:00'))
        self.assertTrue(ddl['reminder_plan']['past'])
        self.assertTrue(ddl['reminder_plan']['catchup_eligible'])
        key = self.engine.plans[ddl['id']].key
        same_lead = self.update('把报告的提醒改成提前两天。','报告',
                                {'reminder_rule':{'mode':'relative','days':2}})
        row = self.result(same_lead,'relative','2026-11-25T17:00')
        self.assertEqual(self.engine.plans[row['id']].key,key)

    def test_absolute_reminder_does_not_invent_start_time_or_move_with_event(self):
        response = self.create('明早九点提醒我跑HPLC。',dict(
            title='跑HPLC',date='2026-11-27',reminder_rule=dict(mode='absolute',at='2026-11-27T09:00')))
        row = self.result(response,'absolute','2026-11-27T09:00')
        self.assertIsNone(row['time'])
        old_key = self.engine.plans[row['id']].key
        moved = self.update('把跑HPLC改到周六。','跑HPLC',dict(date='2026-11-28'))
        row = self.result(moved,'absolute','2026-11-27T09:00')
        self.assertEqual((row['date'],row['time']),('2026-11-28',None))
        self.assertEqual(self.engine.plans[row['id']].key,old_key)

    def test_untimed_ddl_accepts_two_and_five_days_without_inventing_a_deadline_clock(self):
        for days, day in ((2,'2026-11-25'),(5,'2026-11-22')):
            row = self.result(self.create(f'周五交报告，提前{days}天提醒。',
                              dict(title=f'报告{days}',date='2026-11-27',event_type='ddl',
                                   reminder_rule=dict(mode='relative',days=days))),
                              'relative',day+'T08:45')
            self.assertIsNone(row['time'])
            self.assertTrue(row['reminder_plan']['past'])
            self.assertFalse(row['reminder_plan']['catchup_eligible'])
        explicit = self.result(self.create('周五交报告，周四晚上八点提醒我。',
                               dict(title='报告',date='2026-11-27',event_type='ddl',
                                    reminder_rule=dict(mode='absolute',at='2026-11-26T20:00'))),
                               'absolute','2026-11-26T20:00')
        self.assertIsNone(explicit['time'])

    def test_old_override_is_replaced_and_default_does_not_restore_old_minutes(self):
        self.seed(Event('meeting','组会','2026-11-27T15:00',reminder=60))
        self.engine.configure(dict(self.engine.config(),events={'meeting':{'mode':'off'}}))
        row = self.result(self.update('把组会的提醒改成提前半小时。','组会',
                          {'reminder_rule':{'mode':'relative','minutes':30}}),'relative','2026-11-27T14:30')
        self.assertEqual(row['time'],'15:00')
        self.engine.configure(dict(self.engine.config(),events={'meeting':{'mode':'custom','days':5,'time':'07:00'}}))
        row = self.result(self.update('把组会的提醒改到下午两点。','组会',
                          {'reminder_rule':{'mode':'absolute','time':'14:00'}}),'absolute','2026-11-22T14:00')
        self.assertEqual(row['time'],'15:00')
        self.result(self.update('组会不用提醒了。','组会',{'reminder_rule':{'mode':'disabled'}}),'disabled',None)
        row = self.result(self.update('组会恢复默认提醒。','组会',{'reminder_rule':{'mode':'default'}}),
                          'default','2026-11-27T08:45')
        self.assertIsNone(row['reminder'])
        self.assertEqual(stored_event_rule(self.store.settings,'meeting'),{'mode':'default'})
        self.gateway.undo_remote_operation(self.history._undo[-1].operation_id,'undo-default')
        self.assertEqual(stored_event_rule(self.store.settings,'meeting'),{'mode':'off'})
        self.assertIsNotNone(self.store.repository.receipt('undo-default'))

    def test_same_title_confirm_uses_selected_revision_and_rechecks_rule(self):
        self.seed(Event('first','组会','2026-11-27T12:00'),Event('second','组会','2026-11-27T15:00'))
        response = self.update('其中一个组会提前半小时提醒，先预览。','组会',
                               {'reminder_rule':{'mode':'relative','minutes':30}},preview_only=True)
        self.assertEqual([row['id'] for row in response['candidates']],['first','second'])
        selected = self.service.select_candidate(self.session,'second',revision=response['selection_revision'])
        pending = selected['pending_operation']
        self.assertEqual(pending['preview']['after_summary']['reminder_plan']['planned_at'],'2026-11-27T14:30')
        self.engine.configure(dict(self.engine.config(),events={'second':{'mode':'off'}}))
        denied = self.service.confirm_operation(self.session,pending['operation_id'],revision=pending['revision'])
        self.assertEqual(denied['error_code'],'operation_conflict')
        self.assertIsNone(stored_event_rule(self.store.settings,'first'))
        self.assertEqual(stored_event_rule(self.store.settings,'second'),{'mode':'off'})

    def test_batch_event_and_rule_rollback_and_undo_are_one_transaction(self):
        fields = [
            dict(title='报告',date='2026-11-27',event_type='ddl',reminder_rule=dict(mode='relative',days=2)),
            dict(title='实验',date='2026-11-28',reminder_rule=dict(mode='absolute',at='2026-11-28T09:00')),
        ]
        with patch.object(self.store.repository,'_commit',side_effect=OSError('before commit')):
            failed = self.create('添加两个日程和提醒。',*fields)
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(self.store.events,[])
        self.assertEqual(self.store.repository.read_document()['settings']['native_reminders']['events'],{})
        self.assertEqual(self.history.undo_count,0)
        response = self.create('添加两个日程和提醒。',*fields)
        self.assertEqual(response['status'],'succeeded',response.get('error_message'))
        result = response['operation_result']
        self.assertEqual(len(result['events']),2)
        proof = self.store.repository.receipt(result['operation_id'])
        self.assertIn('preference_updates',proof['operation_type'])
        self.assertSetEqual(set(json.loads(proof['affected_ids'])['events']),set(result['event_ids']))
        self.history.undo()
        self.assertEqual(self.store.events,[])
        self.assertEqual(self.engine.config()['events'],{})
        self.history.redo()
        self.assertEqual(len(self.engine.config()['events']),2)
        events = dict(self.engine.config()['events'])
        events[result['event_ids'][0]] = {'mode':'off'}
        self.engine.configure(dict(self.engine.config(),events=events))
        with self.assertRaises(CommandConflictError):
            self.history.undo()
        self.assertEqual(self.history.undo_count,1)

    def test_replan_failure_is_reported_retries_without_model_and_invalidates_old_notice(self):
        self.seed(Event('meeting','组会','2026-11-27T15:00',reminder=15))
        self.now = datetime(2026,11,27,14,46)
        self.engine.tick(self.now)
        self.assertTrue(self.engine.delivery.current_notices)
        with patch.object(self.engine,'_rebuild',side_effect=OSError('rebuild failed')):
            response = self.update('组会不用提醒了。','组会',{'reminder_rule':{'mode':'disabled'}})
        self.assertEqual(response['status'],'succeeded')
        self.assertTrue(response['operation_result']['committed'])
        self.assertEqual(response['operation_result']['event']['reminder_plan']['schedule_status'],'pending')
        self.assertIn('等待调度重算',response['assistant_message'])
        self.assertFalse(self.engine.delivery.current_notices)
        self.assertTrue(self.engine.dirty)
        count = len(self.provider.messages)
        self.engine.tick(self.now+timedelta(minutes=5))
        self.engine.tick(self.now+timedelta(minutes=6))
        self.assertFalse(self.engine.dirty)
        self.assertNotIn('meeting',self.engine.plans)
        self.assertEqual(len(self.provider.messages),count)
        self.assertEqual(self.backend.send.call_count,1)

    def test_batch_update_moves_only_requested_fields_and_reuses_reference_undo(self):
        self.seed(Event('first','组会','2026-11-27T12:00'),Event('second','报告','2026-11-27T15:00'))
        self.engine.configure(dict(self.engine.config(),events={'first':{'mode':'off'}}))
        query = tool('query_events',{'start':'2026-11-27','end':'2026-11-27'})
        update = tool('propose_events',dict(action='update',items=[
            dict(event_id='first',fields=dict(time='13:00',reminder_rule=dict(mode='relative',minutes=15))),
            dict(event_id='second',fields=dict(reminder_rule=dict(mode='absolute',at='2026-11-27T09:00'))),
        ]))
        response = self.send('两个事项的提醒都更新，组会改到13:00。',query,update)
        self.assertEqual(response['status'],'succeeded',response.get('error_message'))
        operation = response['operation_result']['operation_id']
        self.assertEqual(self.gateway.detail('first')['reminder_plan']['planned_at'],'2026-11-27T12:45')
        self.assertEqual(self.gateway.detail('second')['time'],'15:00')
        self.gateway.undo_remote_operation(operation,'undo-batch-rules')
        self.assertEqual(self.gateway.detail('first')['time'],'12:00')
        self.assertEqual(self.engine.config()['events'],{'first':{'mode':'off'}})

    def test_legacy_config_and_json_joint_write_roundtrip(self):
        json_store = Store(self.store.data_file.parent/'legacy.json')
        history = EventCommandHistory(json_store)
        engine = ReminderEngine(json_store,self.backend)
        old = Event('old','报告','2026-11-27T23:59',event_type='ddl',has_time=False,reminder=None)
        history.upsert(old)
        engine.configure({'enabled':True,'time':'09:00',
                          'events':{'old':{'mode':'custom','days':3,'time':'07:00'}}})
        engine.replan(['old'])
        self.assertEqual(engine.plans['old'].when,datetime(2026,11,24,7))
        command = history.preview_upsert(old,reminder_rule={'mode':'relative','days':5})
        history.commit(command)
        reopened = Store(json_store.data_file)
        self.assertEqual(stored_event_rule(reopened.settings,'old'),{'mode':'relative','days':5})
        self.assertEqual(reopened.event_by_id('old'),old)
        history.undo()
        reopened.load()
        self.assertEqual(stored_event_rule(reopened.settings,'old'),{'mode':'custom','days':3,'time':'07:00'})

    def test_disabled_master_and_reply_text_do_not_claim_delivery(self):
        self.engine.configure(dict(self.engine.config(),enabled=False))
        response = self.create('明天下午三点开组会，提前15分钟提醒我。',
                               dict(title='组会',date='2026-11-27',time='15:00',
                                    reminder_rule=dict(mode='relative',minutes=15)))
        row = self.result(response,'relative','2026-11-27T14:45')
        self.assertFalse(row['reminder_plan']['global_enabled'])
        self.assertNotIn(row['id'],self.engine.plans)
        business = {'schema_version':1,'events':[]}
        payload, _ = make_receipt(response,business=business)
        receipt = json.loads(payload)
        self.assertEqual(receipt['business_receipt'],business)
        self.assertIn('全局提醒已关闭',receipt['reply_text'])
        self.assertIn('2026-11-27 14:45',receipt['reply_text'])
        self.backend.send.assert_not_called()
