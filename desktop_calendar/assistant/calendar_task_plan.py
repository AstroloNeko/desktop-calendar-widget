"""Bounded durable Event tasks; no independent storage or command history."""
from dataclasses import asdict
import secrets
import uuid
import re
from datetime import date, timedelta

from calendar_core import COLORS
from desktop_calendar.assistant.calendar_chat_tools import fail, ChatError
from event_input import build_event_candidate
from event_commands import BatchPreview
from calendar_event_service import EventOperationError
from desktop_calendar.assistant.calendar_chat_attributes import adapt_attributes


TASK_TOOLS = {'batch_create_events','batch_update_events','batch_set_completion'}


def run_task(service, name, args, session, owner, request_id, source, message):
    gateway, journal = service.gateway, service.journal
    args=dict(args)
    if re.search(r'不同颜色|三种颜色|各换一种|随机.*颜色',message):
        args['distinct_colors']=True
    if not any(word in message for word in ('各','每天','全部','所有','那三个','那一批','这三个','批量','前三个','前面那','三天')):
        fail('请明确是否操作多个独立事项。','clarification_required')
    if name=='batch_create_events' and '连续' in message and not re.search(r'各|每天.*一次|独立',message):
        fail('连续多日与多个独立事项不同，请明确是否每天各建一个。','clarification_required')
    if not service.auto_execute or any(word in message for word in ('先预览','不要执行','先别执行','先确认')):
        fail('批量任务暂不支持仅预览，请明确是否现在执行。','clarification_required')
    if session.get('pending') and service._pending_status(session, owner) == 'pending':
        fail('请先确认或取消当前操作。','confirmation_required')
    task_id = secrets.token_urlsafe(24)
    task = dict(task_id=task_id, operation_id=task_id, session_id=session['session_id'], request_id=request_id,
        operation=name, status='ready', source=source, constraints=args, clarified={}, result=None)
    if session.get('active_task'):
        previous=journal.get('task',session['active_task'],owner)
        if previous['status']=='needs_clarification' and previous['operation']==name:
            task={**previous,'status':'ready','clarified':args,'attempt_request_id':request_id}
            args={**previous['constraints'],**args}
            task['constraints']=args
            task_id=task['task_id']
        elif previous['status']=='executing':
            fail('上一批操作结果待核查，请先核对，不能自动再执行。','execution_uncertain')
    session['active_task']=task_id
    known = {e['id'] for e in session.get('candidates', [])} | set(session.get('recent_task',{}).get('event_ids',[]))
    if session.get('selected_kind','event') == 'event' and session.get('selected'):
        known.add(session['selected'])
    def prepare():
        if not gateway.writable():
            fail('请先关闭编辑器或等待当前操作完成。','execution_failed')
        entries = []
        items = args.get('items') or [{'event_id':i,'changes':{'done':args['done']}} for i in args['event_ids']]
        if name=='batch_create_events' and re.search(r'(往后|未来|往后面)三天',message) and re.search(r'各|每天',message):
            expected=[(date.today()+timedelta(days=i)).isoformat() for i in (1,2,3)]
            if sorted(item['date'] for item in items)!=expected:
                fail('未来三天应从明天开始，每天各一个，请重新规划日期。','clarification_required')
        colors = None
        if args.get('distinct_colors'):
            if len(items)>len(COLORS):
                fail('当前调色板不足以提供这么多种不同颜色。','clarification_required')
            colors = secrets.SystemRandom().sample(list(COLORS.values()),len(items))
        ids = []
        for index, item in enumerate(items):
            creating = name == 'batch_create_events'
            identifier = str(uuid.uuid4()) if creating else item['event_id']
            if not creating and identifier not in known:
                fail('请先查询并明确批量目标，不能猜测ID。','clarification_required')
            current = None if creating else gateway.store.event_by_id(identifier)
            if not creating and current is None:
                fail('批量目标已不存在。','event_not_found')
            values = dict(item if creating else item['changes'])
            if name != 'batch_set_completion':
                values, _, _ = adapt_attributes(values,message,creating,gateway.categories()['categories'],())
            values.pop('category_basis',None)
            if colors:
                values.update(color=colors[index],color_mode='override')
            candidate = build_event_candidate(values,current=current,identifier=identifier,context=gateway.store)
            entries.append(gateway.events.preview_upsert(candidate))
            ids.append(identifier)
        if len(ids)!=len(set(ids)):
            fail('同一批次不能重复修改同一事项。')
        task['event_ids'] = ids
        task['expected'] = [asdict(e.event()) for e in entries]
        return gateway.events.preview_create_many([e.event() for e in entries]) if name=='batch_create_events' else BatchPreview(tuple(entries),'AI批量修改事项')
    def execute():
        preview = prepare()
        journal.put('task',task_id,owner,task)
        task['status']='executing'
        journal.put('task',task_id,owner,task)
        journal.put('audit',task_id,owner,dict(task_id=task_id,session_id=task['session_id'],request_id=request_id,
            source=source,action=name,status='executing',event_ids=task['event_ids']))
        # Any uncertain exception after this point forbids blind replay.
        result = gateway.events.commit(preview)
        actual = [gateway._detail(gateway.store.event_by_id(i)) for i in task['event_ids']]
        task.update(status='completed',result=dict(task_id=task_id,events=actual,event_ids=task['event_ids'],changed=result.changed))
        journal.put('task',task_id,owner,task)
        return result
    try:
        result = gateway.dispatch(execute)
    except Exception as error:
        # Keep executing durable intent if commit/result publication is uncertain.
        if task['status']=='ready' or isinstance(error,EventOperationError):
            task.update(status='needs_clarification' if isinstance(error,ChatError) and error.code=='clarification_required' else 'failed',error_code=getattr(error,'code','validation_failed'))
            journal.put('task',task_id,owner,task)
            if isinstance(error,ChatError):
                raise
            fail('批量操作未保存，请检查输入后重试。','execution_failed')
        fail('批量操作结果待核查，请勿重复创建。','execution_uncertain')
    session['recent_task'] = dict(task_id=task_id,event_ids=task['event_ids'],events=task['result']['events'])
    session['selected_kind']='event'
    session['selected']=task['event_ids'][-1]
    session['candidates']=task['result']['events']
    try:
        journal.put('audit',task_id,owner,dict(task_id=task_id,session_id=task['session_id'],request_id=request_id,
            source=source,action=name,status='completed',event_ids=task['event_ids']))
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


def recover_task(service, task, owner):
    """Observe only. An uncertain task is never resubmitted automatically."""
    if task['status']=='executing':
        matches = service.gateway.dispatch(lambda: all(
            (event := service.gateway.store.event_by_id(item['id'])) is not None and asdict(event)==item
            for item in task['expected']))
        if matches:
            events=service.gateway.dispatch(lambda: [service.gateway.detail(i) for i in task['event_ids']])
            task.update(status='completed',result=dict(task_id=task['task_id'],event_ids=task['event_ids'],events=events,recovered=True))
            service.journal.put('task',task['task_id'],owner,task)
        else:
            task['error_code']='execution_uncertain'
    return task
