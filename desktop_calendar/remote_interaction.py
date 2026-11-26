"""Negotiated business receipts and locally authorized remote interactions."""
from datetime import date
import json
import re
import secrets

from desktop_calendar.assistant.calendar_chat_protocol import (
    ChatError, envelope, fail, fingerprint, require_calendar_write,
)
from desktop_calendar.remote_history import describe_response
from desktop_calendar.remote_reminder_receipt import event_reminder, reminder_fields
from version import __version__


VERSION = 2
LEGACY_CAPABILITIES = ('business_receipt', 'action', 'reference')
V1_CAPABILITIES = (*LEGACY_CAPABILITIES, 'quick_query')
CAPABILITIES = (*V1_CAPABILITIES, 'proactive_action', 'reminder_quick_query', 'reminder_receipt')


def capabilities_for(version):
    return CAPABILITIES if version == 2 else V1_CAPABILITIES if version == 1 else ()


def capability_request(version, capabilities):
    return json.dumps({
        'interaction_schema_version': version, 'capabilities': capabilities, 'client_version': __version__,
    }, separators=(',', ':')).encode('utf-8')


CAPABILITY_REQUEST = capability_request(VERSION, CAPABILITIES)
TTL = 900
JOBS = 'wechat-job'
ACTION_USE = 'remote-action-use'
SOURCE = 'wechat'  # The published Agent's stored visibility source.
UNDO_REPLIES = ('撤销', '撤销上一步', '撤销这次操作')
_MSG_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_ACTION_ID = re.compile(r'[A-Za-z0-9_-]{24,64}')
_ORDINAL = re.compile(r'第([零〇一二两三四五六七八九十0-9]+)[个项条]')
_NOTICE_ID = re.compile(r'[0-9a-f]{64}')


def interaction_capability(value):
    kind = value['kind']
    if kind in ('proactive_action', 'proactive_reference'):
        return 'proactive_action'
    if kind == 'quick_query' and value['command'] in ('reminders','brief'):
        return 'reminder_quick_query'
    return kind


def interaction_day(day):
    if not isinstance(day, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day):
        raise ValueError('快捷查询日期格式无效。')
    result = date.fromisoformat(day)
    if not 1900 <= result.year <= 2199:
        raise ValueError('快捷查询日期范围无效。')
    return result


def interaction_from_job(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2):
        raise ValueError('不支持的远程交互协议。')
    version, kind, source = value['schema_version'], value.get('kind'), value.get('source_msg_id')
    if version == 2 and kind in ('proactive_action','proactive_reference'):
        fields = {'schema_version','kind','notification_id','channel','attempt'}
        identifier, attempt = value.get('notification_id'), value.get('attempt')
        if (not isinstance(identifier,str) or not _NOTICE_ID.fullmatch(identifier) or value.get('channel') != 'qq'
                or type(attempt) is not int or not 1 <= attempt <= 10):
            raise ValueError('主动通知标识无效。')
        if (kind == 'proactive_action' and value.keys() == fields | {'action','target'}
                and value['target'] == identifier and value['action'] in ('acknowledge','view_items','snooze_10m')):
            return value
        if (kind == 'proactive_reference' and value.keys() == fields | {'notification_type'}
                and value['notification_type'] in ('event','ddl','brief')):
            return value
        raise ValueError('主动通知操作无效。')
    if kind == 'quick_query':
        command = value.get('command')
        fields = {'schema_version','kind','command'}
        if version == 2 and command == 'brief':
            if value.keys() == fields | {'date','scope'} and value['scope'] in ('agenda','ddl'):
                interaction_day(value['date'])
                return value
            raise ValueError('简报查询格式无效。')
        if version == 2 and command == 'reminders':
            fields |= {'start','end','filter'}
            if (fields <= value.keys() <= fields | {'channel'}
                    and value['filter'] in ('all','next','attention','missed','snoozed','failed','unknown')
                    and ('channel' not in value or value['channel'] in ('windows','qq'))
                    and 0 <= (interaction_day(value['end']) - interaction_day(value['start'])).days <= 366):
                return value
            raise ValueError('提醒查询格式无效。')
        fields = {'schema_version', 'kind', 'command', 'start', 'end', 'ddl_only'}
        if (not fields <= value.keys() <= fields | {'done'}
                or value['command'] not in ('today', 'tomorrow', 'week', 'ddl')
                or type(value['ddl_only']) is not bool
                or ('done' in value and type(value['done']) is not bool)):
            raise ValueError('快捷查询格式无效。')
        start, end = value['start'], value['end']
        if not all(isinstance(day, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day)
                   for day in (start, end)):
            raise ValueError('快捷查询日期格式无效。')
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if not (1900 <= first.year <= last.year <= 2199 and 0 <= (last - first).days <= 366):
            raise ValueError('快捷查询日期范围无效。')
        return value
    if not isinstance(source, str) or not _MSG_ID.fullmatch(source):
        raise ValueError('缺少有效的原消息标识。')
    fields = {'schema_version', 'kind', 'source_msg_id'}
    if kind == 'action':
        action = value.get('action_id')
        if set(value) == fields | {'action_id'} and isinstance(action, str) and _ACTION_ID.fullmatch(action):
            return value
    if kind == 'reference' and set(value) <= fields | {'index'}:
        if 'index' not in value or type(value['index']) is int and 1 <= value['index'] <= 30:
            return value
    raise ValueError('远程交互格式无效。')


