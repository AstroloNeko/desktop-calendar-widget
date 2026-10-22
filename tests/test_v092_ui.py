import unittest

from bulk_ui import shift_summary
from habit_schedule import validate_schedule_input
from habit_ui import rule_summary


class UIWordingTests(unittest.TestCase):
    def test_summary_uses_validated_saved_rule(self):
        for frequency, interval, expected in (
            ("daily", 1, "每天"), ("daily", 3, "每隔 3 天"),
            ("weekly", 2, "每隔 2 周"), ("monthly", 1, "每月 15 日"),
            ("yearly", 1, "每年 10 月 15 日"),
        ):
            rule = validate_schedule_input(frequency=frequency, interval=interval, weekdays=[0, 2, 4],
                month_day=15, year_month=10, start_on="2026-10-15", end_on="2027-10-15", workdays_only=True)
            text = rule_summary(rule)
            self.assertIn(expected, text)
            self.assertIn("仅工作日", text)
            self.assertIn("2027-10-15", text)
            self.assertNotIn("frequency=", text)

    def test_shift_summary_does_not_change_signed_input(self):
        self.assertEqual(shift_summary("-3"), "提前 3 天")
        self.assertEqual(shift_summary("7"), "延后 7 天")
        self.assertEqual(shift_summary("0"), "日期保持不变")
        self.assertIn("整数", shift_summary("invalid"))
