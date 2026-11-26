"""Shared chat errors, identities, and response envelopes."""
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar


OWNER = 'local-owner'  # Derived from the adapter's authentication, never client sender_id.
SPECIFIC_DELETE_STAGE = 'reviewing_specific'
_calendar_write_check = ContextVar('calendar_write_check', default=None)


@contextmanager
def calendar_write_guard(check):
    token = _calendar_write_check.set(check)
    try:
        yield
    finally:
        _calendar_write_check.reset(token)


def require_calendar_write():
    check = _calendar_write_check.get()
    if check is not None and not check():
        fail('远程服务授权已改变，本次未写入日历。', 'remote_authorization_changed')


def affirmative_reply(text):
    return bool(re.fullmatch(r'对的(?:[，, ]*对的)?|对|是的|就这样|没错', text))


class ChatError(ValueError):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.field = field


def fail(message, code="invalid_arguments", field=None):
    raise ChatError(code, message, field)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def receipt_message(result, *, concise=False, explicit=()):
    if result.get('action') == 'reminder_action':
        label = {'snooze':'已设置稍后提醒','cancel_snooze':'已取消稍后提醒',
                 'acknowledge':'已标记提醒已处理'}[result['kind']]
        text = f"{label}（{result['count']} 项）"
        if result.get('until'):
            text += ' · '+result['until'].replace('T',' ')
        if result.get('scheduling_pending'):
            text += ' · 等待调度重算'
        if result['kind'] == 'cancel_snooze':
            text += '。原规则按现有补发和去重策略重新评估。'
        text += '。事项时间、截止时间、完成状态和长期规则不受影响。此类运行操作不进入日历撤销历史。'
        return text
    if 'committed' in result and result.get('goal_satisfied') is None:
        if result['committed'] is None:
            return '执行结果暂时无法确认，未自动重试。'
        return '操作已提交，暂时无法核验目标效果。' if result['committed'] else '已确认本次操作未提交。'
    if result.get('goal_satisfied') is False:
        return '操作已提交，但目标效果核验未通过。' if result.get('committed') else '目标效果核验未通过。'
    habit = result.get('habit')
    if habit:
        action = '已更新' if result['action']=='updated' else '已创建'
        if result.get('changed') is False:
            action = '原本已符合要求'
        clock = ' · 提醒 '+habit['reminder_time'] if habit.get('reminder_time') else ''
        return f"{action}习惯「{habit['title']}」 · {habit['schedule']}{clock}。"
    if result.get('deleted'):
        message = f"已删除 {result.get('count',1)} 项"
        message += '，已核对恢复结果。' if result.get('recovered') else '，可撤销。'
        rows = result.get('deleted_events',[])
        return message + ''.join(f"\n{event_line(row)}" if concise else f"\n{row['date']} · {row['title']}" for row in rows)
    rows = result.get('events') or ([result['event']] if result.get('event') else [])
    if not rows:
        return '已核对执行结果。' if result.get('recovered') else ''
    label = {'create':'已创建','complete':'已更新完成状态'}.get(result.get('action'),'已修改')
    if result.get('changed') is False and result.get('goal_satisfied') is True:
        label = '原本已符合要求，无需修改'
    details = []
    for row in rows:
        if concise:
            fields = [event_line(row)]
            event_type = row.get('event_type')
            if event_type in ('urgent', 'ddl') or 'event_type' in explicit:
                fields.append({'urgent': '紧急', 'ddl': 'DDL'}.get(event_type, '一般事项'))
            if {'color', 'color_mode'}.intersection(explicit):
                color = row.get('color')
                if color:
                    fields.append(('跟随分类颜色 ' if row.get('color_mode') == 'inherit' else '颜色 ') + color)
            reminder = row.get('reminder')
            if row.get('reminder_plan'):
                fields.append(reminder_line(row))
            elif reminder is not None or 'reminder' in explicit:
                fields.append('不提醒' if reminder is None else f'提前 {reminder} 分钟提醒')
            if row.get('done') or result.get('action') == 'complete' or 'done' in explicit:
                fields.append('已完成' if row.get('done') else '未完成')
            if 'category_id' in explicit:
                fields.append('分类 ' + (row.get('category') or '无'))
            if 'notes' in explicit:
                fields.append('备注 ' + (row.get('notes') or '无'))
            if 'skip_non_working_days' in explicit:
                fields.append('跳过非工作日' if row.get('skip_non_working_days') else '按自然日')
            if row.get('end_as_ddl') or 'end_as_ddl' in explicit:
                fields.append('最后一天为 DDL' if row.get('end_as_ddl') else '最后一天不设 DDL')
            details.append(' · '.join(fields))
            continue
        dates = row['date'] if row['date']==row.get('end',row['date']) else f"{row['date']} 至 {row['end']}"
        reminder = (reminder_line(row) if row.get('reminder_plan') else
                    '不提醒' if row['reminder'] is None else f"提前 {row['reminder']} 分钟提醒")
        details.append(f"{dates} · {row['title']} · {row['time'] or '全天'} · 有效颜色 {row['color']} · {reminder} · "
                       + ('已完成' if row['done'] else '未完成'))
    return f"{label}（{len(rows)} 项）\n" + '\n'.join(details)


