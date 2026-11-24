"""Detached Habit 2.0 creation; never writes events or implements recurrence."""
from dataclasses import asdict, replace
from datetime import date, timedelta
import re

from calendar_core import RoutineItem, COLORS, normalize_reminder_time
from habit_schedule import validate_schedule_input, schedule_label
from desktop_calendar.assistant.calendar_chat_tools import fail, iso_day, reference_today
from desktop_calendar.assistant.calendar_chat_protocol import fingerprint


def habit_intent(message):
    return bool(re.search(r'习惯|每天|每日|每周|每(?:个)?月|每年|每隔|每星期|每个工作日', message))


def habit_patch(values, message):
    if re.search(r'改名|重命名', message) and not re.search(r'[，,；;]|同时|并且', message):
        if 'title' not in values:
            fail('请提供新的习惯名称。', 'clarification_required')
        return {'title': values['title']}
    fields = set()
    for names, pattern in [({'frequency', 'interval'}, r'每天|每日|每周|每月|每个月|每年|每隔|星期'),
            ({'weekdays'}, r'周|星期'), ({'month_day'}, r'月|年'), ({'year_month'}, r'年'),
            ({'start_on'}, r'开始|起始'), ({'end_on'}, r'结束|年底|直到|截至|截止|持续到'),
            ({'color'}, r'颜色|[红黄绿蓝紫橙]色|#[0-9a-fA-F]{6}'),
            ({'workdays_only'}, r'工作日|节假日|请假日|跳过|休息日'),
            ({'reminder_time', 'reminder_enabled'}, r'提醒')]:
        if re.search(pattern, message):
            fields.update(names)
    if re.search(r'改名|重命名|名称|标题', message) or not fields and re.search(r'改成|改为|改叫', message):
        fields.add('title')
    patch = {k: v for k, v in values.items() if k in fields}
    if re.search(r'不.*提醒|关闭提醒|无需提醒|提醒.*(?:关掉|关闭|取消)', message):
        patch.pop('reminder_time', None)
        patch['reminder_enabled'] = False
    if not patch:
        fail('请明确希望修改的习惯属性。', 'clarification_required')
    return patch


