"""Two-stage reviewed deletion. All calendar access stays on gateway dispatcher."""
import secrets

from desktop_calendar.assistant.calendar_chat_tools import fail, ChatError
from calendar_action_gateway import fingerprint
from calendar_event_service import EventOperationError


def prepare(service, session, ids, owner, request_id):
    scope=session.get('delete_scope')
    if scope and (scope.get('truncated') or scope.get('total',0)>10):
        fail('当前实际匹配'+str(scope['total'])+'项，超过批量删除上限10项；请缩小日期或标题范围，不会截断或自动拆批删除。','clarification_required')
    if not scope or scope.get('truncated') or scope.get('total') != len(scope['ids']):
        fail('没有完整的待删除清单，请先明确范围并重新查询。','clarification_required')
    if not 1 <= len(ids) <= 10 or len(ids)!=len(set(ids)) or set(ids)!=set(scope['ids']):
        fail('批量删除必须完整绑定当前清单的1至10个独立ID，请缩小范围或重新查询。','clarification_required')
    if service.clock()>scope['expires']:
        fail('候选清单已过期，请重新查询。','operation_expired')
    pending=session.get('pending')
    if pending:
        old=service._operation(session['session_id'],pending,owner)
        if old['status']=='pending':
            if old['preview'].get('object_type')=='batch_delete' and old['preview']['event_ids']==ids:
                return service.get_operation_result(session['session_id'],pending,owner)
            fail('请先确认或取消当前操作。','confirmation_required')
    def capture():
        states=[service.gateway._state(i) for i in ids]
        if any(s is None for s in states) or [fingerprint(s) for s in states]!=[scope['hashes'][i] for i in ids]:
            fail('候选事项已变化，请重新查询并核对清单。','operation_conflict')
        return [service.gateway.detail(i) for i in ids]
    rows=service.gateway.dispatch(capture)
    op=dict(operation_id=secrets.token_urlsafe(24),session_id=session['session_id'],request_id=request_id,
        status='pending',stage='reviewing_batch',revision=secrets.token_urlsafe(24),expires=service.clock()+900,
        requires_confirmation=True,preview=dict(object_type='batch_delete',event_id=ids[0],event_ids=ids,
        before=rows,after=None,hashes=scope['hashes'],dates=sorted({r['date'] for r in rows}),
        description='批量删除'+str(len(ids))+'个事项',clarification_id=scope['clarification_id']))
    service.journal.put('operation',op['operation_id'],owner,op)
    session['pending']=op['operation_id']
    service.journal.put('session',session['session_id'],owner,session)
    service._audit(op,owner,'batch-delete-review')
    return response(op)


def response(op):
    message='请核对完整清单，先选择“删除这N项”，再进行最终确认。' if op['stage']=='reviewing_batch' else '请再次核对清单，然后点击“最终确认删除”。'
    if op['status']=='succeeded':
        message='✓ 已实际删除'+str(len(op['preview']['event_ids']))+'项，可一次撤销。'
    elif op['status']!='pending':
        message=op.get('error_message') or '批量删除已终止。'
    return dict(session_id=op['session_id'],request_id=op['request_id'],status=op['status'],pending_operation=op,
        assistant_message=message,
        operation_result=op.get('operation_result'),error_code=op.get('error_code'),error_message=op.get('error_message'))


def confirm(service, op, owner, revision, automatic):
    if op.get('stage') not in ('reviewing_batch','awaiting_final_confirmation'):
        fail('删除确认阶段无效，请重新生成清单。','operation_conflict')
    if automatic or revision is None or revision!=op['revision']:
        fail('批量删除必须使用当前清单上的确认按钮，不能跳过两级确认。','operation_conflict')
    def execute():
        if not service.gateway.writable():
            fail('请先关闭编辑器或等待当前操作完成。','execution_failed')
        ids=op['preview']['event_ids']
        if any(fingerprint(service.gateway._state(i))!=op['preview']['hashes'][i] for i in ids):
            op.update(status='failed',stage='conflict',error_code='operation_conflict',error_message='清单中的事项已变化，整批未删除。请重新查询、核对并确认。')
            service.journal.put('operation',op['operation_id'],owner,op)
            return None
        if op['stage']=='reviewing_batch':
            op.update(stage='awaiting_final_confirmation',revision=secrets.token_urlsafe(24))
            service.journal.put('operation',op['operation_id'],owner,op)
            return None
        preview=service.gateway.events.preview_bulk(ids,'delete')
        op.update(status='executing',stage='executing')
        service.journal.put('operation',op['operation_id'],owner,op)
        try:
            result=service.gateway.events.commit(preview)
        except EventOperationError as exc:
            op.update(status='failed',stage='failed',error_code=exc.code,error_message=str(exc))
            service.journal.put('operation',op['operation_id'],owner,op)
            return None
        op.update(status='succeeded',stage='completed',operation_result=dict(deleted=True,event_ids=ids,count=len(ids),operation_id=op['operation_id']))
        service.journal.put('operation',op['operation_id'],owner,op)
        return result.change
    try:
        change=service.gateway.dispatch(execute)
    except ChatError:
        raise
    except Exception:
        return dict(session_id=op['session_id'],status='failed',error_code='execution_uncertain',
            error_message='无法确认批量删除结果，请查询操作状态；不会自动重试删除。')
    try:
        service._audit(op,owner,'batch-delete-confirm')
    except OSError:
        if op['status']!='succeeded':
            raise
        op['audit_pending']=True
    if op['status']=='succeeded':
        session=service.get_session(op['session_id'],owner)
        for key in ('batch_delete_intent','delete_title','delete_range','delete_scope','delete_intent'):
            session.pop(key,None)
        service.journal.put('session',op['session_id'],owner,session)
    if change:
        try:
            service.gateway.dispatch(lambda: service.gateway.refresh(change))
        except Exception:
            pass  # durable success precedes UI, never replay a committed deletion
    return response(op)
