"""Resolve explicit Event plan constraints without another model round trip."""
import re
from datetime import date, timedelta
from desktop_calendar.assistant.calendar_chat_protocol import fail


def compile_proposal(proposal):
    if 'items' not in proposal or set(proposal) - {'action','items','distinct_colors','preview_only'} or proposal['action']=='delete':
        fail('成员计划需要提供合法的动作和 items；按条件定位事项请使用 target。')
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


def validate_target_plan(plan):
    action = plan['action']
    allowed = {'update':{'fields','color_policy'}, 'complete':{'done'}, 'delete':set()}
    if action not in allowed or set(plan) - ({'action','target','preview_only'} | allowed[action]):
        fail('目标计划的动作与参数不匹配。')
    if action == 'update' and not (plan.get('fields') or plan.get('color_policy')):
        fail('修改计划缺少修改值。')
    if plan.get('color_policy') and {'color','color_mode'} & plan.get('fields',{}).keys():
        fail('请只指定一种颜色策略。')
    if action == 'complete' and 'done' not in plan:
        fail('完成计划缺少 done。')
    target = plan['target']
    if ('start' in target) != ('end' in target) or any(not title.strip() for title in target['titles']):
        fail('请提供完整日期区间及有效标题。')


def member_count(message):
    match = re.search(r'(?<![第\d])([两二三四五六七八九十]|10|[2-9])\s*(?:个|条|项|场|次)(?:独立)?', message)
    if not match:
        return None
    token = match[1].replace('两', '二')
    return int(token) if token.isdigit() else '一二三四五六七八九十'.index(token) + 1


def independent_dates(message, anchor):
    if re.search(r'明后天|明天[、，和与及\s]*后天', message):
        return [(anchor + timedelta(days=n)).isoformat() for n in (1, 2)]
    return []


def check_create_count(message, actual):
    count = member_count(message)
    if count and actual != count:
        fail(f'请求包含{count}个成员，请提交一个包含全部成员的批量创建计划。', 'invalid_arguments')


def check_creation_members(members, message, days):
    check_create_count(message, len(members))
    if not days:
        return
    separate = re.search(r'各|分别|独立', message)
    spanning = re.search(r'(?:一个|一条|一项).{0,8}跨日|(?:同一|一个).{0,8}跨[两二]天', message)
    if spanning or (len(members) == 1 and not separate):
        first = members[0]
        end_matches = first.get('end_date') == days[-1] if 'end_date' in first else (
            first.get('duration_days') == len(days) and not first.get('skip_non_working_days'))
        if len(members) != 1 or first['date'] != days[0] or not end_matches:
            fail('请保留一个覆盖所述日期的跨日事项。')
    elif sorted(item['date'] for item in members) != days:
        fail('成员日期与原请求不一致，请重新规划明天和后天的两个成员。')


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
