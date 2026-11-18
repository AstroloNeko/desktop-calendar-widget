from unittest import TestCase
from unittest.mock import patch
from datetime import date
from dataclasses import asdict
import test_calendar_chat as fixture
from desktop_calendar.assistant.calendar_chat_service import OWNER
from calendar_core import Event


class TaskPlanTests(TestCase):
    setUp=fixture.ChatTests.setUp
    send=fixture.ChatTests.send

    def create(self, request='batch-create-request'):
        self.service.auto_execute=True
        items=[dict(title='WB实验',date=f'2026-11-{day}') for day in (15,16,17)]
        with patch('desktop_calendar.assistant.calendar_task_plan.date') as today:
            today.today.return_value=date(2026,11,14)
            return self.send([fixture.tool('batch_create_events',dict(items=items,distinct_colors=True))],
                '给往后三天各加一个WB实验，随机用三种颜色',request)

    def test_create_atomic_colors_ids_dates_retry_undo(self):
        with patch.object(self.store,'save',wraps=self.store.save) as save:
            result=self.create()
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(save.call_count,1)
        events=result['operation_result']['events']
        self.assertEqual([e['date'] for e in events],['2026-11-15','2026-11-16','2026-11-17'])
        self.assertEqual(len({e['id'] for e in events}),3)
        self.assertEqual(len({e['color'] for e in events}),3)
        retry=self.create()
        self.assertEqual(result,retry)
        self.assertEqual(len(self.store.events),5)
        self.commands.undo()
        self.assertEqual(len(self.store.events),2)
        self.commands.redo()
        self.assertEqual(len(self.store.events),5)

    def test_second_day_not_same_title_elsewhere(self):
        result=self.create()
        identifier=result['operation_result']['event_ids'][1]
        reply=self.send([fixture.tool('preview_update',dict(event_id=identifier,changes={'color':'#8B70D6'}))],
            '把第二天的WB实验改成紫色','second-day-request')
        self.assertEqual(reply['status'],'succeeded',reply)
        self.assertEqual(self.store.event_by_id(identifier).color,'#8B70D6')

    def test_batch_update_and_complete(self):
        result=self.create()
        ids=result['operation_result']['event_ids']
        args=dict(items=[dict(event_id=i,changes={'color':'#52B788'}) for i in ids])
        with patch.object(self.store,'save',wraps=self.store.save) as save:
            reply=self.send([fixture.tool('batch_update_events',args)],'把刚才那三个都改成绿色','update-batch-request')
        self.assertEqual(reply['status'],'succeeded',reply)
        self.assertEqual(save.call_count,1)
        self.assertTrue(all(self.store.event_by_id(i).color=='#52B788' for i in ids))
        reply=self.send([fixture.tool('batch_set_completion',dict(event_ids=ids,done=True))], '全部标记完成','complete-batch-request')
        self.assertEqual(reply['status'],'succeeded',reply)
        self.assertTrue(all(self.store.event_by_id(i).done for i in ids))

    def test_failure_rolls_back_and_no_repeat_continue(self):
        self.service.auto_execute=True
        with patch.object(self.store,'save',side_effect=OSError('disk')):
            reply=self.create()
        self.assertEqual(reply['status'],'failed',reply)
        self.assertEqual(len(self.store.events),2)
        self.assertEqual(self.commands.undo_count,0)
        self.create('new-create-request')
        reply=self.send([], '继续添加','continue-request')
        self.assertEqual(reply['status'],'ok')
        self.assertEqual(len(self.store.events),5)

    def test_unknown_ids_duplicate_ids_rejected(self):
        self.service.auto_execute=True
        for ids in (['guessed'],['event-one','event-one']):
            before=self.store.data_file.read_bytes()
            reply=self.send([fixture.tool('batch_set_completion',dict(event_ids=ids,done=True))],
                '把前面那三个全部标记完成','bad-batch-'+str(len(ids)))
            self.assertNotEqual(reply['status'],'succeeded')
            self.assertEqual(self.store.data_file.read_bytes(),before)

    def test_recover_observes_without_replay(self):
        response=self.create()
        task=response['task']
        task['status']='executing'
        self.journal.put('task',task['task_id'],OWNER,task)
        with patch.object(self.store,'save') as save:
            recovered=self.service.get_task(self.sid,task['task_id'])
        self.assertEqual(recovered['status'],'completed')
        save.assert_not_called()

    def test_delete_shorthand_and_terminal_card(self):
        response=self.send([fixture.tool('query_events',{'keyword':'实验室'}),fixture.tool('preview_delete',{'event_id':'event-one'})],
            '今天实验室组会删了','delete-request')
        self.assertEqual(response['status'],'confirmation_required',response)
        result=self.service.confirm_operation(self.sid,response['pending_operation']['operation_id'])
        self.assertEqual(result['status'],'succeeded')
        response=self.send([fixture.tool('query_events',{'keyword':'项目'})],'查找项目','after-delete-query')
        self.assertIsNone(response['pending_operation'])

    def test_other_color_resolved_locally(self):
        result=self.create()
        identifier=result['operation_result']['event_ids'][1]
        old=self.store.event_by_id(identifier).color
        reply=self.send([],'把第二天的WB实验改个其他颜色','other-color-request')
        self.assertEqual(reply['status'],'succeeded',reply)
        self.assertNotEqual(self.store.event_by_id(identifier).color,old)

    def test_invalid_member_prevents_entire_creation(self):
        self.service.auto_execute=True
        before=self.store.data_file.read_bytes()
        reply=self.send([fixture.tool('batch_create_events',{'items':[
            dict(title='a',date='2026-11-20'),dict(title='b',date='2026-02-30')]})],
            '分别各创建两个事项','invalid-date-batch')
        self.assertNotEqual(reply['status'],'succeeded')
        self.assertEqual(self.store.data_file.read_bytes(),before)
        self.assertEqual(self.commands.undo_count,0)

    def test_native_multiday_not_expanded_by_batch_patch(self):
        result=self.create()
        identifier=result['operation_result']['event_ids'][0]
        self.commands.update(identifier,duration_days=3)
        count=len(self.store.events)
        reply=self.send([fixture.tool('batch_update_events',dict(items=[dict(event_id=identifier,changes={'color':'#52B788'})]))],
            '把前面那批里的第一个改成绿色','multiday-batch-patch')
        self.assertEqual(reply['status'],'succeeded',reply)
        self.assertEqual(len(self.store.events),count)
        self.assertEqual(self.store.event_by_id(identifier).duration_days,3)

    def test_result_receipt_failure_never_replays(self):
        original=self.journal.put
        def broken(kind,identifier,owner,payload):
            if kind=='task' and payload['status']=='completed':
                raise OSError('receipt unavailable')
            return original(kind,identifier,owner,payload)
        with patch.object(self.journal,'put',side_effect=broken):
            result=self.create()
        self.assertEqual(result['error_code'],'execution_uncertain')
        self.assertEqual(len(self.store.events),5)
        recovered=self.service.get_task(self.sid,result['task']['task_id'])
        self.assertEqual(recovered['status'],'completed')
        self.assertEqual(self.commands.undo_count,1)

    def test_clarification_cancel_and_resume_same_identity(self):
        self.service.auto_execute=True
        items=[dict(title='a',date='2026-11-20') for _ in range(7)]
        result=self.send([fixture.tool('batch_create_events',dict(items=items,distinct_colors=True))],
            '各创建七个事项，使用不同颜色','clarify-colors-request')
        task=result['task']
        self.assertEqual(task['status'],'needs_clarification')
        result=self.send([fixture.tool('batch_create_events',dict(items=items[:3],distinct_colors=True))],
            '改为各创建三个，使用不同颜色','resume-colors-request')
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(task['task_id'],result['task']['task_id'])

    def test_http_uses_same_batch_service(self):
        import json
        import urllib.request
        from desktop_calendar.assistant.calendar_chat_http import LocalChatHTTP
        self.service.auto_execute=True
        self.provider.replies=[fixture.tool('batch_create_events',dict(items=[
            dict(title='HTTP隔离',date='2026-12-10'),dict(title='HTTP隔离',date='2026-12-11')]))]
        server=LocalChatHTTP(self.service,'Q'*48,port=0)
        server.start()
        self.addCleanup(server.stop)
        body=dict(session_id=self.sid,message='各创建两个HTTP隔离事项',request_id='http-batch-request')
        request=urllib.request.Request(f'http://127.0.0.1:{server.port}/v1/chat/messages',data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer '+'Q'*48})
        with urllib.request.urlopen(request,timeout=10) as response:
            result=json.load(response)
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(result['operation_result']['event_ids']),2)
        self.assertEqual(self.commands.undo_count,1)
