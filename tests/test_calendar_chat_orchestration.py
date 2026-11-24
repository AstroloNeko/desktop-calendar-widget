"""Regression for tool batches vs single-event writes and intent-scoped patches."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import patch
import unittest

from desktop_calendar.assistant.calendar_chat_tools import ChatError
from desktop_calendar.assistant.calendar_chat_service import OWNER, MAX_ROUNDS
import test_calendar_chat as fixture

tool = fixture.tool


def batch(*calls):
    items = []
    for index, message in enumerate(calls):
        call = message['tool_calls'][0].copy()
        call['id'] = 'batch_'+str(index)
        items.append(call)
    return {'content': None, 'tool_calls': items}


class OrchestrationTests(unittest.TestCase):
    reference_now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    setUp = fixture.ChatTests.setUp
    send = fixture.ChatTests.send
    select = fixture.ChatTests.select
    pending = fixture.ChatTests.pending

    def assert_protocol(self):
        for messages in self.provider.messages:
            for index, message in enumerate(messages):
                if message.get('tool_calls'):
                    ids = [call['id'] for call in message['tool_calls']]
                    replies = messages[index+1:index+1+len(ids)]
                    self.assertEqual([reply.get('tool_call_id') for reply in replies], ids)
                    self.assertTrue(all(reply['role'] == 'tool' for reply in replies))

    def test_multiple_reads_in_one_response_all_get_results(self):
        result = self.send([batch(tool('list_categories', {}), tool('query_events', {'keyword': '实验室'}))], '找实验室组会')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['candidates'][0]['id'], 'event-one')
        self.assert_protocol()

    def test_search_then_detail_then_title_patch_in_separate_rounds(self):
        before = asdict(self.store.event_by_id('event-one'))
        result = self.send([tool('query_events', {'keyword': '实验室'}), tool('get_event', {'event_id': 'event-one'}),
                            tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}})],
                           '把实验室组会那个改成动物实验')
        op = result['pending_operation']
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(op['preview']['changed_fields'], ['title'])
        self.assertEqual(asdict(self.store.event_by_id('event-one')), before)
        self.assert_protocol()

    def test_two_reads_plus_one_proposal_in_same_response(self):
        result = self.send([batch(tool('query_events', {'keyword': '实验室'}), tool('get_event', {'event_id': 'event-one'}),
                                 tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}}))],
                           '把实验室组会改成动物实验')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(self.commands.undo_count, 0)
        self.service.confirm_operation(self.sid, result['pending_operation']['operation_id'])
        self.assertEqual(self.commands.undo_count, 1)

    def test_write_listed_before_reads_is_preflighted_and_ordered(self):
        result = self.send([batch(tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}}),
                                 tool('query_events', {'keyword': '实验室'}))], '把实验室组会改成动物实验')
        self.assertEqual(result['status'], 'confirmation_required')

    def test_two_proposals_rejected_before_reads_or_pending_mutation(self):
        before = self.store.data_file.read_bytes()
        with patch.object(self.gateway, 'query', wraps=self.gateway.query) as query:
            result = self.send([batch(tool('query_events', {'keyword': '实验室'}),
                                     tool('preview_create', {'title': '甲', 'date': '2026-11-20'}),
                                     tool('preview_create', {'title': '乙', 'date': '2026-11-20'}))], '添加一件事情')
            query.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['pending_operation'])
        self.assertEqual(before, self.store.data_file.read_bytes())
        self.assertEqual(self.journal.all('operation', OWNER), [])
        self.assert_protocol()
        self.assertNotIn('第一阶段', result['error_message'])

    def test_bad_batch_can_replan_one_proposal_without_partial_pending(self):
        bad = batch(tool('preview_create', {'title': '甲', 'date': '2026-11-20'}),
                    tool('preview_create', {'title': '乙', 'date': '2026-11-20'}))
        result = self.send([bad, tool('preview_create', {'title': '会议', 'date': '2026-11-20'})], '11.20开会')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(len(self.journal.all('operation', OWNER)), 1)
        self.assert_protocol()

    def test_two_updates_same_target_still_need_a_single_replanned_patch(self):
        self.select()
        bad = batch(tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '甲'}}),
                    tool('preview_update', {'event_id': 'event-one', 'changes': {'time': '16:00'}}))
        result = self.send([bad], '改名为甲，同时改到四点')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.journal.all('operation', OWNER), [])

    def test_invalid_member_prevents_any_proposal_and_safe_retry_works(self):
        result = self.send([batch(tool('preview_create', {'title': '甲', 'date': '2026-11-20'}),
                                 tool('delete', {'event_id': 'event-one'}))], '添加甲')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.journal.all('operation', OWNER), [])
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22'})], '11.22开会', 'next-valid-request')
        self.assertEqual(result['status'], 'confirmation_required')

    def test_invalid_json_can_repair_and_each_id_has_a_result(self):
        bad = tool('preview_create', {})
        bad['tool_calls'][0]['function']['arguments'] = '{bad'
        result = self.send([bad, tool('preview_create', {'title': '会议', 'date': '2026-11-22'})], '11.22开会')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assert_protocol()

    def test_repeated_read_uses_cache_then_stops_no_progress(self):
        reads = [tool('list_categories', {})]*MAX_ROUNDS
        with patch.object(self.gateway, 'categories', wraps=self.gateway.categories) as categories:
            result = self.send(reads, '有哪些分类')
            self.assertEqual(categories.call_count, 1)
        self.assertEqual(result['error_code'], 'tool_loop')
        self.assertLessEqual(len(self.provider.messages), MAX_ROUNDS)
        self.assert_protocol()

    def test_distinct_reads_hit_round_cap_without_writing(self):
        result = self.send([tool('query_events', {'keyword': str(i)}) for i in range(MAX_ROUNDS+2)], '查询事项')
        self.assertEqual(result['error_code'], 'tool_loop')
        self.assertEqual(len(self.provider.messages), MAX_ROUNDS)
        self.assertEqual(self.commands.undo_count, 0)

    def test_timeout_and_next_message_keep_previous_target_and_pending(self):
        self.select()
        original = self.send([tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '细胞实验'}})], '改名为细胞实验')['pending_operation']
        failed = self.send([TimeoutError('secret content')], '查询明天')
        self.assertEqual(failed['error_code'], 'model_unavailable')
        self.assertEqual(failed['pending_operation'], original)
        self.assertEqual(self.service.get_session(self.sid)['selected'], 'event-one')
        self.assertNotIn('secret', str(failed))
        next_result = self.send([tool('preview_update', {'event_id': 'event-one', 'changes': {'time': '16:00'}})], '改到四点')
        self.assertEqual(next_result['pending_operation']['preview']['after']['title'], '细胞实验')

    def test_existing_pending_is_not_refined_by_a_failed_multi_write_batch(self):
        self.select()
        original = self.send([tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '细胞实验'}})], '改名为细胞实验')['pending_operation']
        result = self.send([batch(tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '坏标题'}}),
                                 tool('preview_create', {'title': '另一件', 'date': '2026-11-20'}))], '改名为动物实验')
        self.assertEqual(result['pending_operation'], original)
        self.assertEqual(self.service.get_pending_operations(self.sid)['pending_operation'], original)

    def test_title_only_preserves_seconds_snooze_reminders_and_all_other_fields(self):
        event = self.store.event_by_id('event-one')
        event.due = '2026-11-16T15:00:37'
        event.duration_days = 3
        event.snooze_until = '2026-11-16T15:30:00'
        event.end_as_ddl = True
        self.store.save()
        self.select()
        before = asdict(event)
        noisy = dict(title='动物实验', date='2026-11-22', time='10:00', reminder=None, category_id=None,
                     color='#ff0000', color_mode='inherit', duration_days=1, end_as_ddl=False,
                     skip_non_working_days=True, notes='不应覆盖', event_type='general')
        result = self.send([tool('preview_update', {'event_id': event.id, 'changes': noisy})], '把实验室组会改成动物实验')
        op = result['pending_operation']
        self.assertEqual(op['preview']['changed_fields'], ['title'])
        self.service.confirm_operation(self.sid, op['operation_id'])
        self.assertEqual(asdict(self.store.event_by_id(event.id)), {**before, 'title': '动物实验'})
        self.commands.undo()
        self.assertEqual(asdict(self.store.event_by_id(event.id)), before)

    def test_date_only_patch_preserves_original_clock_and_precision(self):
        event = self.store.event_by_id('event-one')
        event.due = '2026-11-16T15:00:37'
        self.select()
        result = self.send([tool('preview_update', {'event_id': event.id, 'changes': {'date': '2026-11-18', 'time': '10:00'}})], '改到后天')
        self.assertEqual(result['pending_operation']['preview']['after']['due'], '2026-11-18T15:00:37')

    def test_case_a_wb_creation_uses_defaults_not_model_filled_fields(self):
        tomorrow = (date.today()+timedelta(days=1)).isoformat()
        result = self.send([batch(tool('list_categories', {}), tool('preview_create', {
            'title': 'WB实验', 'date': tomorrow, 'time': '16:00', 'duration_days': 3,
            'color': '#ff0000', 'color_mode': 'inherit', 'category_id': None, 'reminder': 30}))], '明天我要做个WB实验')
        op = result['pending_operation']
        expected = self.store.make_quick_event('WB实验', date.fromisoformat(tomorrow))
        for key in ('duration_days', 'color', 'color_mode', 'category_id', 'has_time', 'reminder', 'event_type', 'due'):
            self.assertEqual(op['preview']['after'][key], asdict(expected)[key], key)

    def test_case_b_no_category_inherit_is_legal_at_gateway_and_service(self):
        preview = self.gateway.preview('preview_create', {'title': '会议', 'date': '2026-11-22', 'color_mode': 'inherit', 'category_id': None}, 'temporary-id')
        self.assertEqual(preview['after']['color_mode'], 'override')
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22', 'color_mode': 'inherit'})], '11.22开会')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertFalse(result['pending_operation']['preview']['after']['has_time'])

    def test_explicit_category_follows_color_and_explicit_override_stays(self):
        category = self.store.create_category('商务', '#123456')
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22', 'category_id': category.id})], '11.22开会，分类为商务')
        self.assertEqual(result['pending_operation']['preview']['after']['color'], '#123456')
        self.service.cancel_operation(self.sid, result['pending_operation']['operation_id'])
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22', 'category_id': category.id, 'color': '#abcdef'})], '11.22开会，分类商务，独立颜色#abcdef')
        self.assertEqual(result['pending_operation']['preview']['after']['color_mode'], 'override')
        self.assertEqual(result['pending_operation']['preview']['after']['color'], '#abcdef')

    def test_created_event_detail_and_followup_rename_time_date_completion(self):
        result = self.send([tool('preview_create', {'title': 'WB实验', 'date': '2026-11-20'})], '帮我创建明天的WB实验')
        op = result['pending_operation']
        self.service.confirm_operation(self.sid, op['operation_id'])
        identifier = op['preview']['event_id']
        for text, name, fields in [
            ('把刚才那个改名为细胞实验', 'preview_update', {'title': '细胞实验'}),
            ('把刚才那个改到四点', 'preview_update', {'time': '16:00'}),
            ('改到后天', 'preview_update', {'date': '2026-11-22'}),
            ('把刚才那个标记完成', 'preview_complete', {'done': True}),
            ('刚才那个还没做，取消完成', 'preview_complete', {'done': False})]:
            args = {'event_id': identifier, **({'changes': fields} if name == 'preview_update' else fields)}
            result = self.send([batch(tool('get_event', {'event_id': identifier}), tool(name, args))], text)
            self.assertEqual(result['status'], 'confirmation_required', text)
            self.service.confirm_operation(self.sid, result['pending_operation']['operation_id'])
        self.assertEqual(self.store.event_by_id(identifier).title, '细胞实验')
        self.assertEqual(self.store.event_by_id(identifier).due, '2026-11-22T16:00')
        self.assertFalse(self.store.event_by_id(identifier).done)

    def test_ambiguous_titles_are_not_chosen_by_model_even_in_one_batch(self):
        result = self.send([batch(tool('query_events', {'keyword': '组会'}),
                                 tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}}))], '把组会改名为动物实验')
        self.assertEqual(result['status'], 'clarification_required')
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(self.journal.all('operation', OWNER), [])

    def test_query_only_today_tomorrow_week_unfinished_and_cancel_stay_readonly(self):
        for text in ('明天有什么安排', '这周还有哪些任务没完成', '今天有什么安排'):
            before = self.store.data_file.read_bytes()
            result = self.send([tool('query_events', {'done': False})], text)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(before, self.store.data_file.read_bytes())
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22'})], '11.22开会')
        self.service.cancel_operation(self.sid, result['pending_operation']['operation_id'])
        self.assertEqual(self.commands.undo_count, 0)

    def test_diagnostics_contain_names_counts_field_paths_not_private_values(self):
        request = 'diagnostic-request-1'
        self.send([batch(tool('list_categories', {}), tool('preview_create', {'title': 'PRIVATE TITLE', 'date': '2026-11-22', 'color_mode': 'inherit'}))],
                  '11.22开会 PRIVATE USER CONTENT', request)
        data = self.service.get_diagnostics(request)
        self.assertEqual(data['batches'][0]['count'], 2)
        self.assertEqual([row['kind'] for row in data['batches'][0]['tools']], ['read', 'proposal'])
        self.assertIn('color_mode', data['batches'][0]['tools'][1]['ignored_fields'])
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertNotIn('api_key', json.dumps(data))

    def test_invalid_parameter_field_is_logged_without_value(self):
        self.send([tool('preview_create', {'title': 'private', 'date': '2026-11-22', 'duration_days': 999})], '持续三天的会议', 'field-error-req')
        entry = self.service.get_diagnostics('field-error-req')['batches'][0]['tools'][0]
        self.assertEqual(entry['error_field'], 'arguments.duration_days')
        self.assertNotIn('999', json.dumps(entry))

    def test_repeated_high_frequency_scenarios_remain_consistent(self):
        before = self.store.data_file.read_bytes()
        for index in range(12):
            result = self.send([batch(tool('list_categories', {}), tool('preview_create', {
                'title': 'WB实验', 'date': '2026-11-22', 'color_mode': 'inherit', 'duration_days': 3}))],
                '明天我要做个WB实验', 'repeat-natural-'+str(index))
            self.assertEqual(result['status'], 'confirmation_required')
            self.assertEqual(result['pending_operation']['preview']['after']['duration_days'], 1)
            self.service.cancel_operation(self.sid, result['pending_operation']['operation_id'])
        self.assertEqual(self.store.data_file.read_bytes(), before)

    def test_read_tool_timeout_has_structured_diagnostic_and_no_context_loss(self):
        self.select()
        with patch.object(self.gateway, 'query', side_effect=TimeoutError('private path')):
            result = self.send([tool('query_events', {'keyword': '实验室'})], '找实验室组会', 'read-timeout-request')
        self.assertEqual(result['error_code'], 'tool_unavailable')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.service.get_session(self.sid)['selected'], 'event-one')
        self.assertEqual(self.commands.undo_count, 0)

    def test_bad_color_field_repairs_and_reports_field_name_not_value(self):
        result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22', 'color': 'private-invalid-color'}),
                            tool('preview_create', {'title': '会议', 'date': '2026-11-22'})],
                           '11.22开会', 'color-repair-request')
        self.assertEqual(result['status'], 'confirmation_required')
        diagnostic = self.service.get_diagnostics('color-repair-request')
        self.assertEqual(diagnostic['batches'][0]['tools'][0]['error_field'], 'arguments.color')
        self.assertNotIn('private-invalid-color', json.dumps(diagnostic))

    def test_request_deadline_expires_before_any_proposal(self):
        before = self.store.data_file.read_bytes()
        with patch('desktop_calendar.assistant.calendar_chat_model.time.monotonic', side_effect=[0, 0, 91]):
            result = self.send([tool('preview_create', {'title': '会议', 'date': '2026-11-22'})], '11.22开会')
        self.assertEqual(result['error_code'], 'planning_timeout')
        self.assertEqual(self.store.data_file.read_bytes(), before)
        self.assertEqual(self.journal.all('operation', OWNER), [])

    def test_reasoning_and_each_call_result_preserved_for_next_provider_round(self):
        first = batch(tool('list_categories', {}), tool('query_events', {'keyword': '实验室'}))
        first['reasoning_content'] = 'synthetic reasoning marker'
        self.send([first], '找实验室组会')
        assistant = next(message for message in self.provider.messages[1] if message.get('tool_calls'))
        self.assertEqual(assistant['reasoning_content'], 'synthetic reasoning marker')
        self.assert_protocol()

    def test_just_created_reference_is_not_redirected_by_an_unrelated_search(self):
        result = self.send([tool('preview_create', {'title': 'WB实验', 'date': '2026-11-20'})], '明天做WB实验')
        op = result['pending_operation']
        self.service.confirm_operation(self.sid, op['operation_id'])
        identifier = op['preview']['event_id']
        result = self.send([batch(tool('query_events', {'keyword': '项目组会'}),
                                 tool('get_event', {'event_id': identifier}),
                                 tool('preview_update', {'event_id': identifier, 'changes': {'title': '细胞实验'}}))],
                           '把刚才那个改名为细胞实验')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(result['pending_operation']['preview']['event_id'], identifier)

    def test_rename_titles_containing_field_words_do_not_enable_those_fields(self):
        self.select()
        for text, title in [('把实验室组会改成颜色研究', '颜色研究'), ('把事项改名为持续三天提醒', '持续三天提醒')]:
            result = self.send([tool('preview_update', {'event_id': 'event-one', 'changes': {
                'title': title, 'reminder': 30, 'duration_days': 3, 'color': '#ff0000'}})], text)
            self.assertEqual(result['pending_operation']['preview']['changed_fields'], ['title'])
            self.service.cancel_operation(self.sid, result['pending_operation']['operation_id'])

    def test_choice_continuation_keeps_target_through_a_read_batch(self):
        result = self.send([batch(tool('query_events', {'keyword': '组会'}),
                                 tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}}))], '把组会改名为动物实验')
        self.assertEqual(result['status'], 'clarification_required')
        self.service.select_candidate(self.sid, 'event-one')
        result = self.send([batch(tool('query_events', {'keyword': '项目组会'}),
                                 tool('preview_update', {'event_id': 'event-one', 'changes': {'title': '动物实验'}}))], '继续')
        self.assertEqual(result['pending_operation']['preview']['event_id'], 'event-one')

    def test_generic_date_change_is_not_mistaken_for_rename(self):
        self.select()
        result = self.send([tool('preview_update', {'event_id': 'event-one', 'changes': {'date': '2026-11-22'}})], '把会议改成11.22')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(result['pending_operation']['preview']['changed_fields'], ['due'])
