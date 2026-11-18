"""Explicit bounded paid-model smoke. Synthetic Store; read-only existing AI credentials.

No real calendar/history files are opened. Reports contain only tool metadata and
assertion outcomes, never keys, user profile paths, messages or field values.
"""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calendar_core import Store, Event, EventCategory, COLORS
from event_commands import EventCommandHistory
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_tools import ChatError
from desktop_calendar.assistant.llm_provider import AIConfiguration, DeepSeekProvider, UserProtection


class LimitedProvider:
    def __init__(self, provider):
        self.provider, self.calls = provider, 0

    def complete_bounded(self, messages, tools, remaining):
        if self.calls >= 14:
            raise ChatError('live_test_budget', '隔离验证模型调用预算已用完。')
        self.calls += 1
        return self.provider.complete_bounded(messages, tools, min(remaining, 40))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly permit <=14 model calls with synthetic data')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--followups', action='store_true', help='Create one synthetic event, then refine the same event across turns')
    parser.add_argument('--attributes', action='store_true', help='Synthetic semantic classification and explicit palette combinations')
    parser.add_argument('--habits', action='store_true', help='Synthetic actual Habit 2.0 creation, two requests')
    parser.add_argument('--habit-edits', action='store_true', help='Follow up synthetic habit with scoped changes')
    args = parser.parse_args()
    if not args.live:
        parser.error('Real provider smoke requires explicit --live')
    config = AIConfiguration(Path(os.environ['APPDATA'])/'DesktopCalendar/ai-assistant')
    values = config.load()
    if not values.get('api_key') or not values.get('consent'):
        parser.error('The app must already have user-configured credentials and remote consent')
    provider = LimitedProvider(DeepSeekProvider(config))
    results = []
    with tempfile.TemporaryDirectory(prefix='calendar-chat-live-') as temporary:
        root = Path(temporary)
        store = Store(root/'calendar.json')
        store.events = [Event('qa-cell-event', '细胞实验', '2026-11-22T15:00:37', duration_days=3, color='#123456', notes='仅为隔离测试')]
        if args.attributes:
            store.categories = [EventCategory('qa-lab', '实验', '#123456'), EventCategory('qa-meet', '开会')]
        store.save()
        history = EventCommandHistory(store)
        service = CalendarChatService(CalendarActionGateway(store, history),
            ChatJournal(root/'chat.db', UserProtection()), provider, auto_execute=False)
        if args.habits:
            service.auto_execute = True
            created = []
            for text, frequency, interval in [('新增习惯，每周一开组会，紫色', 'weekly', 1), ('每隔3天做一次PCR', 'daily', 3)]:
                sid = service.new_session()['session_id']
                request = uuid.uuid4().hex
                response = service.send_message(sid, text, request)
                item = store.routines[-1] if response['status'] == 'succeeded' else None
                created.append((sid, item.id if item else None))
                results.append(dict(status=response['status'], diagnostic=service.get_diagnostics(request),
                    passed=item is not None and item.kind == 'habit' and item.frequency == frequency and item.interval == interval
                    and (frequency != 'weekly' or item.weekdays == [0] and item.color == COLORS['鸢尾紫'])))
            if args.habit_edits:
                sid, identifier = created[0]
                for text, expected in [('改成每周一和周四', lambda r: r.frequency == 'weekly' and r.weekdays == [0, 3]),
                                       ('颜色换成绿色', lambda r: r.color == COLORS['薄荷绿']),
                                       ('以后只在工作日执行', lambda r: r.workdays_only),
                                       ('把提醒改成早上九点', lambda r: r.reminder_enabled and r.reminder_time == '09:00'),
                                       ('提醒关掉', lambda r: not r.reminder_enabled)]:
                    request = uuid.uuid4().hex
                    response = service.send_message(sid, text, request)
                    current = next(r for r in store.routines if r.id == identifier)
                    results.append(dict(status=response['status'], diagnostic=service.get_diagnostics(request),
                        passed=response['status'] == 'succeeded' and response['operation_result']['habit_id'] == identifier and expected(current)))
                    if not results[-1]['passed']:
                        break
            report = dict(synthetic_only=True, provider_calls=provider.calls, cases=results,
                          passed=all(entry['passed'] for entry in results) and len(store.events) == 1)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return 0 if report['passed'] else 1
        scenarios = [('A', '明天我要做个WB实验')] if args.followups else [('A', '明天我要做个WB实验'), ('B', '11.22开会'), ('C', '把细胞实验那个改成动物实验')]
        if args.attributes:
            scenarios = [('purple', '加个11.23做WB，紫色'), ('meeting', '明天下午三点组会，提前半小时提醒')]
        for label, text in scenarios:
            sid = service.new_session()['session_id']
            request = uuid.uuid4().hex
            before = store.data_file.read_bytes()
            response = service.send_message(sid, text, request)
            entry = dict(case=label, status=response['status'], error_code=response.get('error_code'),
                         unconfirmed_bytes_unchanged=store.data_file.read_bytes() == before,
                         diagnostic=service.get_diagnostics(request))
            op = response.get('pending_operation')
            if response['status'] == 'confirmation_required' and op:
                after = op['preview']['after']
                if args.attributes:
                    entry['attribute_match'] = (after['category_id'] == ('qa-lab' if label == 'purple' else 'qa-meet') and
                        ((after['color'] == COLORS['鸢尾紫'] and after['color_mode'] == 'override' and not after['has_time'])
                         if label == 'purple' else (after['has_time'] and 'T15:00' in after['due'] and after['reminder'] == 30 and after['color_mode'] == 'inherit')))
                elif label in ('A', 'B'):
                    entry['minimal_defaults'] = (after['duration_days'] == 1 and after['category_id'] is None and
                        after['color_mode'] == 'override' and not after['has_time'] and after['reminder'] is None)
                else:
                    entry['title_only'] = op['preview']['changed_fields'] == ['title'] and after['title'] == '动物实验'
                confirmed = service.confirm_operation(sid, op['operation_id'], revision=op['revision'])
                entry['confirmed_status'] = confirmed['status']
            entry['passed'] = (entry['unconfirmed_bytes_unchanged'] and entry.get('attribute_match', entry.get('minimal_defaults', entry.get('title_only', False)))
                               and entry.get('confirmed_status') == 'succeeded')
            results.append(entry)
            if provider.calls >= 14:
                break
        if args.followups and results[0]['passed']:
            identifier = op['preview']['event_id']
            for label, text, field in [('A-rename', '把刚才那个改名为细胞实验', 'title'),
                                      ('A-time', '把刚才那个改到下午四点', 'time'),
                                      ('A-date', '改到后天', 'date'),
                                      ('A-reminder', '再提前半小时提醒', None)]:
                request = uuid.uuid4().hex
                before = store.data_file.read_bytes()
                response = service.send_message(sid, text, request)
                entry = dict(case=label, status=response['status'], error_code=response.get('error_code'),
                             unconfirmed_bytes_unchanged=store.data_file.read_bytes() == before,
                             diagnostic=service.get_diagnostics(request))
                proposal = response.get('pending_operation')
                if field is None:
                    entry['passed'] = response['status'] == 'clarification_required' and entry['unconfirmed_bytes_unchanged']
                elif response['status'] == 'confirmation_required' and proposal:
                    entry['same_event'] = proposal['preview']['event_id'] == identifier
                    allowed = {'title'} if field == 'title' else {'due', 'has_time'} if field == 'time' else {'due'}
                    entry['minimal_patch'] = set(proposal['preview']['changed_fields']) <= allowed
                    confirmed = service.confirm_operation(sid, proposal['operation_id'], revision=proposal['revision'])
                    entry['confirmed_status'] = confirmed['status']
                    entry['passed'] = entry['same_event'] and entry['minimal_patch'] and confirmed['status'] == 'succeeded' and entry['unconfirmed_bytes_unchanged']
                else:
                    entry['passed'] = False
                results.append(entry)
                if not entry['passed']:
                    break
    report = dict(synthetic_only=True, provider_calls=provider.calls, cases=results,
                  passed=len(results) == (2 if args.attributes else 5 if args.followups else 3) and all(item['passed'] for item in results))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
