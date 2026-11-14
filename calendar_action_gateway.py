"""Sole AI calendar boundary. All Store access runs through the supplied dispatcher."""
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
import hashlib
import json
import re
import threading

from calendar_core import Event, COLORS
from event_commands import CommandConflictError
from event_search import EventSearchIndex, capture_search_store
from calendar_chat_tools import ChatError, iso_day, fail, validate_call


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class CalendarActionGateway:
    def __init__(self, store, commands, dispatch=lambda fn: fn(), refresh=lambda change: None, writable=lambda: True, habit_refresh=lambda: None):
        self.store, self.commands, self.dispatch, self.refresh, self.writable = store, commands, dispatch, refresh, writable
        self.habit_refresh = habit_refresh

    def query(self, arguments):
        validate_call('query_events', arguments)
        return self.dispatch(lambda: self._query(arguments))

    def categories(self):
        return self.dispatch(lambda: {'categories': [{'id': category.id, 'name': category.name, 'color': category.color}
                              for category in self.store.sorted_categories()[:100]], 'palette': dict(COLORS)})

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
        for event in store.events:
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
        if name == 'preview_create_habit':
            from calendar_habit_gateway import prepare_habit
            if previous or any(item.id == identifier for item in self.store.routines):
                fail('请先处理当前操作；习惯ID已存在。', 'operation_conflict')
            return prepare_habit(self.store, args, identifier, message)
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
        preview = self.commands.preview_upsert(candidate, label='AI新建事项' if current is None else 'AI修改事项')
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
        end = iso_day(values.pop('end_date')) if 'end_date' in values else None
        change_date = 'date' in values
        change_time = 'time' in values
        try:
            day = iso_day(values.pop('date')) if 'date' in values else candidate.due_date
        except ChatError as error:
            error.field = 'date'
            raise
        if 'time' in values:
            clock = values.pop('time')
            if clock is not None and (not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock)):
                fail('时间必须是 HH:MM，或明确选择无具体时间。', field='time')
            candidate.has_time = clock is not None
            hour, minute = map(int, clock.split(':')) if clock else (23, 59)
        else:
            hour, minute = candidate.due_at.hour, candidate.due_at.minute
        if change_time:
            candidate.due = datetime.combine(day, time(hour, minute)).isoformat(timespec='minutes')
        elif change_date:
            # Date-only changes preserve the exact old time precision/offset.
            candidate.due = day.isoformat() + candidate.due[10:]
        if values.get('category_id') is not None and self.store.category_by_id(values.get('category_id')) is None:
            fail('分类不存在。', field='category_id')
        if 'category_id' in values and 'color' not in values and 'color_mode' not in values:
            values['color_mode'] = ('inherit' if values['category_id'] else 'override') if creating or candidate.color_mode == 'inherit' else 'override'
        if 'color' in values:
            values.setdefault('color_mode', 'override')
        if values.get('color_mode') == 'inherit' and not values.get('category_id', candidate.category_id):
            if creating:
                # A model's default 'inherit' with no category means no override,
                # not a request to invent a category or change Store constraints.
                values.pop('color_mode')
            else:
                fail('此事项没有分类，请先选择分类，或保留当前颜色。', 'clarification_required', 'changes.color_mode')
        if 'reminder' in values and values['reminder'] is not None and not candidate.has_time:
            fail('此事项没有具体时间，请先确认提醒时刻；不能擅自设置定时提醒。', 'clarification_required', 'reminder')
        for key, value in values.items():
            setattr(candidate, key, value)
        if end is not None:
            if end < day or (end-day).days > 1095:
                fail('结束日期应在开始日期之后且范围合理。', field='end_date')
            count = sum(not candidate.skip_non_working_days or self.store.is_workday(day+timedelta(days=i))
                        for i in range((end-day).days+1))
            if not 1 <= count <= 365:
                fail('日期范围应包含1至365个有效日期。', field='end_date')
            candidate.duration_days = count
            if self.store.event_dates(candidate)[-1] != end:
                fail('结束日期不是有效工作日，请明确最后执行日期。', 'clarification_required', 'end_date')
        if not candidate.title.strip():
            fail('事项标题不能为空。')
        if candidate.end_as_ddl and (candidate.duration_days == 1 or candidate.event_type == 'ddl'):
            fail('末日DDL仅用于多日一般/紧急事项。')
        return candidate

    def execute(self, operation, journal, owner):
        """Only called after explicit confirmation. Journal intent precedes one command."""
        return self.dispatch(lambda: self._execute(operation, journal, owner))

    def _execute(self, operation, journal, owner):
        if not self.writable():
            fail('请先关闭当前编辑器或等待数据操作结束，再确认。', 'execution_failed')
        preview = operation['preview']
        if preview.get('object_type') == 'habit':
            from calendar_core import RoutineItem
            if any(item.id == preview['habit_id'] for item in self.store.routines):
                fail('习惯ID已存在，请核对执行结果。', 'operation_conflict')
            item = RoutineItem(**preview['after'])
            operation['status'] = 'executing'
            journal.put('operation', operation['operation_id'], owner, operation)
            before = list(self.store.routines)
            try:
                self.store.upsert_routine(item)
            except (OSError, ValueError):
                self.store.routines = before
                fail('习惯保存未成功，未应用此操作。', 'execution_failed')
            result = dict(object_type='habit', habit_id=item.id, habit=dict(id=item.id, title=item.title,
                color=item.color, schedule=preview['schedule'], next_on=preview['next_on']), changed=True)
            operation.update(status='succeeded', operation_result=result)
            journal.put('operation', operation['operation_id'], owner, operation)
            self.habit_refresh()
            return result
        if fingerprint(self._state(preview['event_id'])) != preview['before_hash']:
            fail('事项已被修改，请重新查询和预览。', 'operation_conflict')
        deleting = preview['after'] is None
        if not deleting:
            candidate = Event(**preview['after'])
            command = self.commands.preview_upsert(candidate, label='AI确认事项操作')
            if asdict(command.event()) != preview['after'] or [d.isoformat() for d in command.effective_dates] != preview['dates']:
                fail('分类或工作日日历已变化，请重新预览。', 'operation_conflict')
        operation['status'] = 'executing'
        journal.put('operation', operation['operation_id'], owner, operation)
        try:
            change = self.commands.delete(preview['event_id']) if deleting else self.commands.commit(command)
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


class TkDispatcher:
    def __init__(self, app):
        self.app, self.owner = app, threading.get_ident()

    def __call__(self, function):
        from concurrent.futures import Future, TimeoutError
        if threading.get_ident() == self.owner:
            return function()
        future = Future()
        def run():
            if self.app.lifecycle.exiting or self.app.lifecycle.stopped:
                future.cancel()
            elif future.set_running_or_notify_cancel():
                try:
                    future.set_result(function())
                except Exception as error:
                    future.set_exception(error)
        if self.app.lifecycle.exiting or self.app.lifecycle.stopped:
            fail('月历正在退出。', 'service_stopped')
        self.app._call_main(run)
        try:
            return future.result(timeout=10)
        except TimeoutError:
            future.cancel()  # Prevent a timed-out queued callback from later mutating data.
            fail('主窗口暂不可用，请稍后查询操作结果。', 'service_unavailable')
