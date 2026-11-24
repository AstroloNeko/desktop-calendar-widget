"""Synthetic properties, automatic single-event policy and category assignment."""
from dataclasses import asdict
from datetime import datetime, timezone
import unittest
import test_calendar_chat as fixture
from calendar_core import EventCategory, COLORS
from desktop_calendar.assistant.calendar_chat_attributes import adapt_attributes


class AttributeTests(unittest.TestCase):
    reference_now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    setUp = fixture.ChatTests.setUp
    send = fixture.ChatTests.send
    select = fixture.ChatTests.select

    def catalog(self):
        self.store.categories = [EventCategory('lab', '实验', '#123456'), EventCategory('meet', '开会'),
                                 EventCategory('default', '默认')]
        return self.gateway.categories()['categories']

    def create(self, text, **fields):
        self.service.auto_execute = True
        return self.send([fixture.tool('preview_create', dict(title='测试事项', date='2026-11-23', **fields))], text)

    def test_palette_and_all_gui_fields_combined(self):
        self.catalog()
        result = self.create('11.23下午三点WB，紫色，持续三天，跳过休息日，最后一天作为DDL，紧急，提前半小时提醒，备注是先准备抗体',
                             time='15:00', duration_days=3, skip_non_working_days=True, end_as_ddl=True,
                             event_type='urgent', reminder=30, notes='先准备抗体', category_id='lab', category_basis='semantic_high')
        self.assertEqual(result['status'], 'succeeded')
        event = self.store.event_by_id(result['operation_result']['event']['id'])
        self.assertEqual((event.color, event.color_mode, event.category_id), (COLORS['鸢尾紫'], 'override', 'lab'))
        self.assertEqual((event.duration_days, event.skip_non_working_days, event.end_as_ddl, event.reminder), (3, True, True, 30))
        self.assertEqual(event.notes, '先准备抗体')
        self.assertEqual(self.commands.undo_count, 1)
        self.refresh.assert_called_once()

    def test_semantic_category_without_color_inherits(self):
        self.catalog()
        result = self.create('明天PCR', category_id='lab', category_basis='semantic_high')
        event = self.store.event_by_id(result['operation_result']['event']['id'])
        self.assertEqual((event.category_id, event.color_mode), ('lab', 'inherit'))
        self.assertEqual(self.store.effective_event_color(event), '#123456')
        self.assertEqual(result['pending_operation']['preview']['field_provenance']['inferred'], ['category_id'])

    def test_explicit_none_overrides_inference(self):
        self.catalog()
        result = self.create('买耳机，不分类，紫色', category_id='lab', category_basis='semantic_high', color_mode='inherit')
        event = self.store.event_by_id(result['operation_result']['event']['id'])
        self.assertEqual((event.category_id, event.color_mode, event.color), (None, 'override', COLORS['鸢尾紫']))

    def test_explicit_category_beats_inference_and_default_is_real(self):
        categories = self.catalog()
        for name, identifier in [('开会', 'meet'), ('默认', 'default')]:
            scoped, _, provenance = adapt_attributes({'title': 'x', 'date': '2026-11-23', 'category_id': 'lab',
                  'category_basis': 'semantic_high'}, '放到'+name+'分类', True, categories)
            self.assertEqual(scoped['category_id'], identifier)
            self.assertEqual(provenance['inferred'], [])

    def test_no_category_no_optional_pollution(self):
        result = self.create('11.22开会', color_mode='inherit', duration_days=3, reminder=None, event_type='urgent')
        event = self.store.event_by_id(result['operation_result']['event']['id'])
        self.assertEqual((event.duration_days, event.category_id, event.has_time, event.event_type), (1, None, False, 'general'))

    def test_unknown_category_safe_refusal(self):
        before = self.store.data_file.read_bytes()
        result = self.create('归到不存在分类', category_id='missing')
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(before, self.store.data_file.read_bytes())

    def test_unknown_explicit_name_cannot_fall_back_to_real_but_wrong_id(self):
        from desktop_calendar.assistant.calendar_chat_tools import ChatError
        with self.assertRaises(ChatError):
            adapt_attributes({'title': 'x', 'date': '2026-11-23', 'category_id': 'lab'},
                             '归到不存在分类', True, self.catalog())

    def test_range_uses_store_workday_and_end_deadline(self):
        from datetime import date
        self.store.set_date_status(date(2026, 11, 24), 'leave')
        result = self.create('11月23日到11月26日做实验，跳过休息日，最后一天作为DDL',
                             end_date='2026-11-26', skip_non_working_days=True, end_as_ddl=True)
        self.assertEqual(result['status'], 'succeeded')
        event = self.store.event_by_id(result['operation_result']['event']['id'])
        self.assertEqual(self.store.event_dates(event)[-1].isoformat(), '2026-11-26')
        self.assertEqual(event.duration_days, 3)

    def test_time_required_for_reminder(self):
        result = self.create('周三交报告，DDL，提前一天提醒', event_type='ddl', reminder=1440)
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.store.events), 2)

    def test_rename_and_color_patch_do_not_touch_other_fields(self):
        self.catalog()
        self.select()
        self.service.auto_execute = True
        before = asdict(self.store.event_by_id('event-one'))
        result = self.send([fixture.tool('preview_update', {'event_id': 'event-one', 'changes': {
            'title': '不应改名', 'color': '#abcdef', 'category_id': 'lab', 'category_basis': 'semantic_high', 'duration_days': 8}})],
            '把那个实验改成绿色')
        self.assertEqual(result['status'], 'succeeded')
        after = asdict(self.store.event_by_id('event-one'))
        self.assertEqual([k for k in after if after[k] != before[k]], ['color'])
        self.assertEqual(after['color'], COLORS['薄荷绿'])

    def test_category_change_preserves_existing_override(self):
        self.catalog()
        self.select()
        self.service.auto_execute = True
        before = self.store.event_by_id('event-one').color
        result = self.send([fixture.tool('preview_update', {'event_id': 'event-one', 'changes': {'category_id': 'lab'}})], '归到实验分类')
        self.assertEqual(result['status'], 'succeeded')
        event = self.store.event_by_id('event-one')
        self.assertEqual((event.category_id, event.color_mode, event.color), ('lab', 'override', before))

    def test_explicit_inherit_then_single_event_multifield_patch(self):
        self.catalog()
        self.select()
        self.service.auto_execute = True
        result = self.send([fixture.tool('preview_update', {'event_id': 'event-one', 'changes': {'category_id': 'lab', 'color_mode': 'inherit'}})],
                           '归到实验分类，跟随分类颜色')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.event_by_id('event-one').color_mode, 'inherit')

    def test_notes_cannot_authorize_side_effects(self):
        scoped, _, _ = adapt_attributes({'title': 'WB', 'date': '2026-11-23', 'notes': '紧急，红色', 'event_type': 'urgent'},
                                         '明天WB，备注是紧急，红色', True, [])
        self.assertNotIn('event_type', scoped)
        self.assertNotIn('color', scoped)
        self.assertEqual(scoped['notes'], '紧急，红色')

    def test_rename_to_color_or_category_words_stays_title_only(self):
        for text in ['改名为绿色实验', '改名为实验类', '改名为不分类']:
            scoped, _, _ = adapt_attributes({'title': text, 'color': '#abcdef', 'category_id': 'lab'}, text, False, self.catalog())
            self.assertEqual(set(scoped), {'title'})

    def test_yearless_past_date_clarifies_not_rollover(self):
        from datetime import date
        from unittest.mock import patch
        from desktop_calendar.assistant.calendar_chat_tools import ChatError
        with patch('desktop_calendar.assistant.calendar_chat_attributes.date') as local:
            local.today.return_value = date(2026, 11, 13)
            local.side_effect = date
            with self.assertRaises(ChatError):
                adapt_attributes({'title': 'x', 'date': '2027-01-02'}, '1.2开会', True, [])
            scoped, _, _ = adapt_attributes({'title': 'x', 'date': '2027-11-23'}, '11.23开会', True, [])
            self.assertEqual(scoped['date'], '2026-11-23')

    def test_query_cannot_become_automatic_creation(self):
        result = self.create('明天有什么安排')
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.store.events), 2)

    def test_delete_requires_confirmation_and_undo_preserves_id(self):
        self.select()
        self.service.auto_execute = True
        before = asdict(self.store.event_by_id('event-one'))
        result = self.send([fixture.tool('preview_delete', {'event_id': 'event-one'})], '删除那个实验')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(asdict(self.store.event_by_id('event-one')), before)
        op = result['pending_operation']
        result = self.service.confirm_operation(self.sid, op['operation_id'])
        self.assertTrue(result['operation_result']['deleted'])
        self.assertIsNone(self.store.event_by_id('event-one'))
        self.service.confirm_operation(self.sid, op['operation_id'])
        self.assertEqual(self.commands.undo_count, 1)
        self.commands.undo()
        self.assertEqual(asdict(self.store.event_by_id('event-one')), before)

    def test_explicit_preview_opt_out_and_global_preference_denied(self):
        result = self.create('先预览不要执行，明天WB')
        self.assertEqual(result['status'], 'confirmation_required')
        response = self.send([], '以后做实验都自动放到实验分类')
        self.assertEqual(response['status'], 'clarification_required')
        self.assertEqual(len(self.store.events), 2)

    def test_preview_opt_out_persists_while_refining(self):
        result = self.create('先预览不要执行，明天WB')
        identifier = result['pending_operation']['preview']['event_id']
        result = self.send([fixture.tool('preview_update', {'event_id': identifier, 'changes': {'title': '新名称'}})], '把刚才那个改名为新名称')
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(len(self.store.events), 2)

    def test_same_event_category_color_patch_and_catalog_refresh(self):
        self.catalog()
        self.select()
        self.service.auto_execute = True
        result = self.send([fixture.tool('preview_update', {'event_id': 'event-one', 'changes': {'category_id': 'lab', 'color': '#000000'}})],
                           '把明天那个WB改成绿色，再归到实验类')
        self.assertEqual(result['status'], 'succeeded')
        event = self.store.event_by_id('event-one')
        self.assertEqual((event.category_id, event.color), ('lab', COLORS['薄荷绿']))
        self.store.categories[0].name = '科研'
        self.assertEqual(next(c['name'] for c in self.gateway.categories()['categories'] if c['id'] == 'lab'), '科研')

    def test_auto_execution_is_idempotent_and_completion_patch(self):
        self.service.auto_execute = True
        replies = [fixture.tool('preview_create', {'title': 'WB', 'date': '2026-11-23'})]
        result = self.send(replies, '明天WB', 'attribute-idempotency')
        repeat = self.send([], '明天WB', 'attribute-idempotency')
        self.assertEqual(result, repeat)
        identifier = result['operation_result']['event']['id']
        result = self.send([fixture.tool('preview_complete', {'event_id': identifier, 'done': True})], '把刚才那个标记完成')
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(self.store.event_by_id(identifier).done)