def _event_rows(response):
    outcome = response.get('operation_result') or {}
    pending = response.get('pending_operation') or {}
    preview = pending.get('preview') or {}
    if pending.get('status') == 'pending' and isinstance(preview.get('before'), list):
        return preview['before']
    if pending.get('status') == 'pending' and pending.get('requires_confirmation'):
        return preview.get('summaries') or [preview.get('after_summary') or preview.get('before_summary') or {}]
    return (outcome.get('deleted_events') or outcome.get('events') or
            ([outcome['event']] if outcome.get('event') else response.get('candidates')) or [])


def _result_type(response):
    status = response['status']
    outcome = response.get('operation_result') or {}
    if status == 'committed_unverified':
        return 'partial' if outcome.get('goal_satisfied') is False else 'uncertain'
    if status in ('uncertain', 'executing') or response.get('error_code') == 'execution_uncertain':
        return 'uncertain'
    if status in ('confirmation_required', 'pending'):
        return 'confirmation'
    if status in ('clarification_required', 'needs_clarification'):
        return 'candidates' if response.get('candidates') else 'clarification'
    if status in ('failed', 'expired', 'ready'):
        return 'failed'
    if status in ('cancelled', 'canceled'):
        return 'cancelled'
    if outcome.get('goal_satisfied') is True:
        return outcome['action'] if outcome.get('action') in ('create', 'update', 'delete', 'complete', 'undo') else 'message'
    return 'query' if describe_response(response) == '查询完成' else 'message'


def build_business_receipt(service, record, response):
    """Return presentation data plus private references kept only on this device."""
    result_type = _result_type(response)
    version = record.get('interaction_version', 1)
    extended = version == 2 and record.get('reminder_receipt')
    rows = [row for row in _event_rows(response) if row.get('title') and row.get('date')
            and row.get('object_type') != 'reminder'][:30]
    expires = int(service.clock()) + TTL
    receipt = {'schema_version': version, 'result_type': result_type, 'summary': describe_response(response),
               'events': [{**{key: row[key] for key in ('title', 'date', 'end', 'time', 'done', 'event_type', 'deadline')
                              if key in row},
                           'index': position, **(event_reminder(row) if extended else {})}
                          for position, row in enumerate(rows, 1)], 'available_actions': []}
    private = {'expires_at_ts': expires, 'targets': [], 'actions': {}}
    outcome = response.get('operation_result') or {}
    flags = {key: outcome[key] for key in ('committed', 'goal_satisfied', 'changed')
             if key in outcome and (type(outcome[key]) is bool or outcome[key] is None)}
    if flags:
        receipt['outcome'] = flags
    if extended:
        reminder_fields(receipt, response)
        result_type = receipt['result_type']
    if not record.get('session_id') or not record.get('route'):
        return receipt, private

    def add(kind, *, deadline=expires, index=None, **binding):
        if deadline <= service.clock():
            return None
        identifier = secrets.token_urlsafe(24)
        public = {'id': identifier, 'kind': kind, 'expires_at_ts': deadline}
        if index is not None:
            public['index'] = index
        receipt['available_actions'].append(public)
        private['actions'][identifier] = {**binding, 'kind': kind, 'expires_at_ts': deadline}
        return identifier

    with service.lock:
        session = service.get_session(record['session_id'], record['owner'])
        if result_type == 'confirmation':
            op = response.get('pending_operation') or {}
            visible = session.get('visible_preview', {})
            if (op.get('status') == 'pending' and op['operation_id'] == session['pending'] == visible.get('id')
                    and visible.get('revision') == op['revision'] and visible.get('source') == SOURCE):
                deadline = min(expires, int(op['expires']))
                stage = op.get('stage') or 'confirm'
                config = op.get('preview', {}).get('configuration_plan')
                sensitive = bool(config and config['remote_authorization'])
                wire_stage = 'awaiting_remote_authorization' if extended and sensitive else stage
                receipt['pending_confirmation'] = {'stage': wire_stage, 'expires_at_ts': deadline}
                binding = {'operation_id': op['operation_id'], 'revision': op['revision'], 'stage': stage}
                if not sensitive and op.get('preview', {}).get('object_type') != 'reminder':
                    add('confirm', deadline=deadline, **binding)
                    add('cancel', deadline=deadline, **binding)
        if result_type in ('query', 'candidates'):
            revisions = service.gateway.reference_revisions([row for row in rows if row.get('id')])
            private['targets'] = [{'event_id': row['id'], 'revision': revisions[row['id']]}
                                  if row.get('id') in revisions else None for row in rows]
            if any(private['targets']):
                receipt['reference_until'] = expires
            awaiting = session.get('event_input') or {}
            revision = response.get('selection_revision')
            if result_type == 'candidates' and revision and revision == awaiting.get('revision'):
                for index, target in enumerate(private['targets'], 1):
                    if (target and target['event_id'] in awaiting['ids'] and
                            awaiting.get('event_revisions', {}).get(target['event_id']) == target['revision']):
                        add('select_candidate', deadline=min(expires, int(awaiting['expires'])),
                            index=index, selection_revision=revision, **target)
        result = response.get('operation_result') or {}
        operation_id = result.get('operation_id')
        if result_type in ('create', 'update', 'delete', 'complete') and result.get('committed') is True:
            if operation_id and service.gateway.dispatch(lambda: service.gateway.commands.can_undo_operation(operation_id)):
                undo_reference = add('undo', operation_id=operation_id)
                if undo_reference:
                    receipt['undo_reference'] = undo_reference
    if result_type == 'undo':
        receipt['summary'] = '已撤销原操作'
    return receipt, private


