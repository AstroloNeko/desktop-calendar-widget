"""Explicit Event input only: no UI, language inference, persistence or commands."""
from copy import deepcopy
from datetime import date, datetime
import re

from calendar_core import Event, EVENT_TYPES, EVENT_COLOR_MODES


class EventInputError(ValueError):
    def __init__(self, field, message, *, clarification=False):
        super().__init__(message)
        self.field = field
        self.clarification = clarification


def _invalid(field, message, *, clarification=False):
    raise EventInputError(field, message, clarification=clarification)


def parse_event_due(date_text, time_text):
    """Desktop text adapter; retain its whitespace and strptime compatibility."""
    try:
        day = datetime.strptime(date_text.strip(), '%Y-%m-%d').date()
        clock = time_text.strip()
        if clock:
            return datetime.strptime(f'{day.isoformat()} {clock}', '%Y-%m-%d %H:%M'), True
        return datetime.combine(day, datetime.min.time()).replace(hour=23, minute=59), False
    except (ValueError, TypeError, AttributeError):
        _invalid('date', '日期或时间格式不正确。日期请使用 YYYY-MM-DD；时间可以留空，填写时请使用 HH:MM。')


def _day(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _invalid(field, '日期必须是 YYYY-MM-DD。')
    try:
        return date.fromisoformat(value)
    except ValueError:
        _invalid(field, '日期必须是有效的 YYYY-MM-DD。')


FIELDS = frozenset(('title', 'date', 'time', 'end_date', 'event_type', 'duration_days',
    'skip_non_working_days', 'end_as_ddl', 'category_id', 'color', 'color_mode',
    'reminder', 'notes', 'done'))


def build_event_candidate(values, *, current=None, identifier=None, context=None, creating=None):
    """Create or patch a detached Event; context supplies category/workday rules.

    Missing fields preserve values; nullable fields are time/category_id/reminder.
    Legacy file normalization remains in Event.from_dict, not this input boundary.
    ``creating`` preserves proposal-amendment semantics for an unsaved candidate.
    """
    if not isinstance(values, dict) or set(values) - FIELDS:
        _invalid('fields', '事项输入包含未知字段。')
    values = dict(values)
    creating = current is None if creating is None else creating
    for key, value in values.items():
        if key in ('title', 'notes'):
            if not isinstance(value, str) or (key == 'title' and not value.strip()):
                _invalid(key, '事项标题不能为空。' if key == 'title' else '备注必须是文本。')
        elif key in ('skip_non_working_days', 'end_as_ddl', 'done'):
            if type(value) is not bool:
                _invalid(key, '事项开关必须是布尔值。')
        elif key == 'duration_days':
            if type(value) is not int or not 1 <= value <= 365:
                _invalid(key, '持续天数请输入 1～365 之间的整数。')
        elif key == 'reminder':
            if value is not None and (type(value) is not int or not 0 <= value <= 10080):
                _invalid(key, '提醒提前量必须为0至10080分钟或不提醒。')
        elif key == 'event_type':
            if not isinstance(value, str) or value not in EVENT_TYPES:
                _invalid(key, '事项性质无效。')
        elif key == 'color_mode':
            if not isinstance(value, str) or value not in EVENT_COLOR_MODES:
                _invalid(key, '颜色来源无效。')
        elif key == 'color':
            if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                _invalid(key, '颜色格式不正确。')
        elif key == 'category_id':
            if value is not None and (not isinstance(value, str) or not value or context is None or context.category_by_id(value) is None):
                _invalid(key, '分类不存在。')
    if current is None:
        if not identifier or 'title' not in values or 'date' not in values:
            _invalid('fields', '新建事项需要标题、日期和稳定ID。')
        day = _day(values['date'], 'date')
        candidate = Event(identifier, values['title'], day.isoformat()+'T23:59', has_time=False, reminder=None)
    else:
        candidate = deepcopy(current)
    day = _day(values.pop('date'), 'date') if 'date' in values else candidate.due_date
    if 'time' in values:
        clock = values.pop('time')
        if clock is not None and (not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock)):
            _invalid('time', '时间必须是 HH:MM，或明确选择无具体时间。')
        candidate.has_time = clock is not None
        candidate.due = day.isoformat()+'T'+(clock if clock is not None else '23:59')
    elif day != candidate.due_date:
        candidate.due = day.isoformat()+candidate.due[10:]
    end = _day(values.pop('end_date'), 'end_date') if 'end_date' in values else None
    if 'category_id' in values and 'color' not in values and 'color_mode' not in values:
        values['color_mode'] = ('inherit' if values['category_id'] else 'override') if creating or candidate.color_mode == 'inherit' else 'override'
    if 'color' in values:
        values.setdefault('color_mode', 'override')
    if values.get('color_mode', candidate.color_mode) == 'inherit' and not values.get('category_id', candidate.category_id):
        _invalid('color_mode', '此事项没有分类，请先选择分类，或保留当前颜色。', clarification=True)
    if 'reminder' in values and values['reminder'] is not None and not candidate.has_time:
        _invalid('reminder', '此事项没有具体时间，请先确认提醒时刻；不能擅自设置定时提醒。', clarification=True)
    for key, value in values.items():
        setattr(candidate, key, value)
    if not candidate.has_time:
        candidate.reminder = None
    if end is not None:
        if context is None or end < day or (end-day).days > 1095:
            _invalid('end_date', '结束日期应在开始日期之后且范围合理。')
        try:
            candidate.duration_days = context.duration_for_range(day, end, skip_non_working_days=candidate.skip_non_working_days)
        except ValueError:
            _invalid('end_date', '日期范围应包含1至365个有效日期。')
        if context.event_dates(candidate)[-1] != end:
            _invalid('end_date', '结束日期不是有效工作日，请明确最后执行日期。', clarification=True)
    if candidate.end_as_ddl and (candidate.duration_days == 1 or candidate.event_type == 'ddl'):
        _invalid('end_as_ddl', '末日DDL仅用于多日一般/紧急事项。')
    if candidate.color_mode == 'inherit' and context is not None:
        category = context.category_by_id(candidate.category_id)
        if category is None:
            _invalid('category_id', '分类不存在。')
        candidate.color = category.color
    if context is not None:
        try:
            context.event_dates(candidate)
        except (ValueError, OverflowError):
            _invalid('date', '事项日期范围超出支持范围。')
    return candidate
