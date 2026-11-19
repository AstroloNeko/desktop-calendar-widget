"""Shared chat errors, identities, and response envelopes."""
import hashlib
import json


OWNER = 'local-owner'  # Derived from the adapter's authentication, never client sender_id.


class ChatError(ValueError):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.field = field


def fail(message, code="invalid_arguments", field=None):
    raise ChatError(code, message, field)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def receipt_message(result):
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
        return message + ''.join(f"\n{row['date']} · {row['title']}" for row in rows)
    rows = result.get('events') or ([result['event']] if result.get('event') else [])
    if not rows:
        return '已核对执行结果。' if result.get('recovered') else ''
    label = {'create':'已创建','complete':'已更新完成状态'}.get(result.get('action'),'已修改')
    if result.get('changed') is False and result.get('goal_satisfied') is True:
        label = '原本已符合要求，无需修改'
    details = []
    for row in rows:
        dates = row['date'] if row['date']==row.get('end',row['date']) else f"{row['date']} 至 {row['end']}"
        reminder = '不提醒' if row['reminder'] is None else f"提前 {row['reminder']} 分钟提醒"
        details.append(f"{dates} · {row['title']} · {row['time'] or '全天'} · 有效颜色 {row['color']} · {reminder} · "
                       + ('已完成' if row['done'] else '未完成'))
    return f"{label}（{len(rows)} 项）\n" + '\n'.join(details)


def envelope(session_id=None, request_id=None, **fields):
    result = fields.pop('operation_result', None)
    message = fields.pop('assistant_message', '') or (receipt_message(result) if result else '')
    return dict(request_id=request_id, session_id=session_id, status=fields.pop('status', 'ok'),
                assistant_message=message, pending_operation=fields.pop('pending_operation', None),
                candidates=fields.pop('candidates', []), operation_result=result,
                error_code=fields.pop('error_code', None), error_message=fields.pop('error_message', None), **fields)
