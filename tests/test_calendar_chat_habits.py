"""Temporary Store Habit 2.0 AI creation, no real profile or network."""
from dataclasses import asdict
from datetime import date
from unittest.mock import Mock, patch
import unittest

import test_calendar_chat as fixture
from calendar_core import COLORS, Store, RoutineItem
from calendar_chat_tools import ChatError, validate_call
from data_backup import create_backup, read_backup, restore_store


class HabitChatTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    send = fixture.ChatTests.send

    def create(self, message, **fields):
        self.service.auto_execute = True
        self.gateway.habit_refresh = Mock()
        return self.send([fixture.tool('preview_create_habit', dict(title='测试习惯', frequency='daily', **fields))], message)

    def habit(self, message, frequency='daily', **fields):
        self.service.auto_execute = True
        return self.send([fixture.tool('preview_create_habit', dict(title='测试习惯', frequency=frequency, **fields))], message)

    def test_daily_default_and_real_persistence_no_event(self):
        result = self.create('每天背单词，绿色')
        self.assertEqual(result['status'], 'succeeded')
        item = self.store.routines[0]
        self.assertEqual((item.kind, item.frequency, item.interval, item.color, item.workdays_only), ('habit', 'daily', 1, COLORS['薄荷绿'], False))
        self.assertEqual(len(self.store.events), 2)
        self.assertEqual(asdict(Store(self.store.data_file).routines[0]), asdict(item))
        self.gateway.habit_refresh.assert_called_once()

    def test_daily_intervals_do_not_drift(self):
        for n in (2, 3, 7):
            result = self.habit(f'每隔{n}天做PCR', interval=999)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.store.routines[-1].interval, n)

    def test_monday_and_multi_weekday_verified_not_model_label(self):
        for text, expected in [('每周一开组会，紫色', [0]), ('每周一三五健身', [0, 2, 4]), ('每周一和周四开会', [0, 3])]:
            result = self.habit(text, 'weekly', weekdays=[1])
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.store.routines[-1].weekdays, expected)
        self.assertEqual(self.store.routines[0].color, COLORS['鸢尾紫'])

    def test_next_monday_from_friday(self):
        with patch('calendar_habit_gateway.date') as clock:
            clock.today.return_value = date(2026, 11, 13)
            clock.side_effect = date
            result = self.habit('每周一开组会', 'weekly', weekdays=[0])
            self.assertEqual(result['pending_operation']['preview']['next_on'], '2026-11-16')

    def test_monthly_and_yearly_missing_dates_follow_existing_rules(self):
        for frequency, text, fields in [('monthly', '每月31日交月报', dict(month_day=31)),
                                        ('yearly', '每年2月29日纪念日', dict(month_day=29, year_month=2))]:
            result = self.habit(text, frequency, **fields)
            self.assertEqual(result['status'], 'succeeded')
            item = self.store.routines[-1]
            item.start_on = '2024-01-01'
            if frequency == 'monthly':
                self.assertFalse(self.store.routine_planned_on(item, date(2024, 2, 29)))
                self.assertTrue(self.store.routine_planned_on(item, date(2024, 3, 31)))
            else:
                self.assertTrue(self.store.routine_planned_on(item, date(2024, 2, 29)))
                self.assertFalse(self.store.routine_planned_on(item, date(2025, 2, 28)))

    def test_every_month_wording_and_relative_start_defaults(self):
        for phrase in ('每个月15号交月报', '每月15号交月报'):
            result = self.habit(phrase, 'monthly', month_day=15)
            self.assertEqual(result['status'], 'succeeded')
        with patch('calendar_habit_gateway.date') as clock:
            clock.today.return_value = date(2026, 11, 13)
            clock.side_effect = date
            for phrase, expected in [('明天开始', '2026-11-14'), ('下周开始', '2026-11-16'), ('下个月开始', '2026-12-01')]:
                result = self.habit('每天背单词，'+phrase)
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(self.store.routines[-1].start_on, expected)

    def test_custom_intervals_all_units(self):
        for frequency, unit, n in [('weekly', '周', 2), ('monthly', '个月', 3), ('yearly', '年', 2)]:
            result = self.habit(f'每隔{n}{unit}整理记录', frequency, interval=n, weekdays=[0])
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.store.routines[-1].interval, n)

    def test_start_end_workdays_and_reminder(self):
        result = self.habit('每天背单词，从2026年12月1日开始，2026年12月31日结束，仅工作日，9点提醒我',
                            start_on='2026-12-01', end_on='2026-12-31', workdays_only=True,
                            reminder_enabled=True, reminder_time='09:00')
        self.assertEqual(result['status'], 'succeeded')
        item = self.store.routines[0]
        self.assertEqual((item.start_on, item.end_on, item.workdays_only, item.reminder_time), ('2026-12-01', '2026-12-31', True, '09:00'))

    def test_unsolicited_defaults_not_invented(self):
        result = self.habit('每天背单词', color='#ffffff', reminder_enabled=True, reminder_time='18:00',
                            start_on='2099-01-01', end_on='2099-12-31', workdays_only=True)
        self.assertEqual(result['status'], 'succeeded')
        item = self.store.routines[0]
        self.assertEqual((item.start_on, item.end_on, item.reminder_enabled, item.color), (date.today().isoformat(), None, False, COLORS['薄荷绿']))

    def test_explicit_no_reminder_beats_model_default(self):
        result = self.habit('每天背单词，不提醒', reminder_enabled=True, reminder_time='09:00')
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(self.store.routines[0].reminder_enabled)

    def test_model_cannot_invent_clock_when_user_only_asks_reminder(self):
        result = self.habit('每天背单词，提醒我', reminder_enabled=True, reminder_time='18:00')
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.routines, [])

    def test_palette_all_colors(self):
        for text, color in COLORS.items():
            result = self.habit('每天背单词，'+text)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.store.routines[-1].color, color)

    def test_invalid_rules_no_write(self):
        before = self.store.data_file.read_bytes()
        for text, frequency, fields in [('每周开会', 'weekly', {}), ('每年4月31日纪念日', 'yearly', dict(year_month=4, month_day=31)),
                                          ('每天背单词，提醒我', 'daily', dict(reminder_enabled=True)),
                                          ('每月最后一天总结', 'monthly', dict(month_day=31))]:
            result = self.habit(text, frequency, **fields)
            self.assertNotEqual(result['status'], 'succeeded')
            self.assertEqual(self.store.data_file.read_bytes(), before)

    def test_schema_rejects_bad_arrays_and_frequency(self):
        for fields in [dict(frequency='hourly'), dict(frequency='weekly', weekdays=[True]), dict(frequency='weekly', weekdays=[7])]:
            with self.assertRaises(ChatError):
                validate_call('preview_create_habit', dict(title='x', **fields))

    def test_event_habit_intents_cannot_be_swapped(self):
        self.service.auto_execute = True
        for name, args, message in [('preview_create', dict(title='x', date='2026-11-23'), '新增习惯，每周一开会'),
                                    ('preview_create_habit', dict(title='x', frequency='weekly', weekdays=[0]), '创建普通日程，只在下周一开会一次')]:
            result = self.send([fixture.tool(name, args)], message)
            self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.store.events), 2)
        self.assertEqual(self.store.routines, [])

    def test_request_id_dedup_and_same_title_new_request_allowed(self):
        self.service.auto_execute = True
        message, args = '每天背单词', dict(title='背单词', frequency='daily')
        first = self.send([fixture.tool('preview_create_habit', args)], message, 'same-habit-request')
        repeat = self.send([], message, 'same-habit-request')
        self.assertEqual(first, repeat)
        self.assertEqual(len(self.store.routines), 1)
        self.send([fixture.tool('preview_create_habit', args)], message, 'different-habit-request')
        self.assertEqual(len(self.store.routines), 2)

    def test_context_is_typed_and_followup_cannot_modify_event(self):
        result = self.habit('每周一开组会', 'weekly', weekdays=[0])
        session = self.service.get_session(self.sid)
        self.assertEqual(session['selected_kind'], 'habit')
        self.assertEqual(session['selected'], result['operation_result']['habit_id'])
        result = self.send([], '改成每周一和周四')
        self.assertEqual(result['status'], 'clarification_required')
        self.assertIn('尚不支持', result['assistant_message'])

    def test_failed_save_rolls_back_and_history_remains(self):
        old = RoutineItem('old', '旧习惯', habit_done=['2026-11-01'])
        self.store.routines = [old]
        self.store.save()
        before = self.store.data_file.read_bytes()
        with patch.object(self.store, 'save', side_effect=OSError('disk')):
            result = self.habit('每天背单词')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.data_file.read_bytes(), before)
        self.assertEqual(self.store.routines, [old])

    def test_backup_contains_schedule_and_history(self):
        self.habit('每周一三五健身', 'weekly', weekdays=[0, 2, 4])
        self.store.routines[0].habit_done = ['2026-11-13']
        self.store.save()
        target = self.root/'habits.dcalbackup'
        create_backup(self.store.data_file.read_bytes(), target)
        payload = read_backup(target)
        self.assertEqual(payload[1], self.store.data_file.read_bytes())
        original = asdict(self.store.routines[0])
        self.store.delete_routine(original['id'])
        restore_store(self.store, target)
        self.assertEqual(asdict(self.store.routines[0]), original)

    def test_mixed_event_and_habit_batch_no_partial_execution(self):
        from test_calendar_chat_orchestration import batch
        result = self.send([batch(fixture.tool('preview_create_habit', dict(title='x', frequency='daily')),
                                  fixture.tool('preview_create', dict(title='y', date='2026-11-23')))], '每天背单词')
        self.assertNotEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.routines, [])
