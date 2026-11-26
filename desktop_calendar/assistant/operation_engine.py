"""Execute an authorized Event preview once, then verify the stored postconditions."""
from dataclasses import asdict
import json
import secrets
import sqlite3

from calendar_event_service import EventOperationError
from desktop_calendar.assistant.calendar_chat_protocol import SPECIFIC_DELETE_STAGE, ChatError, fail, require_calendar_write
from desktop_calendar.assistant.operation_plan import compile_proposal
from desktop_calendar.reminders.reminder_rules import stored_event_rule


def event_action(record):
    preview = record.get('preview',{})
    if preview.get('object_type') == 'habit':
        return 'habit'
    if preview.get('action'):
        return preview['action']
    if record.get('operation'):
        return {'batch_create_events':'create','batch_set_completion':'complete'}.get(record['operation'],'update')
    if preview.get('after') is None:
        return 'delete'
    return 'create' if preview.get('creation',preview.get('before') is None) else 'update'


def event_result(record, ids, *, changed, committed, verified, details=(), recovered=False):
    action = event_action(record)
    result = dict(action=action, count=len(ids), event_ids=list(ids), operation_id=record['operation_id'],
                  changed=changed, committed=committed, goal_satisfied=verified, recovered=recovered)
    if action == 'delete':
        result['deleted'] = verified is True
        if len(ids) == 1:
            result['event_id'] = ids[0]
        before = record['preview']['before']
        result['deleted_events'] = (before if isinstance(before,list) else [record['preview']['before_summary']]) if verified else []
    elif record.get('task_id') or record['preview'].get('object_type') == 'event_batch':
        result.update(task_id=record['operation_id'], events=details)
    elif details:
        result.update(event=details[0],event_id=ids[0],deleted=False)
    return result


def observe(gateway, expected, colors, rules=None):
    if rules is not None and [stored_event_rule(gateway.store.settings, item['id']) for item in expected] != rules:
        return None
    details = []
    for snapshot, color in zip(expected, colors, strict=True):
        event = gateway.store.event_by_id(snapshot['id'])
        if event is None or asdict(event) != snapshot:
            return None
        detail = gateway.reminder_detail(event)
        if detail['color'] != color:
            return None
        details.append(detail)
    return details


def deletion_ids(record):
    preview = record['preview']
    return preview.get('event_ids',[preview['event_id']])


def deletion_outcome(gateway, record, *, commit_returned=None, recovered=False):
    ids = deletion_ids(record)
    receipt = getattr(gateway.repository, 'receipt', None)
    committed = commit_returned
    remaining = None
    verified = None
    try:
        if receipt and committed is not False:
            proof = receipt(record['operation_id'])
            committed = True if proof and proof['changed'] else None
            if committed:
                affected = json.loads(proof['affected_ids']).get('events',[])
                remaining = sorted(gateway.repository.existing_event_ids(ids))
                verified = not remaining and set(affected) == set(ids)
        elif committed:
            remaining = [i for i in ids if gateway.store.event_by_id(i) is not None]
            verified = not remaining
    except (OSError, sqlite3.Error, ValueError):
        # A readable commit receipt can outlive a failed read of the affected rows.
        pass
    result = event_result(record,ids,changed=committed,committed=committed,verified=verified,recovered=recovered)
    if remaining is not None:
        result['remaining_event_ids'] = remaining
    if verified:
        status, stage, code, message = 'succeeded', 'completed', None, None
    elif committed:
        status, stage, code = 'committed_unverified', 'effect_not_verified', 'effect_not_verified'
        message = '删除已提交，但目标效果核验未通过。' if verified is False else '删除已提交，暂时无法核验目标效果。'
    elif committed is False:
        status, stage, code, message = 'failed', 'failed', 'execution_failed', '已确认本次删除未提交。'
    else:
        status, stage, code, message = 'uncertain', 'uncertain', 'execution_uncertain', '删除结果暂时无法确认，未自动重试。'
    state = dict(status=status,operation_result=result,error_code=code,error_message=message)
    if record['preview'].get('object_type') == 'batch_delete':
        state['stage'] = stage
    return state


