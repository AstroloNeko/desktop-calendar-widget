"""Exercise plan lifetimes through the same service used by desktop and HTTP."""
from dataclasses import replace
from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import patch

import test_calendar_chat as fixture
from calendar_core import Event, Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_tools import ChatError
from event_commands import EventCommandHistory


def propose(action, *members):
    return fixture.tool('propose_events', dict(action=action, items=list(members)))


class EventPlanTests(TestCase):
    setUp = fixture.ChatTests.setUp
    send = fixture.ChatTests.send

    def use_sqlite(self, *events):
        number = len(list(self.root.glob('operations-*.sqlite3')))
        self.store = Store(self.root/f'operations-{number}.sqlite3', repository_factory=SqliteCalendarRepository)
        self.addCleanup(self.store.repository.close)
        self.commands = EventCommandHistory(self.store)
        self.gateway = CalendarActionGateway(self.store, self.commands)
        for event in events:
            self.gateway.events.upsert(event)
        self.commands = EventCommandHistory(self.store)
        self.gateway = CalendarActionGateway(self.store, self.commands)
        self.service = CalendarChatService(self.gateway, self.journal, self.provider, clock=lambda:self.clock)

    def test_replay_reschedule_notice_keeps_patch_and_checks_receipt(self):
        self.use_sqlite(Event('meeting-28','组会','2026-11-28T10:30',reminder=10,notes='每周讨论'),
                        Event('other','组会','2026-11-29T09:00'))
        untouched = self.gateway.detail('other')
        source = dict(start='2026-11-28', end='2026-11-28', keyword='组会')
        moved = dict(date='2026-11-30', end_date='2026-11-30')
        with patch('desktop_calendar.assistant.calendar_chat_attributes.date',wraps=date) as today:
            today.today.return_value = date(2026,11,19)
            result = self.send([fixture.tool('query_events',source),
                                propose('update',dict(event_id='meeting-28',fields=moved))],
                               '会议改期通知：原定11月28日的组会改为11月30日','memo-reschedule')
        receipt = result['operation_result']
        self.assertTrue(receipt['committed'])
        self.assertTrue(receipt['goal_satisfied'])
        self.assertIsNotNone(self.store.repository.receipt(receipt['operation_id']))
        self.assertEqual((receipt['event']['date'],receipt['event']['end'],receipt['event']['time'],
                          receipt['event']['notes'],receipt['event']['reminder']),
                         ('2026-11-30','2026-11-30','10:30','每周讨论',10))
        self.assertEqual(self.gateway.detail('other'),untouched)
        self.commands.undo()
        self.assertEqual(self.gateway.detail('meeting-28')['date'],'2026-11-28')

    def test_reschedule_range_with_patch_has_same_result_for_target_and_items(self):
        self.use_sqlite(Event('meeting-28','组会','2026-11-28T10:30',duration_days=2,reminder=10),
                        Event('other','组会','2026-12-03T09:00'))
        untouched = self.gateway.detail('other')
        fields = dict(date='2026-11-30', end_date='2026-12-02', reminder=30, color='#52B788')
        bounds = dict(start='2026-11-28',end='2026-11-29')
        query = fixture.tool('query_events',dict(bounds,keyword='组会'))
        target = fixture.tool('propose_events',dict(action='update',target=dict(bounds,titles=['组会'],quantity='one'),fields=fields))
        items = propose('update',dict(event_id='meeting-28',fields=fields))
        for index, calls in enumerate(([query,items],[target]),1):
            with self.subTest(index=index):
                result = self.send(calls,'把11月28日到29日的组会改到11月30日至12月2日，换薄荷绿并提前30分钟提醒',f'reschedule-range-{index}')
                self.assertEqual(result['status'],'succeeded')
                row = result['operation_result']['event']
                self.assertEqual((row['id'],row['date'],row['end'],row['color'],row['reminder']),
                                 ('meeting-28','2026-11-30','2026-12-02','#52B788',30))
                self.assertEqual(self.gateway.detail('other'),untouched)
                self.commands.undo()

    def test_noop_reschedule_requires_final_date_to_match(self):
        self.use_sqlite(Event('meeting','组会','2026-11-28T10:30'))
        query = fixture.tool('query_events',dict(keyword='组会'))
        same = propose('update',dict(event_id='meeting',fields={'date':'2026-11-28'}))
        result = self.send([query,same],'把组会改到11月28日','same-date-noop')
        self.assertEqual(result['status'],'succeeded')
        receipt = result['operation_result']
        self.assertEqual((receipt['changed'],receipt['committed'],receipt['goal_satisfied']),(False,False,True))
        self.assertEqual(receipt['event']['date'],'2026-11-28')
        self.assertIn('原本已符合要求',result['assistant_message'])
        self.assertIsNone(self.store.repository.receipt(receipt['operation_id']))

    def test_committed_wrong_date_is_reported_and_not_replayed(self):
        self.use_sqlite(Event('meeting','组会','2026-11-28T10:30'))
        target = dict(start='2026-11-28',end='2026-11-28',titles=['组会'],quantity='one')
        plan = dict(action='update',target=target,fields={'date':'2026-11-30'})
        commit = self.gateway.events.commit

        def commit_wrong_date(command, *, operation_id=None):
            wrong = replace(command.event(),due='2026-11-29T10:30')
            return commit(self.gateway.events.preview_upsert(wrong),operation_id=operation_id)

        with patch.object(self.gateway.events,'commit',side_effect=commit_wrong_date) as executor:
            result = self.send([fixture.tool('propose_events',plan)],'组会改到11月30日','committed-wrong-date')
            self.assertEqual(result['status'],'committed_unverified')
            self.assertEqual(result['error_code'],'effect_not_verified')
            receipt = result['operation_result']
            self.assertEqual((receipt['committed'],receipt['goal_satisfied']),(True,False))
            self.assertIsNotNone(self.store.repository.receipt(receipt['operation_id']))
            self.assertEqual(self.gateway.detail('meeting')['date'],'2026-11-29')
            repeated = self.service.confirm_operation(self.sid,receipt['operation_id'],revision=result['pending_operation']['revision'])
            self.assertEqual(repeated['operation_result'],receipt)
            executor.assert_called_once()

    def test_selection_refuses_event_modified_after_candidate_snapshot(self):
        self.use_sqlite(Event('slot-a','组会','2026-11-20T10:00'),Event('slot-b','组会','2026-11-20T14:00'))
        target = dict(start='2026-11-20',end='2026-11-20',titles=['组会'],quantity='one')
        fields = dict(color='#52B788',reminder=30)
        response = self.send([fixture.tool('propose_events',dict(action='update',target=target,fields=fields))],
                             '选一条改成薄荷绿并提前30分钟提醒','candidate-snapshot')
        self.gateway.events.upsert(replace(self.store.event_by_id('slot-b'),notes='另一个客户端已编辑'))
        result = self.service.select_candidate(self.sid,'slot-b',revision=response['selection_revision'])
        self.assertEqual(result['error_code'],'operation_conflict')
        self.assertIsNone(result['operation_result'])
        self.assertEqual(self.gateway.detail('slot-b')['notes'],'另一个客户端已编辑')
        self.assertEqual(self.commands.undo_count,1)

    def test_text_candidates_are_repaired_into_revision_bound_selection(self):
        for reply in ('2','第二个',None):
            with self.subTest(reply=reply):
                self.use_sqlite(Event('slot-a','组会','2026-11-20T10:00',notes='第一项'),
                                Event('slot-b','组会','2026-11-20T14:00'))
                self.sid = self.service.new_session()['session_id']
                offset = len(self.provider.messages)
                first = self.gateway.detail('slot-a')
                bounds = dict(start='2026-11-20',end='2026-11-20')
                fields = dict(color='#52B788',reminder=30)
                plan = dict(action='update',target=dict(bounds,titles=['组会'],quantity='one'),fields=fields)
                request = f'candidate-repair-{offset}'
                response = self.send([fixture.tool('query_events',dict(bounds,keyword='组会')),
                                      fixture.tool('clarify',dict(question='10:00 和 14:00，选择哪个？')),
                                      fixture.tool('propose_events',plan)],
                                     '周五有两个组会，想把其中一个改成薄荷绿并提前半小时提醒。',request)
                self.assertEqual([row['id'] for row in response['candidates']],['slot-a','slot-b'])
                revision = response['selection_revision']
                self.assertEqual(self.service.get_session(self.sid)['event_input']['plan'],plan)
                self.assertEqual(self.commands.undo_count,0)
                self.service = CalendarChatService(self.gateway,self.journal,self.provider,clock=lambda:self.clock)
                restored = self.service.get_pending_operations(self.sid)
                self.assertEqual(restored['selection_revision'],revision)
                self.assertEqual(restored['candidates'],response['candidates'])
                with self.assertRaises(ChatError):
                    self.service.select_candidate(self.sid,'slot-b',revision='stale')
                result = (self.service.select_candidate(self.sid,'slot-b',revision=revision) if reply is None
                          else self.send([],reply,f'{request}-selection'))
                self.assertEqual(len(self.provider.messages)-offset,3)
                receipt = result['operation_result']
                self.assertEqual((receipt['event_id'],receipt['event']['color'],receipt['event']['reminder']),
                                 ('slot-b','#52B788',30))
                self.assertEqual(self.gateway.detail('slot-a'),first)
                self.assertIsNotNone(self.store.repository.receipt(receipt['operation_id']))

    def test_ambiguous_multiday_waits_then_reuses_draft_after_restart(self):
        self.use_sqlite()
        plan = propose('create',dict(fields=dict(title='WB实验',date='2026-11-20',end_date='2026-11-21',time='09:00')))
        with patch('desktop_calendar.assistant.calendar_chat_service.date',wraps=date) as today:
            today.today.return_value = date(2026,11,19)
            result = self.send([plan],'明后天WB实验','ambiguous-days')
            self.assertEqual(result['status'],'confirmation_required')
            operation = result['pending_operation']
            self.assertEqual(self.store.events,[])
            self.service = CalendarChatService(self.gateway,self.journal,self.provider,clock=lambda:self.clock)
            today.today.return_value = date(2026,11,20)
            result = self.send([], '对的','authorize-span')
        row = result['operation_result']['event']
        self.assertEqual((row['date'],row['end'],row['time']),('2026-11-20','2026-11-21','09:00'))
        self.assertEqual(len(self.provider.messages),1)
        repeated = self.service.confirm_operation(self.sid,operation['operation_id'],revision=operation['revision'])
        self.assertEqual(repeated['operation_result'],result['operation_result'])
        self.assertEqual(self.commands.undo_count,1)

    def test_ambiguous_span_can_be_replaced_by_two_independent_members(self):
        self.use_sqlite()
        plan = propose('create',dict(fields=dict(title='WB实验',date='2026-11-20',end_date='2026-11-21',time='09:00')))
        with patch('desktop_calendar.assistant.calendar_chat_service.date',wraps=date) as today:
            today.today.return_value = date(2026,11,19)
            result = self.send([plan],'明后天WB实验','draft-for-split')
            old = result['pending_operation']
            self.assertEqual(self.store.events,[])
            result = self.send([],'每天各一项','replace-by-split')
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(sorted((row['date'],row['end'],row['time']) for row in result['operation_result']['events']),
                         [('2026-11-20','2026-11-20','09:00'),('2026-11-21','2026-11-21','09:00')])
        self.assertEqual(self.service.confirm_operation(self.sid,old['operation_id'],revision=old['revision'])['status'],'cancelled')
        self.assertEqual(self.commands.undo_count,1)

    def test_yes_does_not_pick_between_two_options(self):
        self.use_sqlite()
        span = propose('create',dict(fields=dict(title='WB实验',date='2026-11-20',end_date='2026-11-21')))
        with patch('desktop_calendar.assistant.calendar_chat_service.date',wraps=date) as today:
            today.today.return_value = date(2026,11,19)
            self.send([fixture.tool('clarify',dict(question='一个跨日还是两天各一个？'))],'明后天WB实验','ask-two-options')
            result = self.send([span],'对的','answer-unspecified')
        self.assertEqual(result['status'],'clarification_required')
        self.assertEqual(len(self.provider.messages),1)
        self.assertEqual(self.store.events,[])

    def batch(self):
        return propose('create', *[dict(fields=dict(title='WB实验', date=day)) for day in
                                   ('2026-11-18','2026-11-19')])

    def test_transcript_batch_then_color_then_observe(self):
        self.service.auto_execute = True
        self.send([fixture.tool('clarify', {'question':'跨日还是两个独立事项？'})],
                  '明天后天加WB实验','clarify-dates')
        dates = [(date.today()+timedelta(days=n)).isoformat() for n in (1,2)]
        proposal = propose('create', *[dict(fields=dict(title='WB实验', date=d)) for d in dates])
        created = self.send([proposal], '两个独立实验', 'create-independent')
        first, second = created['operation_result']['events']
        change = propose('update',dict(event_id=second['id'], fields={'color':second['color']}))
        reply = self.send([fixture.tool('query_events', {'keyword':'WB实验'}),change],
                          f"{second['date']} 的WB实验换个颜色，随便换啥都行",'change-random-color')
        self.assertNotEqual(reply['operation_result']['event']['color'], second['color'])
        check = self.send([], '那你看看换颜色了吗','check-result')
        self.assertIn(reply['operation_result']['event']['color'], check['assistant_message'])
        stale = self.send([], '确认','stale-confirm')
        self.assertIsNone(stale['operation_result'])
        self.assertEqual(self.gateway.detail(first['id'])['color'],first['color'])

    def test_one_member_cannot_discharge_two(self):
        result = self.send([propose('create', dict(fields=dict(title='WB实验',date='2026-11-19')))],
                           '建两个独立实验','truncated-plan')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.commands.undo_count,0)
        self.assertIsNone(result['pending_operation'])

    def test_structured_members_keep_distinct_fields_without_plural_keywords(self):
        self.service.auto_execute = True
        members = [
            dict(title='组会', date='2026-11-20', time='10:00', color='#52B788', reminder=15),
            dict(title='报告', date='2026-11-21', time='15:00', color='#8B70D6', reminder=30),
        ]
        result = self.send([propose('create', *(dict(fields=m) for m in members))],
                           '周五加组会，周六交报告，用不同的颜色','two-different-members')
        events = result['operation_result']['events']
        self.assertEqual([{key:event[key] for key in member} for event,member in zip(events,members)],members)
        self.assertEqual(self.commands.undo_count,1)

    def test_creation_clarification_keeps_full_draft_across_restart_and_midnight(self):
        self.service.auto_execute = True
        members = [dict(fields=dict(title='WB实验',date=day,time=clock,color=color)) for day,clock,color in (
            ('2026-11-19','09:00','#52B788'),('2026-11-20','15:00','#8B70D6'))]
        plan = dict(action='create',items=members,distinct_colors=True)
        with patch('desktop_calendar.assistant.calendar_chat_service.date',wraps=date) as today:
            today.today.return_value = date(2026,11,18)
            asked = self.send([fixture.tool('clarify',dict(question='分别创建这两项吗？',create_plan=plan))],
                              '明天和后天分别加 WB 实验','draft-confirmation')
            self.assertEqual(asked['status'],'clarification_required')
            self.assertEqual(self.commands.undo_count,0)
            self.service = CalendarChatService(self.gateway,self.journal,self.provider,clock=lambda:self.clock)
            today.today.return_value = date(2026,11,19)
            accepted = self.send([], '对的对的','confirm-complete-draft')
        self.assertEqual(accepted['status'],'succeeded')
        self.assertEqual([row['date'] for row in accepted['operation_result']['events']],['2026-11-19','2026-11-20'])
        self.assertEqual(len(self.provider.messages),1)
        self.assertEqual(self.commands.undo_count,1)

    def test_creation_nudge_reuses_receipt_and_explicit_repeat_creates_new_id(self):
        self.service.auto_execute = True
        proposal = propose('create',dict(fields=dict(title='组会',date='2026-11-19')))
        created = self.send([proposal],'把组会加回来','restore-meeting')
        self.assertIn('已创建',created['assistant_message'])
        repeated = self.send([], '，添加一下','nudge-creation')
        self.assertIn('上一项创建已完成',repeated['assistant_message'])
        self.assertEqual(repeated['operation_result'],created['operation_result'])
        self.assertEqual(len(self.provider.messages),1)
        self.assertEqual(self.commands.undo_count,1)
        again = self.send([proposal],'再建一条同名组会','explicit-create-again')
        self.assertNotEqual(again['operation_result']['event_id'],created['operation_result']['event_id'])
        self.assertEqual(self.commands.undo_count,2)

    def test_same_day_same_title_batch_creates_independent_ids(self):
        self.service.auto_execute = True
        experiment = dict(fields=dict(title='WB实验',date='2026-11-19',time='10:00'))
        reply = self.send([propose('create',experiment,experiment)],'这天安排两场 WB 实验','same-day-same-title')
        events = reply['operation_result']['events']
        self.assertEqual(len({row['id'] for row in events}),2)
        self.assertEqual(self.commands.undo_count,1)

    def test_nudge_after_lost_chat_publication_reads_original_creation(self):
        self.service.auto_execute = True
        proposal = propose('create',dict(fields=dict(title='组会',date='2026-11-19')))
        write = self.journal.put

        def interrupted(kind, identifier, owner, payload):
            if kind=='session' and payload.get('last_creation'):
                raise OSError('chat publication interrupted')
            return write(kind,identifier,owner,payload)

        with patch.object(self.journal,'put',side_effect=interrupted), self.assertRaises(OSError):
            self.send([proposal],'加一场组会','create-before-chat-interruption')
        self.service = CalendarChatService(self.gateway,self.journal,self.provider,clock=lambda:self.clock)
        repeated = self.send([], '添加一下','nudge-after-chat-interruption')
        self.assertIn('上一项创建已完成',repeated['assistant_message'])
        self.assertEqual(self.commands.undo_count,1)
        self.assertEqual(len(self.provider.messages),1)

    def test_color_noop_and_committed_mismatch_have_distinct_receipts(self):
        self.service.auto_execute = True
        event = self.gateway.detail('event-one')
        target = dict(start=event['date'],end=event['date'],titles=[event['title']],quantity='one')
        plan = dict(action='update',target=target,fields={'color':event['color']})
        unchanged = self.send([fixture.tool('propose_events',plan)],'改成当前有效颜色','unchanged-color')
        self.assertFalse(unchanged['operation_result']['changed'])
        self.assertTrue(unchanged['operation_result']['goal_satisfied'])
        self.assertIn('原本已符合要求',unchanged['assistant_message'])
        self.assertEqual(self.commands.undo_count,0)
        plan['fields']['color'] = '#010203'
        with patch('desktop_calendar.assistant.operation_engine.observe',return_value=None):
            mismatch = self.send([fixture.tool('propose_events',plan)],'换个颜色','effect-mismatch')
        self.assertEqual(mismatch['status'],'committed_unverified')
        self.assertTrue(mismatch['operation_result']['committed'])
        self.assertFalse(mismatch['operation_result']['goal_satisfied'])
        self.assertIn('已提交',mismatch['assistant_message'])
        self.assertEqual(self.commands.undo_count,1)

    def test_split_and_multiday_creation_have_different_member_counts(self):
        self.service.auto_execute = True
        with patch('desktop_calendar.assistant.calendar_chat_service.date',wraps=date) as today:
            today.today.return_value = date(2026,11,18)
            one = dict(fields=dict(title='WB实验',date='2026-11-19'))
            two = dict(fields=dict(title='WB实验',date='2026-11-20'))
            truncated = self.send([propose('create',one)],'明天后天各加一个 WB 实验','truncated-split')
            self.assertEqual(truncated['status'],'failed')
            split = self.send([propose('create',one,two)],'明天后天各加一个 WB 实验','valid-split')
            self.assertEqual(len(split['operation_result']['events']),2)
            invalid = self.send([propose('create',one,two)],'明后天建一个跨日实验','invalid-span')
            self.assertEqual(invalid['status'],'failed')
            span = dict(fields=dict(title='跨日实验',date='2026-11-19',end_date='2026-11-20'))
            valid = self.send([propose('create',span)],'明后天建一个跨日实验','valid-span')
            self.assertEqual(valid['operation_result']['event']['duration_days'],2)
        self.assertEqual(self.commands.undo_count,2)

    def test_structured_delete_date_answer_keeps_all_titles(self):
        plan = dict(action='delete',target=dict(titles=[],quantity='all_matches'))
        reply = self.send([fixture.tool('propose_events',plan)],'最近三天事项全删了','target-draft')
        self.assertEqual(reply['status'],'clarification_required')
        revision = self.service.get_session(self.sid)['awaiting_target']['revision']
        dates = dict(start='2026-11-16',end='2026-11-18',revision=revision)
        reply = self.send([fixture.tool('revise_event_target',dates)],'11-16到11-18','target-dates')
        op = reply['pending_operation']
        self.assertEqual(set(op['preview']['event_ids']),{'event-one','event-two'})
        self.assertEqual(self.commands.undo_count,0)
        first = self.service.confirm_operation(self.sid,op['operation_id'],revision=op['revision'])
        self.assertEqual(self.commands.undo_count,0)
        self.service.confirm_operation(self.sid,op['operation_id'],revision=first['pending_operation']['revision'])
        self.assertEqual(len(self.store.events),0)

    def test_structured_choice_resumes_without_model_and_keeps_fields(self):
        self.service.auto_execute = True
        target = dict(start='2026-11-16',end='2026-11-16',titles=[],quantity='one')
        plan = dict(action='update',target=target,fields=dict(reminder=25),color_policy='different_from_current')
        first = self.send([fixture.tool('propose_events',plan)],'给其中一场换配色和提醒','target-choice')
        self.assertEqual(len(first['candidates']),2)
        row = first['candidates'][1]
        reply = self.send([], '2. '+row['title'],'target-answer')
        actual = reply['operation_result']['event']
        self.assertEqual((actual['id'],actual['reminder']),(row['id'],25))
        self.assertNotEqual(actual['color'],row['color'])
        self.assertEqual(len(self.provider.messages),1)

    def test_structured_group_update_respects_preview(self):
        self.service.auto_execute = True
        target = dict(start='2026-11-16',end='2026-11-16',titles=[],quantity='all_matches')
        plan = dict(action='complete',done=True,target=target,preview_only=True)
        reply = self.send([fixture.tool('propose_events',plan)],'先看看这次调整','preview-group')
        op = reply['pending_operation']
        self.assertEqual(self.commands.undo_count,0)
        self.service.confirm_operation(self.sid,op['operation_id'],revision=op['revision'])
        self.assertTrue(all(e.done for e in self.store.events))
        self.assertEqual(self.commands.undo_count,1)

    def test_missing_title_keeps_patch_for_revision_without_partial_write(self):
        self.service.auto_execute = True
        target = dict(start='2026-11-16',end='2026-11-16',titles=['实验室组会','不存在'],quantity='all_matches')
        plan = dict(action='update',target=target,fields={'reminder':25})
        reply = self.send([fixture.tool('propose_events',plan)],'修改这两场会议的提醒','target-missing')
        self.assertEqual(reply['status'],'clarification_required')
        self.assertEqual(self.commands.undo_count,0)
        revision = self.service.get_session(self.sid)['awaiting_target']['revision']
        patch = dict(revision=revision,titles=['实验室组会'])
        result = self.send([fixture.tool('revise_event_target',patch)],'只改实验室组会','target-narrow')
        self.assertEqual(result['operation_result']['event']['reminder'],25)
        self.assertEqual(self.commands.undo_count,1)
        stale = self.send([fixture.tool('revise_event_target',patch)],'重复','target-stale')
        self.assertEqual(stale['error_code'],'operation_conflict')
        self.assertEqual(self.commands.undo_count,1)

    def test_restart_and_displayed_candidate_preserve_patch(self):
        response = self.send([fixture.tool('query_events',dict(keyword='组会')),
                              propose('update', dict(event_id='event-one',fields=dict(color='#52B788', reminder=30)))],
                             '组会换成薄荷绿，提前30分钟提醒','prepare-choice')
        self.assertEqual(response['status'],'clarification_required')
        self.service = CalendarChatService(self.gateway,self.journal,self.provider, clock=lambda:self.clock)
        result = self.send([], '2. 项目组会','select-visible-row')
        event = result['operation_result']['event']
        self.assertEqual((event['id'],event['color'],event['reminder']),('event-two','#52B788',30))

    def test_batch_preview_revision_and_confirm(self):
        prepared = self.send([self.batch()], '先预览两个独立实验','preview-batch')
        op = prepared['pending_operation']
        self.assertEqual(self.commands.undo_count,0)
        with self.assertRaises(ChatError):
            self.service.confirm_operation(self.sid,op['operation_id'],revision='old')
        result = self.service.confirm_operation(self.sid,op['operation_id'],revision=op['revision'])
        self.assertEqual(len(result['operation_result']['events']),2)
        self.assertEqual(self.commands.undo_count,1)

    def test_cancel_ready_before_claim_never_writes(self):
        prepared = self.send([propose('create',dict(fields=dict(title='WB',date='2026-11-19')))],
                             '先预览 WB','ready-cancel')
        op = prepared['pending_operation']
        op['status'] = 'ready'
        self.journal.put('operation',op['operation_id'],fixture.OWNER,op)
        self.assertEqual(self.service.get_operation_result(self.sid,op['operation_id'])['status'],'ready')
        self.assertEqual(self.service.cancel_operation(self.sid,op['operation_id'])['status'],'cancelled')
        self.assertEqual(self.commands.undo_count,0)

    def test_wrong_member_dates_rejected_before_writing(self):
        self.service.auto_execute = True
        self.send([fixture.tool('clarify', {'question':'每天各一条吗？'})],'明天后天加WB实验','ask-members')
        wrong = dict(fields=dict(title='WB实验',date='2001-01-01'))
        result = self.send([propose('create',wrong,wrong)],'两个独立实验','wrong-day')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.commands.undo_count,0)

    def test_expired_choice_cannot_execute(self):
        response = self.send([fixture.tool('query_events',dict(keyword='组会')),
                              propose('update',dict(event_id='event-one',fields=dict(color='#52B788')))],
                             '组会换成薄荷绿','await-expiry')
        self.clock += 901
        result = self.service.select_candidate(self.sid,'event-two',revision=response['selection_revision'])
        self.assertEqual(result['status'],'expired')
        self.assertEqual(self.commands.undo_count,0)

    def test_sqlite_receipt_after_lost_publication(self):
        store = Store(self.root/'events.sqlite3',repository_factory=SqliteCalendarRepository)
        self.addCleanup(store.repository.close)
        history = EventCommandHistory(store)
        gateway = CalendarActionGateway(store,history)
        self.service = CalendarChatService(gateway,self.journal,self.provider,clock=lambda:self.clock)
        original = self.journal.put
        def lost(kind, identifier, owner, record):
            if kind == 'task' and record['status']=='completed':
                raise OSError('receipt publication interrupted')
            return original(kind,identifier,owner,record)
        with patch.object(self.journal,'put',side_effect=lost):
            result = self.send([self.batch()],'创建两个独立实验','lost-receipt')
        task_id = result['task']['task_id']
        self.assertIsNotNone(store.repository.receipt(task_id))
        self.service = CalendarChatService(gateway,self.journal,self.provider)
        self.assertEqual(self.service.get_task(self.sid, task_id)['status'],'completed')
        self.assertEqual(len(store.events),2)
        self.assertEqual(history.undo_count,1)
