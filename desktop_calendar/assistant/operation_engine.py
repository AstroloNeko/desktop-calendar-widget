"""Execute an authorized Event preview once, then verify the stored postconditions."""
from dataclasses import asdict
import secrets

from calendar_event_service import EventOperationError
from desktop_calendar.assistant.calendar_chat_tools import ChatError, fail


def observe(gateway, expected, colors):
    details = []
    for snapshot, color in zip(expected, colors, strict=True):
        event = gateway.store.event_by_id(snapshot['id'])
        if event is None or asdict(event) != snapshot:
            return None
        detail = gateway._detail(event)
        if detail['color'] != color:
            return None
        details.append(detail)
    return details


def execute(gateway, journal, kind, record, owner, command, expected, colors):
    """Called on the calendar owner thread. Keep the existing adapter receipt formats."""
    identifier = record['operation_id']
    before = {'status': record['status'], 'revision': record.get('revision')}
    if record['status'] != 'ready':
        raise ChatError('operation_conflict', '计划尚未取得执行授权。')
    record.update(status='executing', expected_colors=colors, execution_protocol=1)
    try:
        journal.transition(kind, identifier, owner, before, record)
    except ChatError as exc:
        if exc.code == 'operation_conflict':
            raise ChatError('claim_conflict', '已有执行器领取当前计划。') from exc
        raise
    try:
        outcome = gateway.events.commit(command, operation_id=identifier)
    except EventOperationError as exc:
        raise ChatError(exc.code, str(exc)) from exc
    details = observe(gateway, expected, colors)
    if details is None:
        raise ChatError('execution_uncertain', '写入后的事项与预览不符，请核查结果。')
    if kind == 'task' or record.get('preview', {}).get('object_type') == 'event_batch':
        result = dict(task_id=identifier, events=details, event_ids=[item['id'] for item in expected], changed=outcome.changed)
    else:
        result = dict(event=details[0], event_id=details[0]['id'], deleted=False, changed=outcome.changed)
    if kind == 'task':
        record.update(status='completed', result=result)
    else:
        record.update(status='succeeded', operation_result=result)
    journal.put(kind, identifier, owner, record)
    return outcome


def recovered(gateway, record, expected):
    """New SQLite plans need both the durable transaction receipt and its effects."""
    receipt = getattr(gateway.repository, 'receipt', None)
    if record.get('execution_protocol'):
        if receipt:
            if not receipt(record['operation_id']):
                return False
        else:
            # On legacy storage, a fresh host-allocated create ID is identifiable.
            # A matching update alone cannot prove which request applied it.
            preview = record.get('preview', {})
            creating = (record.get('operation') == 'batch_create_events'
                        or preview.get('creation') or (preview and preview.get('object_type') != 'event_batch'
                                                      and preview.get('before') is None))
            if not creating:
                return False
    colors = record.get('expected_colors')
    if colors is not None:
        return observe(gateway, expected, colors) is not None
    return all((event := gateway.store.event_by_id(item['id'])) is not None and asdict(event) == item
               for item in expected)


def resolve_target(service, plan, session, owner, request_id, source, chosen=None):
    from desktop_calendar.assistant.calendar_chat_service import envelope
    from desktop_calendar.assistant.operation_plan import validate_target_plan, compile_proposal
    from desktop_calendar.assistant.calendar_batch_delete import prepare
    validate_target_plan(plan)
    sid = session['session_id']
    target = plan['target']
    pending = service._operation(sid,session['pending'],owner) if session.get('pending') else None
    if pending and pending['status'] in ('ready','executing','uncertain'):
        fail('上一计划尚待核对。','execution_uncertain')
    if pending and pending['status']=='pending':
        if pending['preview'].get('object_type') != 'batch_delete' or plan['action'] != 'delete':
            fail('请先确认或取消当前预览。','confirmation_required')
        service.cancel_operation(sid,pending['operation_id'],owner)
        session['pending'] = None
    session.pop('awaiting_target',None)
    session.pop('delete_scope',None)
    session.pop('batch_delete_intent',None)
    def ask(question):
        session['awaiting_target'] = dict(plan=plan,revision=secrets.token_urlsafe(16),expires=service.clock()+900)
        return envelope(sid,request_id,status='clarification_required',assistant_message=question)

    if 'start' not in target:
        return ask('请给出要操作的开始和结束日期。')
    bounds = {key:target[key] for key in ('start','end')}
    result = service.gateway.delete_candidates(bounds,target['titles'] or None)
    rows = result['events']
    if not rows or result['truncated'] or (target['quantity']=='all_matches' and len(rows)>10):
        return ask('该范围内没有匹配事项。' if not rows else '匹配项过多，请缩小范围。')
    missing = set(target['titles']) - {row['title'].strip() for row in rows}
    if missing:
        return ask('未找到：'+'、'.join(sorted(missing)))
    if chosen:
        rows = [row for row in rows if row['id']==chosen]
        if not rows:
            fail('选择的事项已不符合原条件，请重新查询。','operation_conflict')
    for row in rows:
        row.pop('notes',None)
    session.update(candidates=rows,habit_candidates=[],selected_kind='event',selected=rows[0]['id'] if len(rows)==1 else None)
    if target['quantity']=='one' and len(rows)>1:
        revision = secrets.token_urlsafe(16)
        session['event_input'] = dict(plan=plan,ids=[r['id'] for r in rows],revision=revision,expires=service.clock()+900)
        return envelope(sid,request_id,status='clarification_required',candidates=rows,selection_revision=revision,
                        assistant_message='有多个匹配事项，请选择要操作的一项。')
    if plan['action']=='delete':
        service._bind_delete_scope(session,dict(events=rows,total=len(rows),truncated=False),target)
        return prepare(service,session,session['delete_scope']['ids'],owner,request_id)
    items = []
    for row in rows:
        item = dict(event_id=row['id'])
        if plan['action']=='complete':
            item['done'] = plan['done']
        else:
            item['fields'] = dict(plan.get('fields',{}))
            if plan.get('color_policy'):
                palette = service.gateway.categories()['palette'].values()
                item['fields'].update(color=secrets.choice([c for c in palette if c.lower()!=row['color'].lower()]),
                                      color_mode='override')
        items.append(item)
    name,args = compile_proposal(dict(action=plan['action'],items=items))
    return service._tool(name,args,session,owner,request_id,source,structured=True,preview_only=plan.get('preview_only',False))
