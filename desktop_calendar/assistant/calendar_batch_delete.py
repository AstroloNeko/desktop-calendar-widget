"""Two-stage reviewed deletion. All calendar access stays on gateway dispatcher."""
import re
import secrets
from datetime import date

from desktop_calendar.assistant.calendar_chat_tools import fail, ChatError
from desktop_calendar.assistant.calendar_action_gateway import fingerprint
from calendar_event_service import EventOperationError


_ENDPOINT = r'(?:(?:(\d{4})[-年/])?(\d{1,2})[-月/])?(\d{1,2})(?:日|号)?'
_RANGE = re.compile(r'(?<![\d/-])' + _ENDPOINT + r'\s*(?:到|至|[-~～—])\s*' + _ENDPOINT + r'(?![\d/-])')
_COMMAND = re.compile(r'(?:全部|全都|都|全)?(?:删除|删掉|删了|移除)(?:掉|了)?[。！!\s]*$')
_ALL = {'事项','事情','安排','日程','任务','不管是什么事情','不管是什么事项','不管什么事情'}


def _interval(match, today):
    year, month, day, end_year, end_month, end_day = match.groups()
    start = date(int(year or today.year), int(month or today.month), int(day))
    end = date(int(end_year or start.year), int(end_month or start.month), int(end_day))
    if not 0 <= (end-start).days <= 366:
        raise ValueError()
    return dict(start=start.isoformat(), end=end.isoformat())


def _titles(subject):
    """An empty list explicitly means every Event; None means still ambiguous."""
    subject = re.sub(r'^(?:(?:请|帮我|把|将|这几天|这些天|这?[一二三四五六七八九十\d]+天|所有的?|全部的?|的)\s*)+', '', subject)
    subject = subject.strip(' ，,。')
    if subject in _ALL:
        return []
    if not subject or subject in ('实验安排','实验','全部','所有'):
        return None
    parts = re.split(r'(?:[，,、]\s*(?:还有|以及)?|还有|以及)', subject)
    names = [part.strip(' \t"\'“”‘’「」') for part in parts]
    return list(dict.fromkeys(names)) if all(names) else None


def handle_message(service, session, message, owner, request_id):
    from desktop_calendar.assistant.calendar_chat_service import envelope

    def reply(text, status='clarification_required'):
        return envelope(session['session_id'], request_id, status=status, assistant_message=text)

    text = message.strip()
    active = session.get('batch_delete_intent')
    all_answer = active and text in ('全部事项','所有事项','不管是什么事情')
    bounds = session.get('delete_range') or session.get('clarified_query') or {}
    if text in ('全部','全选','所有','全部删除','全删') and session.get('delete_intent'):
        scope = session.get('delete_scope')
        if scope and scope['ids']:
            session['batch_delete_intent'] = True
            return prepare(service,session,scope['ids'],owner,request_id)
        if active and session.get('delete_title') is None and session.get('delete_range'):
            all_answer = True
        else:
            return reply('请明确日期范围，以及要删除全部事项还是哪些标题。')
    command = _COMMAND.search(text)
    interval = _RANGE.search(text)
    date_answer = active and interval and interval.group() == text
    named_answer = None
    if active and session.get('delete_title') is None and session.get('delete_range') and not (interval or command or all_answer):
        names = _titles(text)
        if names and service.gateway.delete_candidates(bounds,names)['total']:
            named_answer = names
    if not (date_answer or named_answer or all_answer or (command and (active or re.search(r'全|所有|都删', text)))):
        return None

    pending = service._operation(session['session_id'],session['pending'],owner) if session.get('pending') else None
    def discard_preview():
        if pending and pending['status']=='pending' and pending['preview'].get('object_type')=='batch_delete':
            service.cancel_operation(session['session_id'],pending['operation_id'],owner)
            session['pending'] = None
        session.pop('delete_scope',None)

    session.update(batch_delete_intent=True, delete_intent=True)
    old_filters = (session.get('delete_scope') or {}).get('filters')
    if interval:
        try:
            bounds = _interval(interval,date.today())
            if len(list(_RANGE.finditer(text))) != 1:
                raise ValueError()
        except ValueError:
            discard_preview()
            session.pop('delete_range',None)
            session.pop('clarified_query',None)
            return reply('日期范围无效，请给出一个有效的开始和结束日期。')
    if named_answer:
        session['delete_title'] = named_answer
    elif all_answer:
        session['delete_title'] = []
    elif command:
        subject = text[:command.start()]
        if interval:
            subject = subject[:interval.start()] + subject[interval.end():]
        if re.search(r'除了?|保留|别删|不要删|不删', subject):
            discard_preview()
            session['delete_title'] = None
            return reply('请列出要删除的标题，或取消当前操作。')
        subject = re.sub(r'^(?:不要精确匹配标题|拜托|算了)[，,]\s*', '', subject.strip())
        subject = subject.removeprefix('我的意思是').strip()
        # A conversational aside before a named, dated subject is outside the title.
        subject = re.sub(r'^[^，,]{1,6}[吧把][，,]\s*(?=这几天[^，,]*实验)', '', subject)
        if subject.strip(' ，,') or not active:
            session['delete_title'] = _titles(subject)
    titles = session.get('delete_title')
    if isinstance(titles,str):  # Existing sessions stored a single title.
        titles = [titles]
    if not bounds and '这几天' in text and session.get('recent_task'):
        days = [e['date'] for e in session['recent_task']['events']]
        if days:
            bounds = dict(start=min(days),end=max(days))
    if 'start' not in bounds or 'end' not in bounds:
        discard_preview()
        return reply('请给出开始和结束日期，例如 11-18 到 11-20。')
    bounds = {key:bounds[key] for key in ('start','end')}
    session['delete_range'] = bounds
    label = bounds['start']+' 至 '+bounds['end']
    if titles is None:
        discard_preview()
        return reply(label+'：要删除这段时间的全部事项，还是只删某些实验？请指定标题或回复“全部事项全删”。')
    filters = dict(**bounds,titles=titles)
    if filters != old_filters:
        discard_preview()
    result = service.gateway.delete_candidates(bounds,titles or None)
    if pending and session.get('pending') and old_filters == filters and (
            pending['preview'].get('event_ids') != [e['id'] for e in result['events']]):
        discard_preview()
    if not result['total']:
        session.pop('delete_scope',None)
        return reply(label+' 没有'+('标题为「'+'」「'.join(titles)+'」的' if titles else '')+'事项。','ok')
    if result['total'] > 10:
        session.pop('delete_scope',None)
        return reply(f"{label} 共匹配 {result['total']} 项，超过每批 10 项的上限，请缩小日期或标题范围。",'failed')
    found_titles = {item['title'].strip() for item in result['events']}
    missing = [title for title in titles if title not in found_titles]
    if missing:
        session.pop('delete_scope',None)
        return reply(label+' 未找到「'+'」「'.join(missing)+'」。请核对标题，或明确只删除已匹配的标题。')
    service._bind_delete_scope(session,result,filters)
    result = prepare(service,session,session['delete_scope']['ids'],owner,request_id)
    description = '全部事项' if not titles else '标题为「'+'」「'.join(titles)+'」的事项'
    result['assistant_message'] = f'{label}，{description}，共 {len(session["delete_scope"]["ids"])} 项。' + result['assistant_message']
    return result


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
