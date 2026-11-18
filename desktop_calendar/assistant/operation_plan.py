"""Resolve explicit Event plan constraints without another model round trip."""
import re
from datetime import date, timedelta
from desktop_calendar.assistant.calendar_chat_tools import fail


def compile_proposal(proposal):
    action, items = proposal['action'], proposal['items']
    keys = {'create': {'fields'}, 'update': {'fields','event_id'}, 'complete': {'event_id','done'}}
    if any(set(item) != keys[action] for item in items):
        fail('计划成员缺少必填字段或含有无关字段。')
    distinct = proposal.get('distinct_colors', False)
    if distinct and (len(items) == 1 or action == 'complete'):
        fail('多色策略仅用于批量创建或修改。')
    if action == 'create':
        members = [item['fields'] for item in items]
        single, batch = 'preview_create', 'batch_create_events'
    elif action == 'update':
        members = [dict(event_id=item['event_id'], changes=item['fields']) for item in items]
        single, batch = 'preview_update', 'batch_update_events'
    else:
        if len(items) == 1:
            return 'preview_complete', items[0]
        if len({item['done'] for item in items}) != 1:
            fail('同一完成状态计划需要统一的完成值。')
        return 'batch_set_completion', dict(event_ids=[i['event_id'] for i in items], done=items[0]['done'])
    if len(items) == 1:
        return single, members[0]
    return batch, dict(items=members, distinct_colors=distinct)


def member_count(message):
    match = re.search(r'(?<![第\d])([两二三四五六七八九十]|10|[2-9])\s*(?:个|条|项|场|次)(?:独立)?', message)
    if not match:
        return None
    token = match[1].replace('两', '二')
    return int(token) if token.isdigit() else '一二三四五六七八九十'.index(token) + 1


def independent_dates(message, anchor):
    if re.search(r'明天[、，和与及\s]*后天', message):
        return [(anchor + timedelta(days=n)).isoformat() for n in (1, 2)]
    return []


def check_create_count(message, actual):
    count = member_count(message)
    if count and actual != count:
        fail(f'请求包含{count}个成员，请提交一个包含全部成员的批量创建计划。', 'invalid_arguments')


def selected_reply(message, candidates):
    text = message.strip()
    number = re.fullmatch(r'(\d+)(?:[.．、]\s*(.+))?', text)
    if number and 1 <= int(number[1]) <= len(candidates):
        item = candidates[int(number[1]) - 1]
        if not number[2] or number[2] == item['title']:
            return item['id']
    fragment = re.fullmatch(r'(.+?)(?:的那个|那个|那一个)', text)
    matches = [item for item in candidates if item['title'] == text or
               (fragment and fragment[1] in item['title'])]
    return matches[0]['id'] if len(matches) == 1 else None


def choice_with_color(message, candidates, palette):
    """A displayed candidate may be followed by one explicit palette name."""
    chosen = selected_reply(message, candidates)
    if chosen:
        return chosen, None
    parts = re.split(r'[，,]', message, maxsplit=1)
    if len(parts) != 2 or parts[1].strip() not in palette:
        return None, None
    return selected_reply(parts[0], candidates), palette[parts[1].strip()]


def dated_target(message, candidates):
    """Only resolve a literal title plus a single, explicit target day."""
    matches = re.findall(r'(?<!\d)(?:(\d{4})[-年](\d{1,2})[-月](\d{1,2})|(\d{1,2})(?:号|日|的))', message)
    if len(matches) != 1:
        return None
    year, month, day, short_day = matches[0]
    selected = []
    for item in candidates:
        when = date.fromisoformat(item['date'])
        if item['title'] in message and (
                (year and (when.year, when.month, when.day) == (int(year), int(month), int(day)))
                or (short_day and when.day == int(short_day))):
            selected.append(item['id'])
    return selected[0] if len(selected) == 1 else None
