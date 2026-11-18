"""User transcript regression: duplicates are distinct identities, all means all."""
from unittest import TestCase
from unittest.mock import patch
from datetime import date
from calendar_core import Event, RoutineItem
from desktop_calendar.assistant.calendar_chat_tools import ChatError
from desktop_calendar.assistant.calendar_chat_service import OWNER
import test_calendar_chat as fixture


class BatchDeleteTests(TestCase):
    def setUp(self):
        fixture.ChatTests.setUp(self)
        self.store.events=[Event(i,title,day+'T23:59',has_time=False) for i,title,day in (
            ('A','WB实验','2026-11-15'),('B','WB实验','2026-11-15'),('C','WB实验','2026-11-16'),
            ('D','WB实验','2026-11-17'),('E','细胞实验','2026-11-16'),('F','组会','2026-11-17'))]
        self.store.save()
        self.counter=0
        self.bounds()

    def bounds(self):
        session=self.service.get_session(self.sid)
        session['clarified_query']=dict(start='2026-11-15',end='2026-11-17')
        self.journal.put('session',self.sid,OWNER,session)

    def say(self,message,replies=()):
        self.counter+=1
        return fixture.ChatTests.send(self,replies,message,'delete-test-request-'+str(self.counter))

    def preview(self,message='把这几天所有的WB实验全删除了'):
        response=self.say(message)
        self.assertEqual(response['pending_operation']['preview']['event_ids'],['A','B','C','D'],response)
        return response['pending_operation']

    def confirm(self,op):
        return self.service.confirm_operation(self.sid,op['operation_id'],revision=op['revision'])

    def test_full_transcript_no_single_selection_loop_two_stages(self):
        op=self.preview()
        for message in ('全部','把这几天的WB实验全删了','全部'):
            response=self.say(message)
            self.assertEqual(response['pending_operation']['operation_id'],op['operation_id'])
        self.assertEqual(self.provider.messages,[])
        with patch.object(self.store,'save',wraps=self.store.save) as save:
            first=self.confirm(op)['pending_operation']
            save.assert_not_called()
            self.assertEqual(first['stage'],'awaiting_final_confirmation')
            result=self.confirm(first)
            self.assertEqual(save.call_count,1)
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual([e.id for e in self.store.events],['E','F'])
        self.assertEqual(self.commands.undo_count,1)
        self.confirm(first)
        self.assertEqual(self.commands.undo_count,1)
        self.commands.undo()
        self.assertEqual([e.id for e in self.store.events],list('ABCDEF'))
        self.commands.redo()
        self.assertEqual([e.id for e in self.store.events],['E','F'])

    def test_quotes_and_exact_title(self):
        self.store.events.append(Event('other','WB实验后处理','2026-11-16T10:00'))
        for title in ('WB实验','"WB实验"',"''WB实验\"",'“WB实验”'):
            op=self.preview('把这几天的'+title+'全删了')
            self.service.cancel_operation(self.sid,op['operation_id'])

    def test_no_range_clarifies_then_keeps_intent(self):
        session=self.service.get_session(self.sid)
        session.pop('clarified_query')
        self.journal.put('session',self.sid,OWNER,session)
        response=self.say('把这几天所有的WB实验全删除了')
        self.assertEqual(response['status'],'clarification_required')
        response=self.say('2026-11-15 至 2026-11-17')
        self.assertEqual(len(response['pending_operation']['preview']['before']),4)

    def test_all_without_context_never_selects_calendar(self):
        response=self.say('全部')
        self.assertEqual(len(self.store.events),6)
        self.assertFalse(response.get('pending_operation'))

    def test_empty_query_has_no_selection_prompt(self):
        response=self.say('查找不存在的事项',[fixture.tool('query_events',dict(keyword='不存在的事项'))])
        self.assertEqual(response['candidates'],[])
        self.assertNotIn('选择',response['assistant_message'])

    def test_range_and_title_corrections_replace_reviewed_scope(self):
        self.store.events = [Event(i,title,'2026-11-'+day+'T12:00') for i,title,day in (
            ('outside','WB实验','17'),('wb','WB实验','18'),('cell','细胞实验','19'),
            ('meeting','组会','20'),('similar','WB实验记录','20'),('later','细胞实验','21'))]
        self.store.save()
        with patch('desktop_calendar.assistant.calendar_batch_delete.date',wraps=date) as clock:
            clock.today.return_value=date(2026,11,18)
            vague=self.say('18-20三天的实验安排全删掉')
            self.assertEqual(vague['status'],'clarification_required')
            self.assertIn('2026-11-18 至 2026-11-20',vague['assistant_message'])
            self.assertIsNone(self.say('11-18到11-20')['pending_operation'])
            broad=self.say('18-20这三天的事项全删除')['pending_operation']
            self.assertEqual(broad['preview']['event_ids'],['wb','cell','meeting','similar'])
            same=self.say('不要精确匹配标题，我的意思是不管是什么事情全删了')
            self.assertEqual(same['pending_operation']['operation_id'],broad['operation_id'])
            old=self.confirm(broad)['pending_operation']
            narrow=self.say('傻比把，这几天WB实验全删掉')['pending_operation']
            self.assertEqual(self.confirm(old)['status'],'cancelled')
            self.assertEqual(narrow['preview']['event_ids'],['wb'])
            self.assertEqual(self.say('删了')['pending_operation']['operation_id'],narrow['operation_id'])
            final=self.say('2026-11-18到2026-11-20这三天的细胞实验，还有WB实验，都删除')['pending_operation']
        self.assertEqual(final['preview']['event_ids'],['wb','cell'])
        self.assertEqual(self.confirm(narrow)['status'],'cancelled')
        self.assertEqual(self.commands.undo_count,0)
        self.confirm(self.confirm(final)['pending_operation'])
        self.assertEqual([e.id for e in self.store.events],['outside','meeting','similar','later'])
        self.commands.undo()
        self.assertEqual(len(self.store.events),6)
        self.assertEqual(self.provider.messages,[])

    def test_invalid_or_empty_revised_scope_retires_old_confirmation(self):
        op=self.preview()
        reply=self.say('2026-02-30到2026-03-01这些天事项全删除')
        self.assertEqual(reply['status'],'clarification_required')
        self.assertIsNone(reply['pending_operation'])
        self.assertEqual(self.confirm(op)['status'],'cancelled')
        reply=self.say('2026-12-01到2026-12-02事项全删除')
        self.assertEqual(reply['status'],'ok')
        self.assertIsNone(reply['pending_operation'])
        self.assertNotIn('选择',reply['assistant_message'])
        self.assertEqual(self.commands.undo_count,0)

    def test_scope_question_accepts_titles_or_all_without_repeating_delete(self):
        for answer, expected in [('细胞实验、组会',['E','F']),('全部',list('ABCEDF'))]:
            reply=self.say('2026-11-15至2026-11-17三天的实验安排全删掉')
            self.assertEqual(reply['status'],'clarification_required')
            op=self.say(answer)['pending_operation']
            self.assertEqual(op['preview']['event_ids'],expected)
            self.service.cancel_operation(self.sid,op['operation_id'])

    def test_missing_named_member_never_prepares_partial_union(self):
        op=self.preview()
        result=self.say('这几天的WB实验，还有不存在的实验，都删除')
        self.assertEqual(result['status'],'clarification_required')
        self.assertIn('不存在的实验',result['assistant_message'])
        self.assertIsNone(result['pending_operation'])
        self.assertIsNone(self.say('全部')['pending_operation'])
        self.assertEqual(self.confirm(op)['status'],'cancelled')

    def test_cancel_and_expire(self):
        op=self.preview()
        self.service.cancel_operation(self.sid,op['operation_id'])
        self.confirm(op)
        self.assertEqual(len(self.store.events),6)
        self.bounds()
        op=self.preview()
        self.clock+=901
        response=self.confirm(op)
        self.assertEqual(response['status'],'expired')
        self.assertEqual(len(self.store.events),6)

    def test_any_conflict_refuses_whole_batch(self):
        op=self.preview()
        final=self.confirm(op)['pending_operation']
        self.commands.update('B',title='已改名')
        result=self.confirm(final)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['pending_operation']['stage'],'conflict')
        self.assertEqual(len(self.store.events),6)

    def test_exception_rolls_back_everything(self):
        op=self.preview()
        final=self.confirm(op)['pending_operation']
        original=self.store.data_file.read_bytes()
        with patch.object(self.store,'save',side_effect=OSError('disk')):
            response=self.confirm(final)
        self.assertEqual(response['status'],'failed')
        self.assertEqual(self.store.data_file.read_bytes(),original)
        self.assertEqual(len(self.store.events),6)
        self.assertEqual(self.commands.undo_count,0)

    def test_old_revision_cannot_skip_second_stage(self):
        op=self.preview()
        self.confirm(op)
        with self.assertRaises(ChatError):
            self.confirm(op)
        with self.assertRaises(ChatError):
            self.service.confirm_operation(self.sid,op['operation_id'])
        self.assertEqual(len(self.store.events),6)

    def test_over_limit_and_truncation_refused(self):
        self.store.events.extend(Event(str(i),'WB实验','2026-11-16T10:00') for i in range(40))
        response=self.say('把这几天所有的WB实验全删除了')
        self.assertEqual(response['status'],'failed')
        self.assertFalse(response.get('pending_operation'))
        self.assertEqual(len(self.store.events),46)

    def test_mixed_history_and_refresh_failure(self):
        self.commands.upsert_routine(RoutineItem('habit','习惯'))
        op=self.preview()
        final=self.confirm(op)['pending_operation']
        self.gateway.refresh.side_effect=RuntimeError('UI')
        result=self.confirm(final)
        self.assertEqual(result['status'],'succeeded')
        self.commands.undo()
        self.commands.undo()
        self.assertEqual(len(self.store.events),6)
        self.assertEqual(self.store.routines,[])

    def test_all_binds_previous_valid_candidates(self):
        self.say('把WB实验删了',[fixture.tool('query_events',dict(keyword='WB实验',start='2026-11-15',end='2026-11-17')),
            fixture.tool('preview_delete',dict(event_id='A'))])
        response=self.say('全部')
        self.assertEqual(response['pending_operation']['preview']['event_ids'],['A','B','C','D'])

    def test_receipt_failure_observes_never_replays(self):
        op=self.preview()
        final=self.confirm(op)['pending_operation']
        original=self.journal.put
        def broken(kind,identifier,owner,payload):
            if kind=='operation' and payload['status']=='succeeded':
                raise OSError('journal')
            return original(kind,identifier,owner,payload)
        with patch.object(self.journal,'put',side_effect=broken):
            response=self.confirm(final)
        self.assertEqual(response['error_code'],'execution_uncertain')
        response=self.service.get_operation_result(self.sid,op['operation_id'])
        self.assertEqual(response['status'],'succeeded')
        self.assertEqual(self.commands.undo_count,1)

    def test_http_two_confirmations_same_service(self):
        import json
        import urllib.request
        from desktop_calendar.assistant.calendar_chat_http import LocalChatHTTP
        server=LocalChatHTTP(self.service,'Q'*48,port=0)
        server.start()
        self.addCleanup(server.stop)
        def post(path,body):
            request=urllib.request.Request(f'http://127.0.0.1:{server.port}'+path,data=json.dumps(body).encode(),
                headers={'Content-Type':'application/json','Authorization':'Bearer '+'Q'*48})
            with urllib.request.urlopen(request,timeout=10) as response:
                return json.load(response)
        result=post('/v1/chat/messages',dict(session_id=self.sid,message='把这几天所有的WB实验全删除了',request_id='http-batch-delete'))
        op=result['pending_operation']
        first=post('/v1/operations/'+op['operation_id']+'/confirm',dict(session_id=self.sid,revision=op['revision']))
        self.assertEqual(len(self.store.events),6)
        op=first['pending_operation']
        final=post('/v1/operations/'+op['operation_id']+'/confirm',dict(session_id=self.sid,revision=op['revision']))
        self.assertEqual(final['status'],'succeeded')
        self.assertEqual([e.id for e in self.store.events],['E','F'])

    def test_finished_delete_does_not_hijack_next_query(self):
        op=self.preview()
        first=self.confirm(op)['pending_operation']
        self.confirm(first)
        response=self.say('查找组会',[fixture.tool('query_events',dict(keyword='组会'))])
        self.assertFalse(response.get('pending_operation'))
        self.assertEqual(response['candidates'][0]['id'],'F')

    def test_session_isolation(self):
        op=self.preview()
        other=self.service.new_session()['session_id']
        with self.assertRaises(ChatError):
            self.service.confirm_operation(other,op['operation_id'],revision=op['revision'])
        self.assertEqual(len(self.store.events),6)
