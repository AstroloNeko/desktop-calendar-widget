from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_core import DATA_SCHEMA_VERSION, Event, RoutineItem, Store
from data_backup import BackupError, create_backup, read_backup, restore_store
from habit_schedule import habit_scheduled_on, schedule_label, validate_schedule_input
from timeline_model import build_month_timeline


def habit(**changes):
    defaults = dict(id="habit", title="周期习惯", created_on="2026-08-03", start_on="2026-08-03", workdays_only=False)
    defaults.update(changes)
    return RoutineItem(**defaults)


class HabitScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data.json"
        self.store = Store(self.path)

    def test_legacy_defaults_preserve_identity_history_and_workday_behavior(self):
        raw = {"id": "original", "title": "旧习惯", "created_on": "2026-08-03",
               "habit_done": ["2026-08-03", "2026-08-04"], "reminder_enabled": True, "reminder_time": "09:00"}
        item = RoutineItem.from_dict(raw)
        self.assertEqual(item.frequency, "daily")
        self.assertTrue(item.workdays_only)
        self.assertEqual(item.id, raw["id"])
        self.assertEqual(item.habit_done, raw["habit_done"])
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 5)))
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 8, 8)))

    def test_new_daily_can_include_weekends_and_holidays(self):
        item = habit(start_on="2026-01-01")
        for day in (date(2026, 8, 8), date(2026, 10, 1)):
            self.assertTrue(self.store.routine_planned_on(item, day))

    def test_daily_intervals_two_three_seven(self):
        for interval in (2, 3, 7):
            item = habit(interval=interval)
            for offset in range(30):
                day = item.created_date + timedelta(days=offset)
                self.assertEqual(habit_scheduled_on(item, day), offset % interval == 0)

    def test_missed_checkin_never_shifts_anchor(self):
        item = habit(interval=3)
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 6)))
        self.store.routines = [item]
        self.store.toggle_routine(item, date(2026, 8, 6))
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 9)))
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 8, 10)))

    def test_weekly_one_day(self):
        item = habit(frequency="weekly", weekdays=[6])
        self.assertTrue(habit_scheduled_on(item, date(2026, 8, 9)))
        self.assertFalse(habit_scheduled_on(item, date(2026, 8, 10)))

    def test_weekly_multiple_weekdays(self):
        item = habit(frequency="weekly", weekdays=[0, 2, 4])
        matched = [d for d in range(7) if habit_scheduled_on(item, date(2026, 8, 3) + timedelta(days=d))]
        self.assertEqual(matched, [0, 2, 4])

    def test_every_two_weeks_uses_monday_of_start_week(self):
        item = habit(frequency="weekly", interval=2, weekdays=[0, 2, 4], start_on="2026-08-05")
        expected = {5, 7, 17, 19, 21, 31}
        actual = {d for d in range(1, 32) if habit_scheduled_on(item, date(2026, 8, d))}
        self.assertEqual(actual, expected)

    def test_monthly_fixed_date(self):
        item = habit(frequency="monthly", month_day=15)
        self.assertTrue(habit_scheduled_on(item, date(2026, 8, 15)))
        self.assertTrue(habit_scheduled_on(item, date(2026, 9, 15)))
        self.assertFalse(habit_scheduled_on(item, date(2026, 9, 16)))

    def test_monthly_interval_across_year(self):
        item = habit(frequency="monthly", interval=3, start_on="2026-11-01", month_day=15)
        self.assertTrue(habit_scheduled_on(item, date(2027, 2, 15)))
        self.assertFalse(habit_scheduled_on(item, date(2027, 1, 15)))

    def test_month_ends_skip_without_clamping_or_drift(self):
        for day in (29, 30, 31):
            item = habit(frequency="monthly", start_on="2025-01-01", month_day=day)
            self.assertFalse(any(habit_scheduled_on(item, date(2025, 2, d)) for d in range(1, 29)))
            self.assertTrue(habit_scheduled_on(item, date(2025, 3, day)))
        item = habit(frequency="monthly", start_on="2024-01-01", month_day=29)
        self.assertTrue(habit_scheduled_on(item, date(2024, 2, 29)))

    def test_yearly_leap_day_skips_non_leap_year(self):
        item = habit(frequency="yearly", start_on="2024-02-29", month_day=29, year_month=2)
        self.assertFalse(habit_scheduled_on(item, date(2025, 2, 28)))
        self.assertFalse(habit_scheduled_on(item, date(2025, 3, 1)))
        self.assertTrue(habit_scheduled_on(item, date(2028, 2, 29)))

    def test_yearly_interval(self):
        item = habit(frequency="yearly", interval=2, start_on="2026-10-22", month_day=22, year_month=10)
        self.assertTrue(habit_scheduled_on(item, date(2028, 10, 22)))
        self.assertFalse(habit_scheduled_on(item, date(2027, 10, 22)))

    def test_start_and_end_inclusive(self):
        item = habit(start_on="2026-08-05", end_on="2026-08-07")
        self.assertFalse(habit_scheduled_on(item, date(2026, 8, 4)))
        self.assertTrue(habit_scheduled_on(item, date(2026, 8, 5)))
        self.assertTrue(habit_scheduled_on(item, date(2026, 8, 7)))
        self.assertFalse(habit_scheduled_on(item, date(2026, 8, 8)))

    def test_interval_anchor_can_precede_metadata_creation_date(self):
        item = habit(created_on="2026-10-22", start_on="2026-08-03", interval=3)
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 6)))

    def test_workday_option_respects_legal_adjustment_and_manual_leave(self):
        item = habit(start_on="2026-01-01", workdays_only=True)
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 10, 10)))
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 10, 1)))
        self.store.set_date_status(date(2026, 10, 10), "leave")
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 10, 10)))
        self.store.set_date_status(date(2026, 10, 10), "normal")
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 10, 10)))

    def test_skip_workday_is_filter_not_shift(self):
        item = habit(start_on="2026-08-07", interval=2, workdays_only=True)
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 8, 9)))
        self.assertFalse(self.store.routine_planned_on(item, date(2026, 8, 10)))
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 11)))

    def test_no_checkin_or_storage_write_on_non_plan_day(self):
        item = habit(frequency="weekly", weekdays=[0])
        self.store.routines = [item]
        with patch.object(self.store, "save") as save:
            self.store.toggle_routine(item, date(2026, 8, 4))
        self.assertEqual(item.habit_done, [])
        save.assert_not_called()

    def test_completion_per_date_and_non_plan_denominator(self):
        item = habit(frequency="weekly", weekdays=[0, 2, 4])
        self.store.routines = [item]
        self.store.toggle_routine(item, date(2026, 8, 3))
        self.store.toggle_routine(item, date(2026, 8, 5))
        self.assertTrue(item.is_done_on(date(2026, 8, 3)))
        self.assertFalse(item.is_done_on(date(2026, 8, 7)))
        self.assertEqual(self.store.routine_progress(item, date(2026, 8, 3), date(2026, 8, 9)), (3, 2))

    def test_period_edit_preserves_history_even_when_not_planned_now(self):
        item = habit(habit_done=["2026-08-03", "2026-08-04"])
        self.store.routines = [item]
        edited = replace(item, frequency="weekly", weekdays=[2])
        self.store.upsert_routine(edited)
        self.assertEqual(edited.habit_done, item.habit_done)
        self.assertTrue(edited.is_done_on(date(2026, 8, 4)))
        self.assertEqual(self.store.agenda_items_on(date(2026, 8, 4)), [])
        self.assertEqual(self.store.routine_progress(edited, date(2026, 8, 3), date(2026, 8, 9)), (1, 0))

    def test_completion_still_sinks_and_cancel_restores(self):
        one, two = habit(id="1", title="A"), habit(id="2", title="B")
        self.store.routines = [one, two]
        day = date(2026, 8, 3)
        self.store.toggle_routine(one, day)
        self.assertEqual([i.id for i in self.store.agenda_items_on(day)], ["2", "1"])
        self.store.toggle_routine(one, day)
        self.assertEqual([i.id for i in self.store.agenda_items_on(day)], ["1", "2"])

    def test_reminders_only_planned_dates_including_selected_weekends(self):
        item = habit(frequency="weekly", weekdays=[6], reminder_enabled=True, reminder_time="09:00")
        self.store.routines = [item]
        self.assertEqual(self.store.due_routine_reminders(datetime(2026, 8, 5, 10)), [])
        self.assertEqual(self.store.due_routine_reminders(datetime(2026, 8, 9, 10)), [item])
        self.store.toggle_routine(item, date(2026, 8, 9))
        self.assertEqual(self.store.due_routine_reminders(datetime(2026, 8, 9, 10)), [])

    def test_schedule_edit_does_not_repeat_already_notified_same_date(self):
        item = habit(reminder_enabled=True, reminder_time="09:00")
        self.store.routines = [item]
        self.store.notified.add(self.store.routine_notification_key(item, date(2026, 8, 3)))
        self.store.upsert_routine(replace(item, frequency="weekly", weekdays=[0, 2]))
        self.assertEqual(self.store.due_routine_reminders(datetime(2026, 8, 3, 10)), [])

    def test_disable_and_end_stop_reminders(self):
        item = habit(end_on="2026-08-05", reminder_enabled=True, reminder_time="09:00")
        self.store.routines = [item]
        self.assertEqual(self.store.due_routine_reminders(datetime(2026, 8, 6, 10)), [])
        item.enabled = False
        self.assertEqual(self.store.routines_on(date(2026, 8, 4)), [])

    def test_save_reload_backup_restore_rules_and_history(self):
        item = habit(frequency="monthly", interval=2, month_day=31, end_on="2028-01-31", habit_done=["2026-08-31"])
        self.store.routines = [item]
        self.store.save()
        self.assertEqual(asdict(Store(self.path).routines[0]), asdict(item))
        backup = self.path.with_suffix(".dcalbackup")
        create_backup(self.path.read_bytes(), backup)
        self.assertEqual(read_backup(backup)[0]["schema_version"], DATA_SCHEMA_VERSION)
        self.store.routines.clear()
        self.store.save()
        restore_store(self.store, backup)
        self.assertEqual(asdict(self.store.routines[0]), asdict(item))

    def test_legacy_schema_backup_restores_daily_and_history(self):
        raw = {"version": 7, "routines": [{"id": "old", "title": "旧习惯", "created_on": "2026-08-03", "habit_done": ["2026-08-04"]}]}
        backup = self.path.with_suffix(".dcalbackup")
        create_backup(json.dumps(raw).encode(), backup)
        restore_store(self.store, backup)
        self.assertTrue(self.store.routines[0].workdays_only)
        self.assertEqual(self.store.routines[0].habit_done, ["2026-08-04"])

    def test_old_backup_reader_rejects_new_schema_instead_of_dropping_rules(self):
        self.store.routines = [habit(frequency="yearly")]
        self.store.save()
        backup = self.path.with_suffix(".dcalbackup")
        create_backup(self.path.read_bytes(), backup)
        with patch("data_backup.SCHEMA_VERSION", 7), self.assertRaises(BackupError):
            read_backup(backup)

    def test_rules_do_not_create_events_or_timeline_rows(self):
        self.store.routines = [habit(frequency="weekly", weekdays=[0, 2, 4])]
        self.store.events = [Event("event", "普通事项", "2026-08-03T12:00")]
        for day in range(1, 32):
            self.store.routines_on(date(2026, 8, day))
        model = build_month_timeline(self.store, 2026, 8)
        self.assertEqual([item.id for item in model.items], ["event"])
        self.assertEqual(len(self.store.events), 1)

    def test_unknown_or_partial_fields_fall_back_without_history_loss(self):
        item = RoutineItem.from_dict(dict(id="safe", title="safe", created_on="2026-08-03", frequency="bad", interval=-3,
            weekdays=[-1, 8, "x"], month_day=100, year_month=99, start_on="invalid", end_on="invalid", habit_done=["2026-08-04"]))
        self.assertEqual((item.frequency, item.interval, item.month_day, item.year_month), ("daily", 1, 3, 8))
        self.assertEqual(item.habit_done, ["2026-08-04"])

    def test_editor_validation(self):
        fields = dict(frequency="daily", interval="1", weekdays=[0], month_day="3", year_month="8", start_on="2026-08-03", end_on=None, workdays_only=False)
        self.assertEqual(validate_schedule_input(**fields)["interval"], 1)
        for changes in ({"interval": "0"}, {"interval": "abc"}, {"frequency": "weekly", "weekdays": []},
                        {"end_on": "2026-08-01"}, {"frequency": "yearly", "year_month": "2", "month_day": "30"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_schedule_input(**dict(fields, **changes))

    def test_todo_does_not_become_repeating_habit(self):
        item = habit(kind="todo", frequency="weekly", weekdays=[6])
        self.store.routines = [item]
        self.assertTrue(self.store.routine_planned_on(item, date(2026, 8, 4)))
        self.store.toggle_routine(item, date(2026, 8, 4))
        self.assertEqual(self.store.routines_on(date(2026, 8, 5)), [])
        self.assertEqual(item.habit_done, [])

    def test_summary_names_and_workday_filter(self):
        self.assertIn("周一、三、五", schedule_label(habit(frequency="weekly", weekdays=[0, 2, 4])))
        self.assertIn("仅工作日", schedule_label(habit(workdays_only=True)))


if __name__ == "__main__":
    unittest.main()