def _reference_number(interaction, text, count):
    matches = _ORDINAL.findall(text)
    if len(matches) > 1:
        fail('一次引用只能修改一个明确的事项。', 'reference_conflict')
    ordinal = None
    if matches:
        value = matches[0].replace('两', '二')
        if value.isdigit():
            ordinal = int(value)
        elif len(value) == 1 and value in '一二三四五六七八九十':
            ordinal = '一二三四五六七八九十'.index(value) + 1
        else:
            fail('请使用有效的数字编号。', 'reference_conflict')
    index = interaction.get('index', ordinal if ordinal is not None else (1 if count == 1 else 0))
    if not 1 <= index <= count or ordinal is not None and ordinal != index:
        fail('请在引用消息中明确指定事项编号。', 'reference_conflict')
    return index


def execute_undo_reply(service, journal, scope, record, reference_now):
    previous = max((row for row in journal.all(JOBS, scope) if row['id'] != record['id']
                    and row.get('route') == record['route'] and row.get('owner') == record['owner']),
                   key=lambda row: row['received_at'], default=None)
    if not previous:
        fail('当前会话没有可撤销的远程操作。', 'action_expired')
    return execute_interaction(service, journal, scope, record, reference_now,
                               context={'kind': 'reference', 'source_msg_id': previous['msg_id']})


def proactive_response(record, proof):
    result = {'action':'reminder_action', 'kind':proof['action'], 'count':proof['count'],
              'until':proof['until'], 'changed':proof['changed'], 'committed':True, 'goal_satisfied':True,
              'scheduling_pending':bool(proof.get('scheduling_pending'))}
    return envelope(record['session_id'],record['request_id'],status='succeeded',operation_result=result)


def proactive_id(record):
    return 'proactive-'+fingerprint([record['request_id'],record['route'],record['interaction']])


def recover_proactive(service, record):
    def read():
        engine = service.gateway.reminders
        return engine.runtime_receipt(proactive_id(record)) if engine is not None else None
    proof = service.gateway.dispatch(read)
    return proactive_response(record,proof) if proof else None


