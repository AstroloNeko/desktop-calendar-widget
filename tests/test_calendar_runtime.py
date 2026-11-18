from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

from calendar_runtime import HeadlessRuntime, CalendarRuntime
from calendar_executor import SerialExecutor
from calendar_core import RoutineItem, EventCategory, Store
from desktop_calendar.assistant.calendar_chat_tools import ChatError
from calendar_event_service import EventOperationError
from desktop_calendar.assistant.llm_provider import AIConfiguration
from test_calendar_chat import ScriptedProvider, tool


class EphemeralProtection:
    """Test-only opaque vault: disk contains random handles, never plaintext."""
    def __init__(self):
        self.values = {}

    def encrypt(self, raw):
        key = secrets.token_bytes(32)
        self.values[key] = raw
        return key

    def decrypt(self, raw):
        return self.values[raw]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'calendar.json'
        self.protection = EphemeralProtection()
        self.provider = ScriptedProvider([])
        self.runtime = self.make()
        self.addCleanup(self.runtime.stop)

    def make(self, **options):
        return HeadlessRuntime(self.path, protector=self.protection, provider=self.provider, **options)

    def create(self):
        return self.runtime.events.create_quick('Test', date(2026,11,20)).event_ids[0]

    def test_event_crud_and_restart(self):
        r = self.runtime
        self.assertEqual(r.query({'start':'2026-11-20','end':'2026-11-20'})['total'], 0)
        identifier = self.create()
        r.events.patch(identifier, {'title':'Changed','color':'#52B788'})
        r.events.set_completed(identifier, True)
        self.assertTrue(r.snapshot()['events'][0]['done'])
        r.events.delete(identifier)
        r.stop()
        with self.make() as restarted:
            self.assertEqual(restarted.snapshot()['events'], [])

    def test_habit_rules_history_completion_restart(self):
        r = self.runtime
        habit = RoutineItem('h','Habit',created_on='2026-01-01',workdays_only=False)
        r.mutations.upsert_routine(habit)
        r.mutations.upsert_routine(replace(habit,frequency='weekly',weekdays=[0,2]))
        r.mutations.set_routine_completed('h',date(2026,11,16),True)
        state = r.snapshot()['routines'][0]
        self.assertTrue(state['schedule_history'])
        self.assertIn('2026-11-16',state['habit_done'])
        r.stop()
        with self.make() as restarted:
            self.assertEqual(restarted.snapshot()['routines'][0],state)

    def test_mixed_undo(self):
        self.runtime.mutations.upsert_routine(RoutineItem('h','Habit'))
        self.create()
        self.runtime.undo()
        self.runtime.undo()
        self.assertEqual(self.runtime.snapshot()['routines'],[])
        self.runtime.redo()
        self.assertEqual(len(self.runtime.snapshot()['routines']),1)

    def test_category_invalidates_history(self):
        self.create()
        self.runtime.mutations.upsert_category(EventCategory('c','Category'))
        self.assertIsNone(self.runtime.undo())

    def test_parallel_services_share_one_owner(self):
        owners = []
        original = self.runtime._store.save
        def save():
            owners.append(threading.get_ident())
            return original()
        with patch.object(self.runtime._store,'save',side_effect=save), ThreadPoolExecutor(6) as pool:
            ids = list(pool.map(lambda n:self.runtime.events.create_quick(str(n),date(2026,11,20)).event_ids[0],range(20)))
        self.assertEqual(len(set(ids)),20)
        self.assertEqual(set(owners),{self.runtime.executor.owner})

    def test_snapshot_detached(self):
        self.create()
        snapshot = self.runtime.snapshot()
        snapshot['events'][0]['title']='leak'
        self.assertEqual(self.runtime.snapshot()['events'][0]['title'],'Test')

    def test_arguments_detached(self):
        habit=RoutineItem('h','Habit')
        self.runtime.mutations.upsert_routine(habit)
        habit.title='leak'
        self.assertEqual(self.runtime.snapshot()['routines'][0]['title'],'Habit')

    def test_range_atomic_and_serial(self):
        with patch.object(self.runtime._store,'save',wraps=self.runtime._store.save) as save:
            self.runtime.mutations.set_date_status(date(2026,11,16),'leave',date(2026,11,20))
            self.assertEqual(save.call_count,1)
        self.assertEqual(len(self.runtime.snapshot()['date_states']),5)

    def test_storage_failure_rollback(self):
        before=self.runtime.snapshot()
        with patch.object(self.runtime._store,'save',side_effect=OSError('disk')):
            with self.assertRaises(EventOperationError):
                self.runtime.mutations.upsert_category(EventCategory('c','Category'))
        self.assertEqual(before,self.runtime.snapshot())

    def test_unknown_commit_blocks_next_write(self):
        original=self.runtime._store.save
        def save():
            original()
            raise OSError('after replace')
        with patch.object(self.runtime._store,'save',side_effect=save):
            with self.assertRaises(EventOperationError) as error:
                self.runtime.mutations.upsert_category(EventCategory('c','Category'))
        self.assertEqual(error.exception.code,'execution_uncertain')
        with self.assertRaises(ChatError):
            self.create()

    def test_double_stop_and_reject_new_work(self):
        self.runtime.stop()
        self.runtime.stop()
        self.assertFalse(self.runtime.executor.thread.is_alive())
        with self.assertRaises(ChatError): self.create()
        with self.assertRaises(ChatError): self.runtime.service.new_session()

    def test_provider_clock_journal_injection(self):
        with self.make(clock=lambda:12345) as r:
            self.assertIs(r.service.provider,self.provider)
            self.assertEqual(r.service.clock(),12345)
            self.assertEqual(r.service.journal.path.parent,self.path.parent/'ai-assistant')

    def test_notification_backend_is_optional(self):
        self.assertIsNone(self.runtime._reminders)
        backend=Mock()
        with self.make(notification_backend=backend) as r:
            self.assertIs(r._reminders.backend,backend)
            backend.assert_not_called()

    def test_initialization_failure_no_thread_leak(self):
        before={t.ident for t in threading.enumerate()}
        with patch('calendar_runtime.ChatJournal',side_effect=OSError('journal')):
            with self.assertRaises(OSError): self.make()
        self.assertEqual({t.ident for t in threading.enumerate()},before)

    def test_production_protection_failure_never_plaintext(self):
        with patch('desktop_calendar.assistant.llm_provider.UserProtection',side_effect=OSError('DPAPI')):
            with self.assertRaises(OSError):
                HeadlessRuntime(self.path,provider=self.provider)
        self.assertFalse((self.path.parent/'ai-assistant/configuration.dpapi').exists())

    def test_ai_real_tools_multiturn_delete(self):
        service=self.runtime.service
        sid=service.new_session()['session_id']
        self.provider.replies=[tool('preview_create',{'title':'WB实验','date':'2026-11-20'})]
        created=service.send_message(sid,'新增事项，2026-11-20做WB实验','create-request')
        self.assertEqual(created['status'],'succeeded',created)
        identifier=self.runtime.snapshot()['events'][0]['id']
        self.provider.replies=[tool('preview_update',{'event_id':identifier,'changes':{'title':'细胞实验'}})]
        changed=service.send_message(sid,'把刚才那个改名为细胞实验','rename-request')
        self.assertEqual(changed['status'],'succeeded',changed)
        self.provider.replies=[tool('query_events',{'keyword':'细胞实验'})]
        service.send_message(sid,'查询细胞实验','query-request')
        self.provider.replies=[tool('preview_delete',{'event_id':identifier})]
        pending=service.send_message(sid,'删除刚才那个细胞实验','delete-request')['pending_operation']
        self.assertIsNotNone(pending)
        self.assertEqual(len(self.runtime.snapshot()['events']),1)
        service.confirm_operation(sid,pending['operation_id'],revision=pending['revision'])
        self.assertEqual(self.runtime.snapshot()['events'],[])

    def test_http_auth_and_lifecycle(self):
        config=self.runtime._core.configuration
        config.save({**config.load(),'http_enabled':True,'port':18746})
        # Port zero is test-only injection after normal config validation.
        original=config.load
        with patch.object(config,'load',side_effect=lambda:{**original(),'port':0}):
            server=self.runtime.apply_http()
        token=original()['token']
        base=f'http://127.0.0.1:{server.port}'
        def request(path,body=None,key=token):
            req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=5) as response: return json.load(response)
        sid=request('/v1/sessions',{})['session_id']
        with self.assertRaises(urllib.error.HTTPError) as error:
            request('/v1/sessions',{},key='invalid')
        self.assertEqual(error.exception.code,401)
        self.assertEqual(request(f'/v1/sessions/{sid}/messages')['messages'],[])
        self.provider.replies=[tool('preview_create',{'title':'WB实验','date':'2026-11-20'})]
        created=request('/v1/chat/messages',dict(session_id=sid,message='新增事项2026-11-20做WB实验',request_id='http-create'))
        self.assertEqual(created['status'],'succeeded',created)
        identifier=self.runtime.snapshot()['events'][0]['id']
        self.provider.replies=[tool('preview_delete',{'event_id':identifier})]
        pending=request('/v1/chat/messages',dict(session_id=sid,message='删除刚才那个WB实验',request_id='http-delete'))['pending_operation']
        operation_id=pending['operation_id']
        result=request(f'/v1/operations/{operation_id}?session_id={sid}')
        self.assertEqual(result['status'],'pending')
        result=request(f'/v1/operations/{operation_id}/confirm',dict(session_id=sid,revision=pending['revision']))
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(self.runtime.snapshot()['events'],[])
        self.runtime.stop()
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(server.active_requests,0)

    def test_http_start_failure_keeps_business_usable(self):
        config=self.runtime._core.configuration
        config.save({**config.load(),'http_enabled':True})
        with patch('calendar_runtime.LocalChatHTTP.start',side_effect=OSError('occupied')):
            with self.assertRaises(ChatError): self.runtime.apply_http()
        self.assertIsNone(self.runtime._core.http)
        self.create()

    def test_http_reconfiguration_drains_old_requests(self):
        old=Mock()
        self.runtime._core.http=old
        self.runtime.apply_http()  # Default configuration disables HTTP.
        old.stop.assert_called_once_with(wait_requests=True,timeout=10)
        self.assertIsNone(self.runtime._core.http)

    def test_import_and_independent_process_no_tk(self):
        code="""
import sys,tempfile
from pathlib import Path
from calendar_runtime import HeadlessRuntime
with tempfile.TemporaryDirectory() as folder:
    with HeadlessRuntime(Path(folder)/'calendar.json') as runtime:
        assert runtime.snapshot()['events']==[]
assert not any(m in sys.modules for m in ('tkinter','app','ui_theme','win_integration'))
print('headless process passed')
"""
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_theme_identifiers_unchanged(self):
        from desktop_preferences import VALID_THEME_NAMES,normalize_theme_name
        from ui_theme import THEMES
        self.assertEqual(set(VALID_THEME_NAMES),set(THEMES))
        self.assertEqual(normalize_theme_name('win7_aero'),'aero')
        self.assertEqual(normalize_theme_name('gemini_reviewing'),'slate')

    def test_explicit_journal_injection(self):
        from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
        journal=ChatJournal(self.path.parent/'custom.sqlite3',self.protection)
        with self.make(journal=journal) as runtime:
            self.assertIs(runtime.service.journal,journal)

    def test_provider_initialization_failure_closes_executor(self):
        before={t.ident for t in threading.enumerate()}
        with patch('calendar_runtime.DeepSeekProvider',side_effect=ValueError('provider')):
            with self.assertRaises(ValueError):
                HeadlessRuntime(self.path,protector=self.protection)
        self.assertEqual({t.ident for t in threading.enumerate()},before)

    def test_notification_invalidation_after_commit(self):
        with self.make(notification_backend=Mock()) as runtime:
            with patch.object(runtime._reminders,'invalidate') as invalidate:
                runtime.events.create_quick('x',date(2026,11,20))
                self.assertEqual(invalidate.call_count,1)
                with patch.object(runtime._store,'save',side_effect=OSError('disk')):
                    with self.assertRaises(EventOperationError):
                        runtime.mutations.upsert_category(EventCategory('c','Cat'))
                self.assertEqual(invalidate.call_count,1)

    def test_llm_does_not_occupy_business_executor_and_stop_is_bounded(self):
        entered,release=threading.Event(),threading.Event()
        def complete(*args):
            entered.set()
            release.wait(3)
            return {'content':'done'}
        self.provider.complete=complete
        sid=self.runtime.service.new_session()['session_id']
        with ThreadPoolExecutor() as pool:
            pending=pool.submit(self.runtime.service.send_message,sid,'查询本月事项','slow-request')
            self.assertTrue(entered.wait(1))
            self.create()  # A network wait must not block serial business operations.
            try:
                with self.assertRaises(ChatError): self.runtime.stop(timeout=.01)
                self.assertEqual(self.runtime.state,'stopping')
                with self.assertRaises(ChatError): self.create()
            finally: release.set()
            pending.result(timeout=3)
        self.runtime.stop()
        self.assertFalse(self.runtime.executor.thread.is_alive())

    def test_batch_one_undo_preserved(self):
        one=self.create()
        two=self.create()
        self.runtime.events.bulk([one,two],'delete')
        self.assertEqual(self.runtime.snapshot()['events'],[])
        self.runtime.undo()
        self.assertEqual({e['id'] for e in self.runtime.snapshot()['events']},{one,two})

    def test_desktop_core_refresh_failure_preserves_actual_commit(self):
        self.runtime._core.gateway.refresh=Mock(side_effect=RuntimeError('UI'))
        service=self.runtime.service
        sid=service.new_session()['session_id']
        self.provider.replies=[tool('preview_create',{'title':'WB实验','date':'2026-11-20'})]
        result=service.send_message(sid,'新增事项2026-11-20做WB实验','refresh-request')
        repeated=service.send_message(sid,'新增事项2026-11-20做WB实验','refresh-request')
        self.assertEqual(len(self.runtime.snapshot()['events']),1)
        self.assertEqual(result,repeated)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor=SerialExecutor(timeout=.1)
        self.addCleanup(self.executor.stop)

    def test_exception_and_reentrant(self):
        with self.assertRaises(ValueError): self.executor(lambda: int('invalid'))
        self.assertEqual(self.executor(lambda:self.executor(lambda:42)),42)

    def test_queued_timeout_cancels_without_write(self):
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        writes=[]
        with ThreadPoolExecutor() as pool:
            running=pool.submit(self.executor,lambda:(entered.set(),release.wait(2)))
            entered.wait(1)
            with self.assertRaises(ChatError) as error: self.executor(lambda:writes.append(1))
            self.assertEqual(error.exception.code,'service_unavailable')
            release.set()
            try: running.result()
            except ChatError: pass
        self.executor(lambda:None)
        self.assertEqual(writes,[])

    def test_running_timeout_is_uncertain_no_replay(self):
        release=threading.Event()
        writes=[]
        try:
            with self.assertRaises(ChatError) as error:
                self.executor(lambda:(release.wait(2),writes.append(1)))
            self.assertEqual(error.exception.code,'execution_uncertain')
        finally: release.set()
        self.executor.stop()
        self.assertEqual(writes,[1])

    def test_stop_timeout_retains_thread_until_finished(self):
        entered,release=threading.Event(),threading.Event()
        with ThreadPoolExecutor() as pool:
            running=pool.submit(self.executor,lambda:(entered.set(),release.wait(2)))
            entered.wait(1)
            try:
                with self.assertRaises(ChatError): self.executor.stop(timeout=.01)
                with self.assertRaises(ChatError): self.executor(lambda:None)
            finally: release.set()
            try: running.result()
            except ChatError: pass
        self.executor.stop()
        self.assertFalse(self.executor.thread.is_alive())
