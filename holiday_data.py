from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


@dataclass(frozen=True)
class HolidayInfo:
    name: str
    short_name: str
    kind: str = "festival"  # festival | day_off | workday


# 国务院办公厅公布的 2026 年节假日安排。年度调休并不存在永久规则，
# 因此单独按年份维护，后续版本可在不动界面代码的情况下更新。
_DAYS_OFF_2026 = (
    (date(2026, 1, 1), date(2026, 1, 3), "元旦", date(2026, 1, 1)),
    (date(2026, 2, 15), date(2026, 2, 23), "春节", date(2026, 2, 17)),
    (date(2026, 4, 4), date(2026, 4, 6), "清明节", date(2026, 4, 5)),
    (date(2026, 5, 1), date(2026, 5, 5), "劳动节", date(2026, 5, 1)),
    (date(2026, 6, 19), date(2026, 6, 21), "端午节", date(2026, 6, 19)),
    (date(2026, 9, 25), date(2026, 9, 27), "中秋节", date(2026, 9, 25)),
    (date(2026, 10, 1), date(2026, 10, 7), "国庆节", date(2026, 10, 1)),
)

_WORKDAYS_2026 = {
    date(2026, 1, 4),
    date(2026, 2, 14),
    date(2026, 2, 28),
    date(2026, 5, 9),
    date(2026, 9, 20),
    date(2026, 10, 10),
}

_FIXED_FESTIVALS = {
    (1, 1): ("元旦", "元旦"),
    (2, 14): ("情人节", "情人节"),
    (3, 8): ("妇女节", "妇女节"),
    (3, 12): ("植树节", "植树节"),
    (4, 1): ("愚人节", "愚人节"),
    (5, 1): ("劳动节", "劳动节"),
    (5, 4): ("青年节", "青年节"),
    (6, 1): ("儿童节", "儿童节"),
    (7, 1): ("建党节", "建党节"),
    (8, 1): ("建军节", "建军节"),
    (9, 10): ("教师节", "教师节"),
    (10, 1): ("国庆节", "国庆节"),
    (12, 24): ("平安夜", "平安夜"),
    (12, 25): ("圣诞节", "圣诞节"),
}

# 常用农历节日换算后的公历日期。覆盖近期版本的主要使用年份；
# 法定放假日仍以上方国务院年度安排为准。
_LUNAR_FESTIVALS = {
    2024: ((2, 9, "除夕", "除夕"), (2, 10, "春节", "春节"), (2, 24, "元宵节", "元宵节"), (6, 10, "端午节", "端午节"), (8, 10, "七夕", "七夕"), (9, 17, "中秋节", "中秋节"), (10, 11, "重阳节", "重阳节")),
    2025: ((1, 28, "除夕", "除夕"), (1, 29, "春节", "春节"), (2, 12, "元宵节", "元宵节"), (5, 31, "端午节", "端午节"), (8, 29, "七夕", "七夕"), (10, 6, "中秋节", "中秋节"), (10, 29, "重阳节", "重阳节")),
    2026: ((2, 16, "除夕", "除夕"), (2, 17, "春节", "春节"), (3, 3, "元宵节", "元宵节"), (6, 19, "端午节", "端午节"), (8, 19, "七夕", "七夕"), (9, 25, "中秋节", "中秋节"), (10, 18, "重阳节", "重阳节")),
    2027: ((2, 5, "除夕", "除夕"), (2, 6, "春节", "春节"), (2, 20, "元宵节", "元宵节"), (6, 9, "端午节", "端午节"), (8, 8, "七夕", "七夕"), (9, 15, "中秋节", "中秋节"), (10, 8, "重阳节", "重阳节")),
    2028: ((1, 25, "除夕", "除夕"), (1, 26, "春节", "春节"), (2, 9, "元宵节", "元宵节"), (5, 28, "端午节", "端午节"), (8, 26, "七夕", "七夕"), (10, 3, "中秋节", "中秋节"), (10, 26, "重阳节", "重阳节")),
    2029: ((2, 12, "除夕", "除夕"), (2, 13, "春节", "春节"), (2, 27, "元宵节", "元宵节"), (6, 16, "端午节", "端午节"), (8, 16, "七夕", "七夕"), (9, 22, "中秋节", "中秋节"), (10, 15, "重阳节", "重阳节")),
    2030: ((2, 2, "除夕", "除夕"), (2, 3, "春节", "春节"), (2, 17, "元宵节", "元宵节"), (6, 5, "端午节", "端午节"), (8, 5, "七夕", "七夕"), (9, 12, "中秋节", "中秋节"), (10, 5, "重阳节", "重阳节")),
}

_QINGMING = {
    2024: date(2024, 4, 4),
    2025: date(2025, 4, 4),
    2026: date(2026, 4, 5),
    2027: date(2027, 4, 5),
    2028: date(2028, 4, 4),
    2029: date(2029, 4, 4),
    2030: date(2030, 4, 5),
}


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _official_2026(day: date) -> Optional[HolidayInfo]:
    if day in _WORKDAYS_2026:
        return HolidayInfo("调休上班", "班", "workday")
    for start, end, name, festival_day in _DAYS_OFF_2026:
        if start <= day <= end:
            display_name = name if day == festival_day else f"{name}假期"
            display_text = name if day == festival_day else "休"
            return HolidayInfo(display_name, display_text, "day_off")
    return None


def _common_festival(day: date) -> Optional[HolidayInfo]:
    fixed = _FIXED_FESTIVALS.get((day.month, day.day))
    if fixed:
        return HolidayInfo(*fixed)

    moving = {
        _nth_weekday(day.year, 5, calendar.SUNDAY, 2): ("母亲节", "母亲节"),
        _nth_weekday(day.year, 6, calendar.SUNDAY, 3): ("父亲节", "父亲节"),
        _nth_weekday(day.year, 11, calendar.THURSDAY, 4): ("感恩节", "感恩节"),
    }
    if day in moving:
        return HolidayInfo(*moving[day])

    if _QINGMING.get(day.year) == day:
        return HolidayInfo("清明节", "清明节")

    for month, month_day, name, short in _LUNAR_FESTIVALS.get(day.year, ()):
        if (day.month, day.day) == (month, month_day):
            return HolidayInfo(name, short)
    return None


def holiday_for(day: date) -> Optional[HolidayInfo]:
    common = _common_festival(day)
    official = _official_2026(day) if day.year == 2026 else None
    if official:
        if official.kind == "day_off" and common:
            return HolidayInfo(common.name, common.short_name, "day_off")
        return official
    return common


def is_workday(day: date) -> bool:
    """Use the official schedule when known and weekdays everywhere else."""
    info = holiday_for(day)
    if info and info.kind == "workday":
        return True
    if info and info.kind == "day_off":
        return False
    return day.weekday() < 5


def official_schedule_years() -> tuple[int, ...]:
    return (2026,)
