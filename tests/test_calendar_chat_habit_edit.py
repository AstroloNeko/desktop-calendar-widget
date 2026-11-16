"""Habit edits: scoped patches, temporal stats, unified undo and compatibility."""
from dataclasses import asdict, replace
from datetime import date, timedelta
import json
from unittest.mock import patch, Mock
import unittest
import test_calendar_chat as fixture
from calendar_core import RoutineItem, Store, COLORS
from data_compat import decode_calendar_data, DataVersionTooNew
from data_backup import create_backup, restore_store, read_backup


class HabitEditingTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    send = fixture.ChatTests.send

    def seed(self, identifier='habit-one', title='背单词'):
        item = RoutineItem(identifier, title, color=COLORS['海盐蓝'], created_on='2026-01-01', start_on='2026-01-01', workdays_only=False,
                           habit_done=['2026-01-02', '2026-01-03'])
        self.store.upsert_routine(item)
        self.service.auto_execute = True
        self.gateway.habit_refresh = Mock()
        return item

    def select_habit(self, identifier='habit-one'):
        session = self.service.get_session(self.sid)
        session.update(selected=identifier, selected_kind='habit')
        self.journal.put('session', self.sid, 'local-owner', session)

    def edit(self, changes, message, identifier='habit-one', prefix=()):
        return self.send([*prefix, fixture.tool('preview_update_habit', {'habit_id': identifier, 'changes': changes})], message)

    def test_search_detail_then_title_only_patch(self):
        self.seed()
        before = asdict(self.store.routines[0])
        result = self.edit({'title': '英语学习', 'color': '#ffffff', 'frequency': 'monthly'}, '把背单词改成英语学习',
            prefix=[fixture.tool('query_habits', {'keyword': '背单词'}), fixture.tool('get_habit', {'habit_id': 'habit-one'})])
        self.assertEqual(result['status'], 'succeeded')
        after = asdict(self.store.routines[0])
        self.assertEqual([k for k in before if before[k] != after[k]], ['title'])
        self.gateway.habit_refresh.assert_called_once()

    def test_color_only_patch_and_rename_word_is_not_field(self):
        self.seed(); self.select_habit()
        before = asdict(self.store.routines[0])
        result = self.edit({'color': '#ffffff', 'title': '坏标题', 'start_on': '2099-01-01', 'frequency': 'weekly'}, '把组会习惯改成绿色')
        self.assertEqual(result['status'], 'succeeded')
        after = asdict(self.store.routines[0])
        self.assertEqual([k for k in before if before[k] != after[k]], ['color'])
        self.assertEqual(after['color'], COLORS['薄荷绿'])
        self.assertEqual(self.edit({'title': '提醒我每周一'}, '改名为提醒我每周一')['status'], 'succeeded')
        original_start = self.store.routines[0].start_on
        for name in ('今天开始', '下周开始', '今年年底'):
            self.assertEqual(self.edit({'title': name}, '改名为'+name)['status'], 'succeeded')
            self.assertEqual(self.store.routines[0].start_on, original_start)
            self.assertIsNone(self.store.routines[0].end_on)

    def test_weekly_patch_drops_unrelated_yearly_fields_and_off_keeps_clock(self):
        self.seed(); self.select_habit()
        original = self.store.routines[0]
        self.edit({'frequency': 'weekly', 'weekdays': [0], 'month_day': 31, 'year_month': 12}, '改成每周一')
        self.assertEqual(self.store.routines[0].month_day, original.month_day)
        self.assertEqual(self.store.routines[0].year_month, original.year_month)
        self.edit({'reminder_enabled': True, 'reminder_time': '09:00'}, '早上九点提醒')
        self.edit({'reminder_enabled': False, 'reminder_time': '18:00'}, '提醒关掉')
        self.assertEqual(self.store.routines[0].reminder_time, '09:00')

    def test_multi_turn_all_recurrence_units_same_identity(self):
        self.seed(); self.select_habit()
        for phrase, fields in [('改成每周二四六', dict(frequency='weekly', weekdays=[0])),
                ('改成每隔3天', dict(frequency='daily', interval=3)),
                ('改成每隔2周', dict(frequency='weekly', interval=2, weekdays=[1, 3, 5])),
                ('改成每隔3个月', dict(frequency='monthly', interval=3, month_day=15)),
                ('以后每隔2年进行一次', dict(frequency='yearly', interval=2, year_month=11, month_day=23))]:
            result = self.edit(fields, phrase)
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['operation_result']['habit_id'], 'habit-one')
            self.assertEqual(self.store.routines[0].habit_done, ['2026-01-02', '2026-01-03'])
        self.assertEqual(len(self.store.routines), 1)
        self.assertEqual(self.commands.undo_count, 5)

    def test_period_cutover_preserves_past_plan_and_statistics(self):
        self.seed(); self.select_habit()
        before = self.store.routine_progress(self.store.routines[0], date(2026, 1, 1), date(2026, 1, 10))
        self.assertEqual(self.edit(dict(frequency='weekly', weekdays=[0, 2, 4]), '以后改成每周一三五')['status'], 'succeeded')
        item = self.store.routines[0]
        self.assertEqual(self.store.routine_progress(item, date(2026, 1, 1), date(2026, 1, 10)), before)
        self.assertEqual(len(item.schedule_history), 1)
        self.assertEqual(item.schedule_history[0]['until'], date.today().isoformat())
        for n in range(1, 8):
            day = date.today()+timedelta(days=n)
            self.assertEqual(self.store.routine_planned_on(item, day), day.weekday() in [0, 2, 4])

    def test_same_day_multiple_edits_retain_original_history(self):
        self.seed(); self.select_habit()
        self.edit(dict(frequency='weekly', weekdays=[0]), '改成每周一')
        self.edit(dict(frequency='weekly', weekdays=[1, 3]), '改成每周二四')
        self.assertEqual(len(self.store.routines[0].schedule_history), 1)
        self.assertEqual(self.store.routines[0].schedule_history[0]['rule']['frequency'], 'daily')

    def test_start_end_workdays_and_reminder_patches(self):
        self.seed(); self.select_habit()
        for text, changes in [('这个习惯从下个月一号开始', {'start_on': '2099-01-01'}),
                ('这个习惯持续到今年年底', {'end_on': '2099-12-31'}),
                ('改成永不结束', {'end_on': None}),
                ('以后只在工作日做', {'workdays_only': True}),
                ('把背单词提醒改成早上九点', {'reminder_time': '09:00'}),
                ('提醒关掉', {'reminder_enabled': False})]:
            self.assertEqual(self.edit(changes, text)['status'], 'succeeded')
        item = self.store.routines[0]
        self.assertTrue(item.workdays_only)
        self.assertFalse(item.reminder_enabled)
        self.assertIsNone(item.end_on)
        self.assertEqual(item.reminder_time, '09:00')
        self.assertEqual(item.start_on, date(date.today().year+(date.today().month == 12), date.today().month % 12+1, 1).isoformat())

    def test_multiple_same_name_clarifies_then_number_selection(self):
        self.seed(); self.seed('habit-two')
        before = self.store.data_file.read_bytes()
        result = self.edit({'color': '#ffffff'}, '把背单词改成绿色', prefix=[fixture.tool('query_habits', {'keyword': '背单词'})])
        self.assertEqual(result['status'], 'clarification_required')
        self.assertEqual(len(result['habit_candidates']), 2)
        self.assertEqual(before, self.store.data_file.read_bytes())
        result = self.send([], '2')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.routines[1].color, COLORS['薄荷绿'])

    def test_nonexistent_and_wrong_object_id_safe(self):
        self.seed(); self.select_habit()
        self.store.delete_routine('habit-one')
        self.assertNotEqual(self.edit({'color': '#ffffff'}, '颜色换成绿色')['status'], 'succeeded')
        self.assertEqual(len(self.store.events), 2)

    def test_undo_redo_same_stack_preserves_new_checkins(self):
        self.seed(); self.select_habit()
        self.edit({'frequency': 'weekly', 'weekdays': [0]}, '改成每周一')
        item = self.store.routines[0]
        item.habit_done.append(date.today().isoformat())
        self.store.save()
        self.commands.undo()
        self.assertEqual(self.store.routines[0].frequency, 'daily')
        self.assertIn(date.today().isoformat(), self.store.routines[0].habit_done)
        self.commands.redo()
        self.assertEqual(self.store.routines[0].frequency, 'weekly')
        self.assertIn(date.today().isoformat(), self.store.routines[0].habit_done)

    def test_failure_rollback_and_request_idempotency(self):
        self.seed(); self.select_habit()
        before = self.store.data_file.read_bytes()
        with patch.object(self.store, 'save', side_effect=OSError('disk')):
            self.assertEqual(self.edit({'color': '#ffffff'}, '改成绿色')['status'], 'failed')
        self.assertEqual(before, self.store.data_file.read_bytes())
        self.assertEqual(self.commands.undo_count, 0)
        replies = [fixture.tool('preview_update_habit', {'habit_id': 'habit-one', 'changes': {'color': '#ffffff'}})]
        first = self.send(replies, '改成绿色', 'habit-update-same-request')
        self.assertEqual(first, self.send([], '改成绿色', 'habit-update-same-request'))
        self.assertEqual(self.commands.undo_count, 1)

    def test_backup_restart_and_older_reader_rejection(self):
        self.seed(); self.select_habit()
        self.edit({'frequency': 'weekly', 'weekdays': [0]}, '改成每周一')
        original = asdict(self.store.routines[0])
        archive = self.root/'edited.dcalbackup'
        create_backup(self.store.data_file.read_bytes(), archive)
        self.store.delete_routine('habit-one')
        restore_store(self.store, archive)
        self.assertEqual(asdict(Store(self.store.data_file).routines[0]), original)
        data = self.store.data_file.read_bytes()
        with self.assertRaises(DataVersionTooNew):
            decode_calendar_data(data, supported=8)
        self.assertEqual(data, self.store.data_file.read_bytes())

    def test_query_readonly_and_sessions_separate(self):
        self.seed()
        result = self.send([fixture.tool('query_habits', {'keyword': '背单词'})], '查询背单词习惯')
        self.assertEqual(len(result['habit_candidates']), 1)
        new_sid = self.service.new_session()['session_id']
        self.assertIsNone(self.service.get_session(new_sid)['selected'])
        self.assertEqual(self.commands.undo_count, 0)

    def test_read_only_response_to_explicit_edit_is_replanned(self):
        self.seed(); self.select_habit()
        result = self.send([fixture.tool('get_habit', {'habit_id': 'habit-one'}), {'content': '已了解'},
            fixture.tool('preview_update_habit', {'habit_id': 'habit-one', 'changes': {'color': '#ffffff'}})], '颜色换成绿色')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.routines[0].color, COLORS['薄荷绿'])

    def test_concurrent_retries_write_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.seed(); self.select_habit()
        self.provider.replies = [fixture.tool('preview_update_habit', {'habit_id': 'habit-one', 'changes': {'color': '#ffffff'}})]
        def run(_):
            return self.service.send_message(self.sid, '改成绿色', 'concurrent-habit-edit')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['status'], 'succeeded')
        self.assertEqual(self.commands.undo_count, 1)

    def test_unconfirmed_patch_checks_conflict(self):
        self.seed(); self.select_habit()
        result = self.edit({'color': '#ffffff'}, '改成绿色，先预览')
        op = result['pending_operation']
        self.store.upsert_routine(replace(self.store.routines[0], title='已在界面改名'))
        before = self.store.data_file.read_bytes()
        result = self.service.confirm_operation(self.sid, op['operation_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'operation_conflict')
        self.assertEqual(before, self.store.data_file.read_bytes())

    def test_schema8_migration_checkpoint_and_invalid_history_fail_closed(self):
        self.seed()
        raw = json.loads(self.store.data_file.read_bytes())
        raw['version'] = 8
        for item in raw['routines']:
            item.pop('schedule_history', None)
        original = json.dumps(raw).encode()
        self.store.data_file.write_bytes(original)
        loaded = Store(self.store.data_file)
        self.assertIsNone(loaded.load_error)
        loaded.save()
        self.assertEqual(json.loads(loaded.data_file.read_bytes())['version'], 9)
        self.assertTrue(any(read_backup(p)[1] == original for p in (self.root/'backups').glob('*.dcalbackup')))
        raw['version'] = 9
        raw['routines'][0]['schedule_history'] = [{'until': '2026-11-14', 'rule': {}}]
        corrupted = json.dumps(raw).encode()
        loaded.data_file.write_bytes(corrupted)
        rejected = Store(loaded.data_file)
        self.assertIsNotNone(rejected.load_error)
        with self.assertRaises(ValueError):
            rejected.save()
        self.assertEqual(loaded.data_file.read_bytes(), corrupted)