def event_line(row):
    date = row['date']
    dates = f"{date} 至 {row['end']}" if row.get('end') and row['end'] != date else date
    return f"{dates} · {row['title']} · {row.get('time') or '全天'}"


def reminder_line(row):
    plan = row.get('reminder_plan')
    if not plan:
        return ''
    rule = plan['rule']
    mode = plan['mode']
    label = {'default':'默认提醒', 'relative':'相对提醒', 'absolute':'固定时刻提醒', 'disabled':'不提醒'}[mode]
    fields = [label]
    if mode == 'relative':
        minutes = rule.get('minutes', row.get('reminder'))
        if 'days' in rule:
            fields.append(f"提前 {rule['days']} 天")
        elif minutes is not None:
            fields.append(f'提前 {minutes} 分钟')
    if row.get('deadline'):
        fields.append('截止 '+row['deadline']+(' '+row['time'] if row.get('time') else '（无具体时刻）'))
    if plan.get('planned_at'):
        fields.append('提醒时刻 '+plan['planned_at'].replace('T', ' '))
    elif mode != 'disabled':
        fields.append('当前无有效触发时间')
    if plan.get('global_enabled') is False:
        fields.append('全局提醒已关闭')
    if plan.get('snoozed_at'):
        fields.append('当前稍后提醒 '+plan['snoozed_at'].replace('T', ' '))
    if plan.get('past'):
        fields.append('已到或已过提醒时刻')
        fields.append('符合当前补发条件' if plan.get('catchup_eligible') else '当前不符合补发条件')
    if plan['schedule_status'] == 'pending' and (mode != 'disabled' or plan.get('global_enabled') is not False):
        fields.append('规则已保存，等待调度重算')
    return ' · '.join(fields)


def wechat_delete_review(operation):
    preview = operation['preview']
    batch = preview.get('object_type') == 'batch_delete'
    rows = preview['before'] if batch else [preview.get('before_summary')]
    if not rows or any(not row for row in rows):
        return None
    lines = [f"请核对以下 {len(rows)} 个待删除事项。"]
    lines.extend(f"{i}. {event_line(row)}" for i, row in enumerate(rows, 1))
    action = {'reviewing_batch': '删除这批', 'awaiting_final_confirmation': '最终确认删除',
              SPECIFIC_DELETE_STAGE: '确认删除'}.get(operation.get('stage'),
                                                      '确认删除' if operation.get('specific_delete') else '确认')
    lines.append(f'核对后回复“{action}”，或回复“取消”。')
    review = '\n'.join(lines)
    return review if len(review.encode('utf-8')) <= 1900 else None


def envelope(session_id=None, request_id=None, **fields):
    result = fields.pop('operation_result', None)
    message = fields.pop('assistant_message', '') or (receipt_message(result) if result else '')
    return dict(request_id=request_id, session_id=session_id, status=fields.pop('status', 'ok'),
                assistant_message=message, pending_operation=fields.pop('pending_operation', None),
                candidates=fields.pop('candidates', []), operation_result=result,
                error_code=fields.pop('error_code', None), error_message=fields.pop('error_message', None), **fields)
