"""Sole AI calendar boundary. All Store access runs through the supplied dispatcher."""
from dataclasses import asdict, replace
from datetime import date, datetime
import hashlib
import json
from tk_dispatcher import TkDispatcher  # Compatibility export; implementation is desktop-only.

from calendar_core import Event, COLORS
from event_input import build_event_candidate, EventInputError
from calendar_event_service import CalendarEventService, OperationContext
from event_commands import CommandConflictError
from event_search import EventSearchIndex, capture_search_store
from calendar_chat_tools import ChatError, iso_day, fail, validate_call


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class CalendarActionGateway:
    def __init__(self, store, commands, dispatch=lambda fn: fn(), refresh=lambda change: None, writable=lambda: True, habit_refresh=lambda: None):
        self.store, self.commands, self.dispatch, self.refresh, self.writable = store, commands, dispatch, refresh, writable
        self.habit_refresh = habit_refresh
        self.repository = store.repository
        self.events = CalendarEventService(commands, OperationContext(source='calendar-chat'))

    def query(self, arguments):
        validate_call('query_events', arguments)
        return self.dispatch(lambda: self._query(arguments))

    def categories(self):
        return self.dispatch(lambda: {'categories': [{'id': category.id, 'name': category.name, 'color': category.color}
                              for category in self.repository.list_categories()[:100]], 'palette': dict(COLORS)})

    def query_delete_targets(self,args):
        """Exact title + explicit date interval, never a fuzzy search for deletion."""
        validate_call('query_events',args)
        title=args.get('keyword','').strip(' \t\r\n\"\'“”‘’「」')
        if not title or title in ('全部','所有') or not all(k in args for k in ('start','end')):
            fail('请提供精确标题和起止日期。','clarification_required')
        start,end=iso_day(args['start']),iso_day(args['end'])
        if end<start or (end-start).days>366:
            fail('删除查询范围应为0至366天。','clarification_required')
        def read():
            found=[self._detail(e) for e in self.repository.list_events() if e.title.strip()==title
                and ('done' not in args or e.done==args['done'])
                and ('category_id' not in args or e.category_id==args['category_id'])
                and any(start<=d<=end for d in self.store.event_dates(e))]
            found.sort(key=lambda e:(e['date'],e['id']))
            return dict(events=found[:30],total=len(found),truncated=len(found)>30)
        return self.dispatch(read)

    def query_habits(self, args):
        validate_call('query_habits', args)
        def read():
            items = [r for r in self.repository.list_routines() if r.kind == 'habit' and args.get('keyword', '').casefold() in r.title.casefold()]
            return dict(habits=[self._habit_detail(r) for r in items[:30]], total=len(items))
        return self.dispatch(read)

    def _habit_detail(self, item):
        from habit_schedule import schedule_label
        return dict(id=item.id, object_type='habit', schedule=schedule_label(item),
                    **{k: v for k, v in asdict(item).items() if k not in ('id', 'habit_done', 'schedule_history', 'completed_on')})

    def habit_detail(self, identifier):
        def read():
            item = next((r for r in self.store.routines if r.id == identifier and r.kind == 'habit'), None)
            if item is None:
                fail('习惯不存在或已删除。', 'habit_not_found')
            return self._habit_detail(item)
        return self.dispatch(read)

    def _query(self, args):
        today = date.today()
        start = iso_day(args.get('start', today.replace(day=1).isoformat()))
        end = iso_day(args.get('end', start.isoformat()))
        global_keyword = bool(args.get('keyword')) and 'start' not in args and 'end' not in args
        if not args.get('start') and not args.get('end'):
            import calendar
            end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        if end < start or (end-start).days > 366:
            fail('查询范围应为0至366天。')
        store = self.store
        matches = None
        if args.get('keyword'):
            index = EventSearchIndex.from_store(capture_search_store(store))
            matches = {result.key for result in index.query(args['keyword'], limit=len(store.events)) if result.kind == 'event'}
        found = []
        for event in self.repository.list_events():
            if matches is not None and event.id not in matches:
                continue
            if 'done' in args and event.done != args['done']:
                continue
            if 'category_id' in args and event.category_id != args['category_id']:
                continue
            days = store.event_dates(event)
            if args.get('ddl_only'):
                if not store.event_has_deadline(event) or (not global_keyword and not start <= days[-1] <= end):
                    continue
            elif not global_keyword and not any(start <= day <= end for day in days):
                continue
            summary = self._detail(event)
            summary.pop('notes', None)  # Fetch a target's notes only via get_event.
            found.append(summary)
        found.sort(key=lambda item: (item['date'], item['id']))
        return {'events': found[:30], 'total': len(found), 'truncated': len(found) > 30}

    def _detail(self, event):
        days = self.store.event_dates(event)
        category = self.store.category_by_id(event.category_id)
        return dict(id=event.id, title=event.title, date=days[0].isoformat(), end=days[-1].isoformat(),
                    time=event.due_at.strftime('%H:%M') if event.has_time else None,
                    event_type=event.event_type, category_id=event.category_id,
                    category=category.name if category else None, done=event.done,
                    reminder=event.reminder, deadline=days[-1].isoformat() if self.store.event_has_deadline(event) else None,
                    duration_days=event.duration_days, skip_non_working_days=event.skip_non_working_days,
                    end_as_ddl=event.end_as_ddl, color=self.store.effective_event_color(event),
                    color_mode=event.color_mode, color_override=event.color, notes=event.notes)

    def detail(self, identifier):
        def read():
            event = self.store.event_by_id(identifier)
            if event is None:
                fail('事项已不存在。', 'event_not_found')
            return self._detail(event)
        return self.dispatch(read)

    def _state(self, identifier):
        event = self.store.event_by_id(identifier)
        if event is None:
            return None
        return {'event': asdict(event), 'dates': [day.isoformat() for day in self.store.event_dates(event)],
                'category': asdict(self.store.category_by_id(event.category_id)) if event.category_id else None,
                'notifications': sorted(key for key in self.store.notified if key.startswith(identifier+':'))}

    def preview(self, name, arguments, event_id, previous=None, *, message=''):
        validate_call(name, arguments)
        return self.dispatch(lambda: self._preview(name, arguments, event_id, previous, message))

    def _preview(self, name, args, identifier, previous, message=''):
        if name in ('preview_create_habit', 'preview_update_habit'):
            from calendar_habit_gateway import prepare_habit
            current = next((r for r in self.store.routines if r.id == identifier and r.kind == 'habit'), None)
            if name == 'preview_update_habit' and current is None:
                fail('习惯不存在或已删除。', 'habit_not_found')
            if previous or name == 'preview_create_habit' and current:
                fail('请先处理当前操作；习惯ID已存在。', 'operation_conflict')
            return prepare_habit(self.store, args if name == 'preview_create_habit' else args['changes'], identifier, message,
                                 current if name == 'preview_update_habit' else None)
        current = self.store.event_by_id(identifier)
        before = self._state(identifier)
        if previous and previous['before_hash'] != fingerprint(before):
            fail('事项或工作日日历已变化，请重新查询。', 'operation_conflict')
        if name != 'preview_create' and current is None and not (previous and previous['before'] is None):
            fail('事项已不存在。', 'event_not_found')
        if name == 'preview_delete':
            if previous:
                fail('请先确认或取消当前操作，再删除。', 'confirmation_required')
            return dict(event_id=identifier, before_hash=fingerprint(before), before=asdict(current), after=None,
                        before_summary=self._detail(current), after_summary=None,
                        dates=[d.isoformat() for d in self.store.event_dates(current)], deadline=None,
                        requested_fields=[], changed_fields=['deleted'], description='删除事项')
        candidate = Event(**previous['after']) if previous else replace(current) if current else replace(
            self.store.make_quick_event(args['title'], iso_day(args['date'])), id=identifier)
        if name == 'preview_complete':
            candidate.done = args['done']
        else:
            patch = args if name == 'preview_create' else args['changes']
            candidate = self._patch(candidate, patch, creating=current is None)
        preview = self.events.preview_upsert(candidate, label='AI新建事项' if current is None else 'AI修改事项')
        return dict(event_id=identifier, before_hash=fingerprint(before),
                    before=asdict(current) if current else None, after=asdict(preview.event()),
                    before_summary=self._detail(current) if current else None, after_summary=self._detail(preview.event()),
                    dates=[day.isoformat() for day in preview.effective_dates],
                    deadline=preview.deadline.isoformat() if preview.deadline else None,
                    requested_fields=sorted((set(previous.get('requested_fields', [])) if previous else set()) |
                        ({'done'} if name == 'preview_complete' else set(patch))),
                    changed_fields=[key for key, value in asdict(preview.event()).items()
                                    if current is None or asdict(current).get(key) != value],
                    description='新建事项' if current is None else '修改事项')

    def _patch(self, candidate, values, *, creating=False):
        values = dict(values)
        values.pop('category_basis', None)
        # Keep the existing AI-specific tolerance for an invented create default.
        # The shared validator itself never accepts category-less inheritance.
        if creating and values.get('color_mode') == 'inherit' and not values.get('category_id', candidate.category_id):
            values.pop('color_mode')
        try:
            return build_event_candidate(values, current=candidate, context=self.store, creating=creating)
        except EventInputError as error:
            field = 'changes.color_mode' if error.field == 'color_mode' and error.clarification else error.field
            fail(str(error), 'clarification_required' if error.clarification else 'invalid_arguments', field)

    def execute(self, operation, journal, owner):
        """Only called after explicit confirmation. Journal intent precedes one command."""
        return self.dispatch(lambda: self._execute(operation, journal, owner))

    def _execute(self, operation, journal, owner):
        if not self.writable():
            fail('请先关闭当前编辑器或等待数据操作结束，再确认。', 'execution_failed')
        preview = operation['preview']
        if preview.get('object_type') == 'habit':
            from calendar_core import RoutineItem
            current = next((r for r in self.store.routines if r.id == preview['habit_id']), None)
            if fingerprint(asdict(current) if current else None) != preview['before_hash']:
                fail('习惯已变化，请重新查询。', 'operation_conflict')
            item = RoutineItem(**preview['after'])
            operation['status'] = 'executing'
            journal.put('operation', operation['operation_id'], owner, operation)
            try:
                from calendar_mutation_service import mutation_service
                change = mutation_service(self.commands).upsert_routine(item).change
            except (OSError, ValueError) as exc:
                if getattr(exc, 'code', None) == 'execution_uncertain':
                    fail('习惯保存结果待核查，请勿重复操作。', 'execution_uncertain')
                fail('习惯保存未成功，未应用此操作。', 'execution_failed')
            result = dict(object_type='habit', habit_id=item.id, action='updated' if current else 'created', habit=dict(id=item.id, title=item.title,
                color=item.color, schedule=preview['schedule'], next_on=preview['next_on']), changed=change is not None)
            operation.update(status='succeeded', operation_result=result)
            journal.put('operation', operation['operation_id'], owner, operation)
            self.habit_refresh()
            return result
        if fingerprint(self._state(preview['event_id'])) != preview['before_hash']:
            fail('事项已被修改，请重新查询和预览。', 'operation_conflict')
        deleting = preview['after'] is None
        if not deleting:
            candidate = Event(**preview['after'])
            command = self.events.preview_upsert(candidate, label='AI确认事项操作')
            if asdict(command.event()) != preview['after'] or [d.isoformat() for d in command.effective_dates] != preview['dates']:
                fail('分类或工作日日历已变化，请重新预览。', 'operation_conflict')
        operation['status'] = 'executing'
        journal.put('operation', operation['operation_id'], owner, operation)
        try:
            outcome = self.events.delete(preview['event_id']) if deleting else self.events.commit(command)
            change = outcome.change
        except (OSError, ValueError) as error:
            raise ChatError('execution_failed', '日历保存未成功，未应用此修改。') from error
        result = dict(event=None if deleting else self._detail(self.store.event_by_id(preview['event_id'])),
                      deleted=deleting, event_id=preview['event_id'], changed=change is not None)
        operation.update(status='succeeded', operation_result=result)
        # If this write fails, durable 'executing' remains; recovery NEVER replays it.
        journal.put('operation', operation['operation_id'], owner, operation)
        if change is not None:
            self.refresh(change)
        return result

    def recover(self, operation):
        def check():
            if operation['preview'].get('object_type') == 'habit':
                item = next((r for r in self.store.routines if r.id == operation['preview']['habit_id']), None)
                return item is not None and asdict(item) == operation['preview']['after']
            event = self.store.event_by_id(operation['preview']['event_id'])
            if operation['preview']['after'] is None:
                return event is None
            return event is not None and asdict(event) == operation['preview']['after']
        return self.dispatch(check)
