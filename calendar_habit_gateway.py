"""Detached Habit 2.0 creation; never writes events or implements recurrence."""
from dataclasses import asdict
from datetime import date, timedelta
import re

from calendar_core import RoutineItem, COLORS, normalize_reminder_time
from habit_schedule import validate_schedule_input, schedule_label
from calendar_chat_tools import fail, iso_day
from calendar_action_gateway import fingerprint


def habit_intent(message):
    return bool(re.search(r'习惯|每天|每日|每周|每(?:个)?月|每年|每隔|每星期|每个工作日', message))


def prepare_habit(store, values, identifier, message):
    today = date.today()
    cadence = re.search(r'每(?:隔[一二三四五六七八九十\d]+)?(?:个)?(天|日|周|星期|月|年)', message)
    if cadence:
        expected = {'天': 'daily', '日': 'daily', '周': 'weekly', '星期': 'weekly', '月': 'monthly', '年': 'yearly'}[cadence[1]]
        if values['frequency'] != expected:
            fail('周期与用户明确描述不一致，请重新解析习惯周期。', 'invalid_arguments')
    interval = values.get('interval', 1) if '每隔' in message else 1
    explicit_interval = re.search(r'每隔(\d+)', message)
    if explicit_interval:
        interval = int(explicit_interval[1])
    anchor = iso_day(values.get('start_on', today.isoformat())) if re.search(r'开始|起始|起执行', message) else today
    # Validate explicit relative start dates locally, not an LLM weekday label.
    if re.search(r'明天开始', message):
        anchor = today+timedelta(days=1)
    elif re.search(r'今天开始', message):
        anchor = today
    elif re.search(r'下周开始', message):
        anchor = today+timedelta(days=7-today.weekday())
    elif '下个月开始' in message:
        anchor = date(today.year+(today.month == 12), today.month % 12+1, 1)
    if re.search(r'最后一天|月底|[一二三四五六七八九十\d]+次后结束', message):
        fail('现有习惯支持固定月日及结束日期，暂不支持月末动态日期或次数结束。', 'clarification_required')
    weekdays = values.get('weekdays', [])
    # Explicit weekday text is checked against Python's Monday=0 convention.
    matched = re.findall(r'(?:每周|星期|周)([一二三四五六日天](?:[、和及,， ]*[一二三四五六日天])*)', message)
    if values['frequency'] == 'weekly' and matched:
        weekdays = sorted({'一二三四五六日'.index(char) if char != '天' else 6
                           for group in matched for char in group if char in '一二三四五六日天'})
    try:
        schedule = validate_schedule_input(frequency=values['frequency'], interval=interval,
            weekdays=weekdays, month_day=values.get('month_day', anchor.day), year_month=values.get('year_month', anchor.month),
            start_on=anchor.isoformat(), end_on=values.get('end_on') if re.search(r'结束|直到|截至|截止', message) else None,
            workdays_only=values.get('workdays_only', False) if re.search(r'工作日|跳过|节假日|请假日', message) else False)
    except ValueError as error:
        fail(str(error), 'clarification_required')
    color = COLORS['薄荷绿']
    specified = [value for name, value in COLORS.items() if name in message or
                 any(char+'色' in message for char in name if char in '红黄绿蓝紫橙')]
    if len(set(specified)) > 1:
        fail('请明确习惯使用哪一种颜色。', 'clarification_required')
    if specified:
        color = specified[0]
    elif re.search(r'#[0-9A-Fa-f]{6}', message):
        color = re.search(r'#[0-9A-Fa-f]{6}', message)[0]
    disabled = bool(re.search(r'不提醒|关闭提醒|无需提醒', message))
    enabled = not disabled and bool(re.search(r'提醒', message)) and values.get('reminder_enabled', False)
    clock = normalize_reminder_time(values.get('reminder_time')) if enabled else None
    explicit_clock = re.search(r'\d{1,2}[:：]\d{2}|[零〇一二三四五六七八九十两\d]+[点时]', message)
    if enabled and (not clock or not explicit_clock):
        fail('请明确习惯提醒时间，例如每天09:00；不会自动编造时间。', 'clarification_required')
    if '提醒' in message and not enabled and not re.search(r'不提醒|关闭提醒|无需提醒', message):
        fail('请提供提醒时间并开启提醒。', 'clarification_required')
    item = RoutineItem(id=identifier, title=values['title'].strip(), kind='habit', color=color,
                       created_on=today.isoformat(), reminder_enabled=enabled, reminder_time=clock, **schedule)
    if not item.title:
        fail('习惯标题不能为空。')
    # Bound observational scan; Store is the sole workday/recurrence authority.
    next_on = None
    for offset in range(366*8):
        day = max(today, anchor)+timedelta(days=offset)
        if item.end_on and day.isoformat() > item.end_on:
            break
        if store.routine_planned_on(item, day):
            next_on = day.isoformat()
            break
    return dict(object_type='habit', event_id=identifier, habit_id=identifier, before=None, after=asdict(item),
                before_hash=fingerprint(None), dates=[], description='新建习惯', schedule=schedule_label(item), next_on=next_on)