def execute_proactive(service, record, context, reference_now, worker):
    from desktop_calendar.assistant.calendar_reminder_tools import query_brief
    from desktop_calendar.reminders.reminder_engine import ReminderError, local_wall
    from desktop_calendar.storage.calendar_repository import RepositoryConflictError

    if record['channel'] != 'qq' or not -60 <= service.clock()-reference_now.timestamp() < TTL:
        fail('通知交互已过期，请在桌面端重新核对。','action_expired')

    def owned():
        gateway = service.gateway
        engine = gateway.reminders
        binding = engine.delivery.binding() if engine is not None else None
        if worker is None or binding is None or binding.worker is not worker:
            fail('未核验此主动通知的绑定或能力。','action_expired')
        require_calendar_write()
        now = local_wall(gateway.reminder_now())
        identifier, attempt = context['notification_id'], context['attempt']
        notification_type = context.get('notification_type')
        _, _, batch, qq = engine.verified_qq_batch(identifier,binding,now,attempt,notification_type)
        action, index, minutes = context.get('action'), None, 10
        if action is None:
            text = record['text'].strip(' 。！!')
            if notification_type == 'brief':
                if text not in ('查看简报','查看事项','查看DDL','查看截止事项'):
                    fail('引用简报可回复「查看简报」或「查看DDL」。','clarification_required')
                brief = query_brief(gateway,{'date':batch['when'][:10],
                                            'scope':'ddl' if text in ('查看DDL','查看截止事项') else 'agenda'},SOURCE)
                return envelope(record['session_id'],record['request_id'],assistant_message=brief['text'],
                                brief_result=brief,plain_reminder=True)
            count = len(qq['members'])
            if _ORDINAL.search(text):
                index = _reference_number(context,text,min(count,20))
                text = _ORDINAL.sub('',text).strip(' ：:，,')
            text, entire = re.subn(r'^(?:所有|全部|整条)\s*|(?:都|全部)$','',text)
            if text in ('知道了','已处理','标记已处理'):
                action = 'acknowledge'
            elif text in ('查看事项','查看提醒','查看'):
                action = 'view_items'
            elif match := re.fullmatch(r'(?:稍后|推迟|延后)?\s*([0-9]{1,4})\s*分钟(?:后)?(?:提醒)?',text):
                action, minutes = 'snooze_10m', int(match[1])
            elif text in ('推迟半小时','延后半小时','半小时后提醒'):
                action, minutes = 'snooze_10m', 30
            else:
                fail('请回复「查看事项」「知道了」或「10分钟后提醒」。多项可指定编号或全部。','clarification_required')
            if action != 'view_items' and count > 1 and index is None and not entire:
                fail('请指定原通知中的编号，或明确操作全部有效提醒。','clarification_required')
        if action not in binding.supported_actions:
            fail('当前绑定未启用该主动通知动作。','action_expired')
        if not gateway.writable():
            fail('请关闭正在编辑的窗口后重试。','service_unavailable')
        key = None if action == 'view_items' else proactive_id(record)
        result = engine.apply_qq_action(identifier,action,binding,now,attempt=attempt,
                                        notification_type=notification_type,operation_id=key,index=index,minutes=minutes)
        if action == 'view_items':
            items = result['items']
            text = f'原通知中仍有效的事项共 {len(items)} 项。'
            text += ''.join(f"\n{n}. {item['title'][:55]}" for n,item in enumerate(items[:20],1))
            if len(items) > 20:
                text += '\n已展示前 20 项，其余请在桌面端查看。'
            return envelope(record['session_id'],record['request_id'],assistant_message=text,plain_reminder=True)
        return proactive_response(record,{**result,'scheduling_pending':engine.dirty})

    try:
        return service.gateway.dispatch(owned)
    except (ReminderError, RepositoryConflictError) as error:
        fail(str(error),'reminder_conflict')


