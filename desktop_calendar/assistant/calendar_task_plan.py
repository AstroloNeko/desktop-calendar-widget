"""Bounded durable Event tasks; no independent storage or command history."""
from dataclasses import asdict, replace
import secrets
import uuid
import re
from datetime import date, timedelta

from desktop_calendar.assistant.operation_engine import event_result, execute as execute_plan, recovered
from desktop_calendar.assistant.operation_plan import member_count
from calendar_core import COLORS
from desktop_calendar.assistant.calendar_chat_protocol import ChatError, fail, fingerprint
from desktop_calendar.assistant.calendar_chat_tools import TASK_TOOLS as TASK_TOOLS, reference_today
from event_input import build_event_candidate
from event_commands import BatchPreview
from calendar_event_service import EventOperationError
from desktop_calendar.assistant.calendar_chat_attributes import adapt_attributes


def run_task(service, name, args, session, owner, request_id, source, message, *, structured=False, preview_only=False):
    gateway, journal = service.gateway, service.journal
    args=dict(args)
    if re.search(r'不同颜色|三种颜色|各换一种|随机.*颜色',message):
        args['distinct_colors']=True
    if not structured and not member_count(message) and not any(word in message for word in ('各','每天','全部','所有','那三个','那一批','这三个','批量','前三个','前面那','三天')):
        fail('请明确是否操作多个独立事项。','clarification_required')
    if not structured and name=='batch_create_events' and '连续' in message and not re.search(r'各|每天.*一次|独立',message):
        fail('连续多日与多个独立事项不同，请明确是否每天各建一个。','clarification_required')
    preview_only = preview_only or not service.auto_execute or any(word in message for word in ('先预览','不要执行','先别执行','先确认'))
    if session.get('pending') and service._pending_status(session, owner) in ('pending','ready','executing','uncertain'):
        fail('请先确认或取消当前操作。','confirmation_required')
    task_id = secrets.token_urlsafe(24)
    task = dict(task_id=task_id, operation_id=task_id, session_id=session['session_id'], request_id=request_id,
        operation=name, status='ready', expires=service.clock()+900, source=source, constraints=args, clarified={}, result=None)
    if session.get('active_task'):
        previous=journal.get('task',session['active_task'],owner)
        if not structured and previous['status']=='needs_clarification' and previous['operation']==name:
            task={**previous,'status':'ready','clarified':args,'attempt_request_id':request_id}
            args={**previous['constraints'],**args}
            task['constraints']=args
            task_id=task['task_id']
        elif previous['status'] in ('executing','uncertain'):
            fail('上一批操作结果待核查，请先核对，不能自动再执行。','execution_uncertain')
    session['active_task']=task_id
    known = {e['id'] for e in session.get('candidates', [])} | set(session.get('recent_task',{}).get('event_ids',[]))
    if session.get('selected_kind','event') == 'event' and session.get('selected'):
        known.add(session['selected'])
    def prepare():
        if not gateway.writable():
            fail('请先关闭编辑器或等待当前操作完成。','execution_failed')
        candidates = []
        fixed_colors = []
        items = args.get('items') or [{'event_id':i,'changes':{'done':args['done']}} for i in args['event_ids']]
        if name=='batch_create_events' and re.search(r'(往后|未来|往后面)三天',message) and re.search(r'各|每天',message):
            expected=[(reference_today(date.today)+timedelta(days=i)).isoformat() for i in (1,2,3)]
            if sorted(item['date'] for item in items)!=expected:
                fail('未来三天应从明天开始，每天各一个，请重新规划日期。','clarification_required')
        ids = []
        for item in items:
            creating = name == 'batch_create_events'
            identifier = str(uuid.uuid4()) if creating else item['event_id']
            if not creating and identifier not in known:
                fail('请先查询并明确批量目标，不能猜测ID。','clarification_required')
            current = None if creating else gateway.store.event_by_id(identifier)
            if not creating and current is None:
                fail('批量目标已不存在。','event_not_found')
            values = dict(item if creating else item['changes'])
            if not structured and name != 'batch_set_completion':
                values, _, _ = adapt_attributes(values,message,creating,gateway.categories()['categories'],())
            values.pop('category_basis',None)
            candidate = build_event_candidate(values,current=current,identifier=identifier,context=gateway.store)
            candidates.append(candidate)
            fixed_colors.append(structured and ('color' in values or 'color_mode' in values))
            ids.append(identifier)
        if len(ids)!=len(set(ids)):
            fail('同一批次不能重复修改同一事项。')
        if args.get('distinct_colors'):
            reserved = [gateway.store.effective_event_color(c).casefold() for c, fixed in zip(candidates,fixed_colors) if fixed]
            available = [color for color in COLORS.values() if color.casefold() not in reserved]
            needed = len(candidates)-len(reserved)
            if len(set(reserved)) != len(reserved) or needed > len(available):
                fail('无法同时保留指定颜色并为每项分配不同颜色。','clarification_required')
            colors = iter(secrets.SystemRandom().sample(available,needed))
            candidates = [c if fixed else replace(c,color=next(colors),color_mode='override')
                          for c, fixed in zip(candidates,fixed_colors)]
        preview = (gateway.events.preview_create_many(candidates) if name=='batch_create_events'
                   else BatchPreview(tuple(gateway.events.preview_upsert(c) for c in candidates),'AI批量修改事项'))
        task['event_ids'] = ids
        task['before_hashes'] = [fingerprint(gateway._state(i)) for i in ids]
        task['expected'] = [asdict(entry.event()) for entry in preview.entries]
        return preview
    def execute():
        preview = prepare()
        if preview_only:
            entries = preview.entries
            batch = dict(object_type='event_batch', creation=name=='batch_create_events', description='批量事项操作', event_id=task['event_ids'][0],
                         action={'batch_create_events':'create','batch_set_completion':'complete'}.get(name,'update'),
                         event_ids=task['event_ids'], after=task['expected'], before=None,
                         before_hashes=task['before_hashes'],
                         summaries=[gateway._detail(entry.event()) for entry in entries])
            op = dict(operation_id=task_id, session_id=session['session_id'], request_id=request_id,
                      preview=batch, status='pending', requires_confirmation=True, revision=secrets.token_urlsafe(24),
                      expires=service.clock()+900)
            journal.put('operation',task_id,owner,op)
            session['pending']=task_id
            session.pop('active_task',None)
            return op
        journal.put('task',task_id,owner,task)
        journal.put('session',session['session_id'],owner,session)
        colors = [gateway.store.effective_event_color(entry.event()) for entry in preview.entries]
        return execute_plan(gateway, journal, 'task', task, owner, preview, task['expected'], colors)
    try:
        result = gateway.dispatch(execute)
    except Exception as error:
        # Keep executing durable intent if commit/result publication is uncertain.
        if getattr(error,'code',None) == 'claim_conflict':
            raise
        if task['status']=='ready' or isinstance(error,(ChatError,EventOperationError)) and error.code != 'execution_uncertain':
            task.update(status='needs_clarification' if isinstance(error,ChatError) and error.code=='clarification_required' else 'failed',error_code=getattr(error,'code','validation_failed'))
            journal.put('task',task_id,owner,task)
            if isinstance(error,ChatError):
                raise
            fail('批量操作未保存，请检查输入后重试。','execution_failed')
        fail('批量操作结果待核查，请勿重复创建。','execution_uncertain')
    if preview_only:
        return result
    if task['status']=='completed':
        session['recent_task'] = dict(task_id=task_id,event_ids=task['event_ids'],events=task['result']['events'])
        session['selected_kind']='event'
        session['selected']=task['event_ids'][-1]
        session['candidates']=task['result']['events']
    try:
        journal.put('audit',task_id,owner,dict(task_id=task_id,session_id=task['session_id'],request_id=request_id,
            source=source,action=name,status=task['status'],event_ids=task['event_ids']))
    except OSError:
        # Durable intent and completed task receipt already exist. Do not claim
        # the calendar was unchanged or retry a committed batch over audit I/O.
        task['audit_pending']=True
    # Receipt first: refresh failure must not turn success into another execution.
    if result.change:
        try:
            gateway.dispatch(lambda: gateway.refresh(result.change))
        except Exception:
            pass
    return task


