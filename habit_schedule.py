"""Calendar matching for existing RoutineItem habits, not event occurrences."""
from __future__ import annotations

import calendar
from datetime import date

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
SCHEDULE_FIELDS = ("frequency", "interval", "weekdays", "month_day", "year_month", "start_on", "end_on", "workdays_only")


def _date_or(value, default):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return default


def normalize_schedule(item) -> None:
    """Safe legacy/partial-data fallback, without touching completion history."""
    item.frequency = item.frequency if item.frequency in FREQUENCIES else "daily"
    item.interval = item.interval if type(item.interval) is int and 1 <= item.interval <= 999 else 1
    item.start_on = _date_or(item.start_on, item.created_on)
    item.end_on = _date_or(item.end_on, None)
    if item.end_on and item.end_on < item.start_on:
        item.end_on = None
    anchor = date.fromisoformat(item.start_on)
    days = item.weekdays if isinstance(item.weekdays, (list, tuple)) else []
    item.weekdays = sorted({day for day in days if type(day) is int and 0 <= day <= 6}) or [anchor.weekday()]
    item.month_day = item.month_day if type(item.month_day) is int and 1 <= item.month_day <= 31 else anchor.day
    item.year_month = item.year_month if type(item.year_month) is int and 1 <= item.year_month <= 12 else anchor.month
    item.workdays_only = item.workdays_only is True


def habit_scheduled_on(item, day: date) -> bool:
    """Missing month-days are skipped, never clamped or shifted.

    Weekly intervals use the Monday-containing start week. A missed completion
    never changes the start anchor, interval, or future planned dates.
    Workday/leave rules are applied by Store using its single calendar service.
    """
    start = date.fromisoformat(item.start_on)
    if day < start or (item.end_on and day > date.fromisoformat(item.end_on)):
        return False
    elapsed = (day - start).days
    if item.frequency == "daily":
        return elapsed % item.interval == 0
    if item.frequency == "weekly":
        return ((elapsed + start.weekday()) // 7) % item.interval == 0 and day.weekday() in item.weekdays
    if item.frequency == "monthly":
        months = (day.year - start.year) * 12 + day.month - start.month
        return months % item.interval == 0 and day.day == item.month_day
    return ((day.year - start.year) % item.interval == 0
            and day.month == item.year_month and day.day == item.month_day)


def schedule_label(item) -> str:
    if item.kind != "habit":
        return "待办 · 完成一次即结束"
    n = item.interval
    if item.frequency == "daily":
        text = "每天" if n == 1 else f"每隔 {n} 天"
    elif item.frequency == "weekly":
        weekdays = "、".join("一二三四五六日"[d] for d in item.weekdays)
        text = ("每周" if n == 1 else f"每隔 {n} 周") + " · 周" + weekdays
    elif item.frequency == "monthly":
        text = ("每月" if n == 1 else f"每隔 {n} 个月") + f" {item.month_day} 日"
    else:
        text = ("每年" if n == 1 else f"每隔 {n} 年") + f" {item.year_month} 月 {item.month_day} 日"
    if item.workdays_only:
        text += " · 仅工作日"
    if item.end_on:
        text += f" · 至 {item.end_on}"
    return text


def validate_schedule_input(*, frequency, interval, weekdays, month_day, year_month, start_on, end_on, workdays_only):
    """Editor validation is strict; storage loading remains deliberately tolerant."""
    if frequency not in FREQUENCIES:
        raise ValueError("请选择有效的重复周期。")
    try:
        step = int(interval)
        day = int(month_day)
        month = int(year_month)
        start = date.fromisoformat(start_on)
        end = date.fromisoformat(end_on) if end_on else None
    except (TypeError, ValueError):
        raise ValueError("日期请使用 YYYY-MM-DD，间隔和月日请填写整数。") from None
    if not 1 <= step <= 999 or not 1 <= day <= 31 or not 1 <= month <= 12:
        raise ValueError("间隔须为 1–999，月份为 1–12，日期为 1–31。")
    if end and end < start:
        raise ValueError("结束日期不能早于开始日期。")
    if frequency == "weekly" and (not weekdays or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays)):
        raise ValueError("请至少选择一个星期。")
    if frequency == "yearly" and day > calendar.monthrange(2000, month)[1]:
        raise ValueError("这个月日不存在；2 月最多可选 29 日。")
    return dict(frequency=frequency, interval=step, weekdays=sorted(set(weekdays)), month_day=day,
                year_month=month, start_on=start.isoformat(), end_on=end.isoformat() if end else None,
                workdays_only=bool(workdays_only))