def execute_interaction(service, journal, scope, record, reference_now, *, context=None, worker=None):
    context = context or record['interaction']
    if context['kind'] == 'quick_query':
        if context['command'] in ('reminders','brief'):
            from desktop_calendar.assistant.calendar_reminder_tools import query_brief, query_reminders, row_text
            if context['command'] == 'brief':
                result = service.gateway.dispatch(lambda: query_brief(
                    service.gateway,{key:context[key] for key in ('date','scope')},SOURCE))
                return envelope(record['session_id'],record['request_id'],assistant_message=result['text'],
                                brief_result=result,plain_reminder=True)
            result = service.gateway.dispatch(lambda: query_reminders(service.gateway,{
                key:context[key] for key in ('start','end','filter','channel') if key in context},SOURCE))
            text = f"查询到 {result['total']} 条提醒实例。"
            if not result['enabled']:
                text += ' 全局日程提醒已关闭，仍可查看历史记录。'
            text += ''.join(f'\n{n}. {row_text(row)}' for n,row in enumerate(result['rows'],1))
            if len(result['rows']) < result['total']:
                text += '\n其余提醒请在桌面端查看。'
            return envelope(record['session_id'],record['request_id'],assistant_message=text,
                            reminder_query=result,plain_reminder=True)
        query = service.gateway.query({key: context[key] for key in ('start', 'end', 'ddl_only', 'done')
                                       if key in context})
        events = query['events']
        summary = '当前查询条件下没有匹配结果。'
        if events:
            summary = (f"查到 {query['total']} 项，列出前 {len(events)} 项。" if query['truncated']
                       else f'查到 {len(events)} 项。')
        return envelope(record['session_id'], record['request_id'], assistant_message=summary, candidates=events)
    if context['kind'] in ('proactive_action','proactive_reference'):
        return execute_proactive(service,record,context,reference_now,worker)
    try:
        origin = journal.get(JOBS, fingerprint([scope, context['source_msg_id']]), scope)
    except ChatError:
        fail('找不到原远程回执，请重新查询。', 'reference_conflict')
    local = origin.get('interaction_reply') or {}
    if (origin.get('verified_scope') != scope or origin.get('state') != 'submitted'
            or origin.get('route') != record['route'] or origin.get('owner') != record['owner']
            or origin.get('session_id') != record['session_id'] or local.get('expires_at_ts', 0) <= service.clock()):
        fail('引用或按钮已失效，或不属于当前会话。', 'reference_conflict')
    owner, sid, request_id = record['owner'], record['session_id'], record['request_id']
    action_id = context.get('action_id')
    if context['kind'] == 'reference':
        if record['text'].strip(' 。！!') in UNDO_REPLIES:
            action_id = next((key for key, value in local['actions'].items() if value['kind'] == 'undo'), None)
        else:
            targets = local.get('targets', [])
            target = targets[_reference_number(context, record['text'], len(targets)) - 1]
            if not target:
                fail('引用的事项已变更，请重新查询。', 'reference_conflict')
            return service.send_message(sid, record['text'], request_id, owner, SOURCE,
                                        reference_now=reference_now, reference_target=target)
    action = local.get('actions', {}).get(action_id)
    if not action or action['expires_at_ts'] <= service.clock():
        fail('该操作按钮已失效。', 'action_expired')
    key = fingerprint([origin['id'], action_id])
    try:
        journal.get(ACTION_USE, key, scope)
    except ChatError as error:
        if error.code != 'authentication_failed':
            raise
    else:
        fail('该操作按钮已处理。', 'action_already_used')
    session = service.get_session(sid, owner)
    kind = action['kind']
    if kind in ('confirm', 'cancel'):
        operation = (service.get_pending_operations(sid, owner).get('pending_operation')
                     if session['pending'] == action['operation_id'] else None)
        if not (operation and operation['status'] == 'pending' and operation['revision'] == action['revision']
                and (operation.get('stage') or 'confirm') == action['stage']):
            fail('待确认事项已更新或结束，请重新核对。', 'operation_conflict')
        visible = session.get('visible_preview', {})
        if (visible.get('source') != SOURCE or visible.get('id') != action['operation_id']
                or visible.get('revision') != action['revision']):
            fail('待确认卡片已失效，请重新核对。', 'operation_conflict')
        if kind == 'confirm' and operation['preview'].get('configuration_plan',{}).get('remote_authorization'):
            fail('请明确回复「授权确认」，按钮无法授权私人数据发送。','confirmation_required')
    if kind == 'select_candidate':
        waiting = session.get('event_input') or {}
        event_id = action['event_id']
        if (waiting.get('revision') != action['selection_revision'] or waiting['expires'] <= service.clock()
                or event_id not in waiting['ids']
                or service.gateway.event_revisions([event_id]).get(event_id) != action['revision']):
            fail('候选清单已变化，请重新查询。', 'operation_conflict')
    require_calendar_write()
    journal.put(ACTION_USE, key, scope, {'used_at': service.clock()})
    if kind == 'cancel':
        return service.cancel_operation(sid, action['operation_id'], owner, SOURCE)
    if kind == 'undo':
        operation_id = 'undo-' + key
        change = service.gateway.undo_remote_operation(action['operation_id'], operation_id)
        members = getattr(change, 'changes', (change,))
        message = '已撤销原操作。'
        if service.gateway.reminders is not None or any(
                entry.before.reminder_rule is not None or entry.after.reminder_rule is not None for entry in members):
            message += ' 已发送的通知无法撤回。'
        return envelope(sid, request_id, status='succeeded', assistant_message=message,
                        operation_result={'operation_id': operation_id, 'action': 'undo', 'committed': True,
                                          'goal_satisfied': True, 'count': len(members)})
    if kind == 'select_candidate':
        message = str(waiting['ids'].index(action['event_id']) + 1)
    else:
        message = {'reviewing_batch': '删除这批', 'awaiting_final_confirmation': '最终确认删除',
                   'reviewing_specific': '确认删除'}.get(action['stage'], '确认')
    return service.send_message(sid, message, request_id, owner, SOURCE, reference_now=reference_now)
