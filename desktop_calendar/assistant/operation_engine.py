"""Execute an authorized Event preview once, then verify the stored postconditions."""
from dataclasses import asdict

from calendar_event_service import EventOperationError
from desktop_calendar.assistant.calendar_chat_tools import ChatError


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
