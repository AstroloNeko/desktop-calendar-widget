import unittest
from datetime import date

from holiday_data import holiday_for, is_workday, official_schedule_years


class HolidayDataTests(unittest.TestCase):
    def test_2026_official_days_off(self) -> None:
        self.assertEqual(holiday_for(date(2026, 2, 17)).name, "春节")
        self.assertEqual(holiday_for(date(2026, 2, 16)).name, "除夕")
        self.assertEqual(holiday_for(date(2026, 2, 18)).short_name, "休")
        self.assertEqual(holiday_for(date(2026, 9, 25)).name, "中秋节")

    def test_2026_adjusted_workdays(self) -> None:
        info = holiday_for(date(2026, 10, 10))
        self.assertEqual(info.kind, "workday")
        self.assertEqual(info.short_name, "班")

    def test_recurring_and_moving_festivals(self) -> None:
        self.assertEqual(holiday_for(date(2028, 9, 10)).name, "教师节")
        self.assertEqual(holiday_for(date(2028, 4, 4)).name, "清明节")
        self.assertEqual(holiday_for(date(2026, 5, 10)).name, "母亲节")
        self.assertEqual(holiday_for(date(2026, 6, 21)).kind, "day_off")

    def test_schedule_year_metadata(self) -> None:
        self.assertEqual(official_schedule_years(), (2026,))

    def test_workday_rule_respects_weekends_holidays_and_makeup_days(self) -> None:
        self.assertTrue(is_workday(date(2026, 8, 3)))
        self.assertFalse(is_workday(date(2026, 8, 2)))
        self.assertFalse(is_workday(date(2026, 10, 2)))
        self.assertTrue(is_workday(date(2026, 10, 10)))


if __name__ == "__main__":
    unittest.main()
