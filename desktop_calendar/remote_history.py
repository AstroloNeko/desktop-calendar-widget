"""Safe, read-only presentation of the existing remote and Agent receipts."""
from dataclasses import dataclass
import json

from desktop_calendar.assistant.calendar_chat_protocol import ChatError, fingerprint


@dataclass(frozen=True)
class HistoryRow:
    received_at: float
    processed_at: float
    result: str
    upload: str
    notification: str
    model_unavailable: bool = False
    channel: str = 'unknown'


def describe_response(response):
    status = response.get('status')
    result = response.get('operation_result') or {}
    if status == 'uncertain' or response.get('error_code') == 'execution_uncertain':
        return '执行结果不确定'
    if status in ('confirmation_required', 'pending'):
        return '当时等待确认'
    if status in ('clarification_required', 'needs_clarification'):
        return '当时等待补充信息'
    if status == 'cancelled':
        return '当时已取消'
    if result.get('committed') is True:
        if result.get('goal_satisfied') is not True:
            return '已提交，效果待核验'
        action = {'create': '已创建', 'update': '已修改', 'delete': '已删除'}.get(result.get('action'), '已提交')
        count = result.get('count') or len(result.get('event_ids') or ()) or 1
        return f'{action} {count} 项'
    if status in ('failed', 'expired', 'committed_unverified'):
        return {'expired': '操作已过期', 'committed_unverified': '已提交，效果待核验'}.get(status, '处理失败')
    if result.get('goal_satisfied') is True and result.get('committed') is False:
        return '已核验，无需写入'
    if status == 'ok' and (response.get('candidates') or response.get('habit_candidates')
                           or response.get('assistant_message') == '当前查询条件下没有匹配结果。'):
        return '查询完成'
    return '消息已处理，写入状态未记录'


def _legacy_result(record, agent_journal, agent_path):
    owner, request_id = record.get('owner'), record.get('request_id')
    if owner and request_id and record.get('agent_journal') == agent_path:
        try:
            response = agent_journal.get('request', fingerprint([owner, request_id]), owner).get('response')
        except ChatError:
            response = None
        if isinstance(response, dict):
            return describe_response(response), response.get('error_code') == 'model_unavailable'
    try:
        body = json.loads(record.get('receipt', '{}'))
    except (ValueError, TypeError):
        body = {}
    if isinstance(body, dict):
        if body.get('ok') is True:
            return '消息已处理，写入状态未记录', False
        if body.get('ok') is False:
            return '处理失败', body.get('error') == 'model_unavailable'
    return '本地结果未知', False


def describe_history(records, agent_journal, processing_id, scope):
    agent_path = str(agent_journal.path.resolve())
    history = []
    for record in sorted(records, key=lambda row: row['received_at'], reverse=True):
        state = record.get('state')
        model_unavailable = False
        processed_at = record.get('processed_at', 0)
        if record.get('verified_scope') != scope:
            upload = '回执已保留，暂停上传' if state == 'ready' else ''
            history.append(HistoryRow(record['received_at'], 0, '旧任务缺少账号核验记录', upload, ''))
            continue
        if state == 'received':
            result = '已接收，等待处理'
        elif state == 'executing':
            result = '正在处理' if processing_id == record['id'] else '执行结果待核查'
        elif state == 'uncertain':
            result = '执行结果不确定'
        else:
            result = record.get('history_result')
            if result:
                model_unavailable = record.get('model_unavailable') is True
            else:
                result, model_unavailable = _legacy_result(record, agent_journal, agent_path)
        upload = {'ready': '回执待上传', 'submitted': '回执已上传'}.get(state, '暂无回执')
        notification = ''
        if state == 'submitted':
            notification = {
                'sent': '通知已发送', 'failed': '通知失败', 'pending': '通知待确认',
                'skipped': '未发送通知',
            }.get(record.get('notify_status'), '送达未知')
        history.append(HistoryRow(record['received_at'], processed_at, result, upload, notification, model_unavailable,
                                  record.get('channel', 'unknown')))
    return tuple(history)
