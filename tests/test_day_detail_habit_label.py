from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import DayDetailDialog
from calendar_core import RoutineItem
from habit_schedule import schedule_label


class DayDetailHabitLabelTests(unittest.TestCase):
    def test_subtitle_shows_each_habits_schedule(self):
        for item in (RoutineItem("daily", "每天", workdays_only=False),
                     RoutineItem("weekly", "每周", frequency="weekly", weekdays=[1, 3], workdays_only=False),
                     RoutineItem("interval", "间隔", interval=3, workdays_only=False)):
            dialog = SimpleNamespace(day=date(2026, 11, 25), _base_row=Mock(return_value=(Mock(), Mock(), Mock())))
            with patch("app.tk.Label") as label:
                DayDetailDialog._build_routine_row(dialog, item)
            self.assertEqual(label.call_args_list[1].kwargs["text"], "习惯 · " + schedule_label(item))