def execute(gateway, journal, kind, record, owner, command, expected=None, colors=None):
    """Called on the calendar owner thread. Keep the existing adapter receipt formats."""
    identifier = record['operation_id']
    before = {'status': record['status'], 'revision': record.get('revision')}
    deleting = expected is None
    batch_delete = deleting and record['preview'].get('object_type') == 'batch_delete'
    if batch_delete:
        before['stage'] = record.get('stage')
        authorized = record['status'] == 'pending' and (
            before['stage'] == 'awaiting_final_confirmation' or
            before['stage'] == SPECIFIC_DELETE_STAGE and 1 <= len(deletion_ids(record)) <= 5)
    else:
        authorized = record['status'] == ('confirmed' if deleting else 'ready')
    if not authorized:
        raise ChatError('operation_conflict', '计划尚未取得执行授权。')
    require_calendar_write()
    record.update(status='executing', expected_colors=colors, execution_protocol=1)
    if not deleting:
        entries = getattr(command, 'entries', (command,))
        record['expected_reminder_rules'] = [entry.after.rule() for entry in entries]
    if batch_delete:
        record['stage'] = 'executing'
    try:
        journal.transition(kind, identifier, owner, before, record)
    except ChatError as exc:
        if exc.code == 'operation_conflict':
            raise ChatError('claim_conflict', '已有执行器领取当前计划。') from exc
        raise
    try:
        require_calendar_write()
    except ChatError as error:
        record.update(status='failed', error_code=error.code, error_message=str(error))
        if batch_delete:
            record['stage'] = 'failed'
        journal.put(kind, identifier, owner, record)
        raise
    try:
        outcome = gateway.events.commit(command, operation_id=identifier)
    except EventOperationError as exc:
        if deleting:
            record.update(deletion_outcome(gateway,record,commit_returned=None if exc.code=='execution_uncertain' else False))
            journal.put(kind,identifier,owner,record)
            return None
        raise ChatError(exc.code, str(exc)) from exc
    if deleting:
        if outcome.changed:
            gateway.replan_after_commit(deletion_ids(record))
        record.update(deletion_outcome(gateway,record,commit_returned=outcome.changed))
        journal.put(kind,identifier,owner,record)
        return outcome
    if outcome.changed:
        gateway.replan_after_commit(item['id'] for item in expected)
    details = observe(gateway, expected, colors, record['expected_reminder_rules'])
    result = event_result(record,[item['id'] for item in expected],changed=outcome.changed,
                          committed=outcome.changed,verified=details is not None,details=details or [])
    result_key = 'result' if kind == 'task' else 'operation_result'
    record[result_key] = result
    if details is None:
        record.update(status='committed_unverified' if outcome.changed else 'failed',error_code='effect_not_verified',
                      error_message='操作已提交，但目标效果核验未通过。' if outcome.changed else '目标效果核验未通过，本次未写入。')
    else:
        record['status'] = 'completed' if kind == 'task' else 'succeeded'
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
        return observe(gateway, expected, colors, record.get('expected_reminder_rules')) is not None
    return all((event := gateway.store.event_by_id(item['id'])) is not None and asdict(event) == item
               for item in expected)


def resolve_target(gateway, target, chosen=None, *, exact_title_without_date=False):
    """Resolve and recheck candidates without accessing chat state."""
    if 'start' not in target and not exact_title_without_date:
        return [], '请给出要操作的开始和结束日期。'
    bounds = {key:target[key] for key in ('start','end') if key in target}
    result = (gateway.delete_candidates(bounds,target['titles'] or None) if bounds else
              gateway.target_candidates(bounds,target['titles'],exact_title_without_date=True))
    rows = result['events']
    if not rows or result['truncated'] or (target['quantity']=='all_matches' and len(rows)>10):
        return [], '该范围内没有匹配事项。' if not rows else '匹配项过多，请缩小范围。'
    missing = set(target['titles']) - {row['title'].strip() for row in rows}
    if missing:
        return [], '未找到：'+'、'.join(sorted(missing))
    if chosen:
        rows = [row for row in rows if row['id']==chosen]
        if not rows:
            fail('选择的事项已不符合原条件，请重新查询。','operation_conflict')
    return rows, None


def compile_target_members(gateway, plan, rows):
    """Build the existing internal Event command from resolved rows."""
    items = []
    for row in rows:
        item = dict(event_id=row['id'])
        if plan['action']=='complete':
            item['done'] = plan['done']
        else:
            item['fields'] = dict(plan.get('fields',{}))
            if plan.get('color_policy'):
                palette = gateway.categories()['palette'].values()
                item['fields'].update(color=secrets.choice([c for c in palette if c.lower()!=row['color'].lower()]),
                                      color_mode='override')
        items.append(item)
    return compile_proposal(dict(action=plan['action'],items=items))