def recover_task(service, task, owner, *, resume_ready=True):
    """Resume authorized ready plans; reconcile executing plans by observation only."""
    if task['status']=='ready' and service.clock() > task.get('expires', float('inf')):
        task.update(status='expired', error_code='operation_expired')
        service.journal.transition('task', task['task_id'], owner, {'status':'ready'}, task)
    if resume_ready and task['status']=='ready' and 'before_hashes' in task:
        def resume():
            gateway = service.gateway
            if not gateway.writable():
                return
            if [fingerprint(gateway._state(i)) for i in task['event_ids']] != task['before_hashes']:
                task.update(status='failed', error_code='operation_conflict')
                service.journal.transition('task', task['task_id'], owner, {'status':'ready'}, task)
                return
            from calendar_core import Event
            entries = tuple(gateway.events.preview_upsert(Event(**snapshot)) for snapshot in task['expected'])
            execute_plan(gateway, service.journal, 'task', task, owner, BatchPreview(entries,'AI批量事项'),
                         task['expected'], [gateway.store.effective_event_color(e.event()) for e in entries])
        service.gateway.dispatch(resume)
    if task['status'] in ('executing','uncertain'):
        matches = service.gateway.dispatch(lambda: recovered(service.gateway, task, task['expected']))
        if matches:
            events=service.gateway.dispatch(lambda: [service.gateway.detail(i) for i in task['event_ids']])
            task.update(status='completed',result=event_result(task,task['event_ids'],changed=True,committed=True,
                                                               verified=True,details=events,recovered=True))
            service.journal.put('task',task['task_id'],owner,task)
        else:
            task.update(status='uncertain', error_code='execution_uncertain')
            service.journal.put('task',task['task_id'],owner,task)
    return task