def prepare_habit(store, values, identifier, message, previous=None):
    if previous:
        values = habit_patch(values, message)
    requested = set(values)
    if previous:
        values = {**asdict(previous), **values}
    today = reference_today(date.today)
    cadence = re.search(r'每(?:隔[一二三四五六七八九十\d]+)?(?:个)?(天|日|周|星期|月|年)', message)
    period_change = previous is None or bool(requested & {'frequency', 'interval', 'weekdays', 'month_day', 'year_month'})
    if cadence and period_change:
        expected = {'天': 'daily', '日': 'daily', '周': 'weekly', '星期': 'weekly', '月': 'monthly', '年': 'yearly'}[cadence[1]]
        if values['frequency'] != expected:
            fail('周期与用户明确描述不一致，请重新解析习惯周期。', 'invalid_arguments')
    interval = values.get('interval', 1) if '每隔' in message or previous and not period_change else 1
    explicit_interval = re.search(r'每隔(\d+)', message)
    if explicit_interval and period_change:
        interval = int(explicit_interval[1])
    anchor = iso_day(values.get('start_on', today.isoformat())) if previous or re.search(r'开始|起始|起执行', message) else today
    # Validate explicit relative start dates locally, not an LLM weekday label.
    if re.search(r'明天开始', message) and (previous is None or 'start_on' in requested):
        anchor = today+timedelta(days=1)
    elif re.search(r'今天开始', message) and (previous is None or 'start_on' in requested):
        anchor = today
    elif re.search(r'下周开始', message) and (previous is None or 'start_on' in requested):
        anchor = today+timedelta(days=7-today.weekday())
    elif re.search(r'下个月(?:一号|1号|1日)?开始', message) and (previous is None or 'start_on' in requested):
        anchor = date(today.year+(today.month == 12), today.month % 12+1, 1)
    if re.search(r'最后一天|月底|[一二三四五六七八九十\d]+次后结束', message) and (period_change or 'end_on' in requested):
        fail('现有习惯支持固定月日及结束日期，暂不支持月末动态日期或次数结束。', 'clarification_required')
    weekdays = values.get('weekdays', [])
    # Explicit weekday text is checked against Python's Monday=0 convention.
    matched = re.findall(r'(?:每周|星期|周)([一二三四五六日天](?:[、和及,， ]*[一二三四五六日天])*)', message)
    if values['frequency'] == 'weekly' and matched and period_change:
        weekdays = sorted({'一二三四五六日'.index(char) if char != '天' else 6
                           for group in matched for char in group if char in '一二三四五六日天'})
    end_on = values.get('end_on') if previous or re.search(r'结束|直到|截至|截止', message) else None
    if previous is None or 'end_on' in requested:
        if '永不结束' in message:
            end_on = None
        elif '今年年底' in message:
            end_on = date(today.year, 12, 31).isoformat()
    try:
        schedule = validate_schedule_input(frequency=values['frequency'], interval=interval,
            weekdays=weekdays, month_day=values.get('month_day', anchor.day), year_month=values.get('year_month', anchor.month),
            start_on=anchor.isoformat(), end_on=end_on,
            workdays_only=values.get('workdays_only', False) if previous or re.search(r'工作日|跳过|节假日|请假日', message) else False)
    except ValueError as error:
        fail(str(error), 'clarification_required')
    color = previous.color if previous else COLORS['薄荷绿']
    specified = [value for name, value in COLORS.items() if name in message or
                 any(char+'色' in message for char in name if char in '红黄绿蓝紫橙')]
    if len(set(specified)) > 1:
        fail('请明确习惯使用哪一种颜色。', 'clarification_required')
    if specified and (previous is None or 'color' in requested):
        color = specified[0]
    elif re.search(r'#[0-9A-Fa-f]{6}', message) and (previous is None or 'color' in requested):
        color = re.search(r'#[0-9A-Fa-f]{6}', message)[0]
    disabled = bool(re.search(r'不.*提醒|关闭提醒|无需提醒|提醒.*(?:关掉|关闭|取消)', message))
    enabled = not disabled and bool(re.search(r'提醒', message)) and values.get('reminder_enabled', False)
    if previous and 'reminder_time' in requested and not disabled:
        enabled = True
    if previous and not requested & {'reminder_enabled', 'reminder_time'}:
        enabled = previous.reminder_enabled
    clock = normalize_reminder_time(values.get('reminder_time')) if enabled else previous.reminder_time if previous and 'reminder_time' not in requested else None
    explicit_clock = re.search(r'\d{1,2}[:：]\d{2}|[零〇一二三四五六七八九十两\d]+[点时]', message)
    if enabled and (not clock or not explicit_clock) and (previous is None or requested & {'reminder_enabled', 'reminder_time'}):
        fail('请明确习惯提醒时间，例如每天09:00；不会自动编造时间。', 'clarification_required')
    if '提醒' in message and not enabled and not disabled and (previous is None or requested & {'reminder_enabled', 'reminder_time'}):
        fail('请提供提醒时间并开启提醒。', 'clarification_required')
    item = replace(previous, title=values['title'].strip(), color=color, reminder_enabled=enabled, reminder_time=clock, **schedule) if previous else RoutineItem(id=identifier, title=values['title'].strip(), kind='habit', color=color,
                       created_on=today.isoformat(), reminder_enabled=enabled, reminder_time=clock, **schedule)
    item = store.prepare_routine(item)
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
    return dict(object_type='habit', event_id=identifier, habit_id=identifier, before=asdict(previous) if previous else None, after=asdict(item),
                before_hash=fingerprint(asdict(previous) if previous else None), dates=[], description='修改习惯' if previous else '新建习惯', schedule=schedule_label(item), next_on=next_on)
