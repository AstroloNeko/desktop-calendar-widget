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
from calendar_core import Store, Event
from event_commands import EventCommandHistory
from calendar_action_gateway import CalendarActionGateway
from calendar_chat_sessions import ChatJournal
from calendar_chat_service import CalendarChatService
from calendar_chat_tools import ChatError
from llm_provider import AIConfiguration, DeepSeekProvider, UserProtection


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
        store.save()
        history = EventCommandHistory(store)
        service = CalendarChatService(CalendarActionGateway(store, history),
            ChatJournal(root/'chat.db', UserProtection()), provider)
        scenarios = [('A', '明天我要做个WB实验')] if args.followups else [('A', '明天我要做个WB实验'), ('B', '11.22开会'), ('C', '把细胞实验那个改成动物实验')]
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
                if label in ('A', 'B'):
                    entry['minimal_defaults'] = (after['duration_days'] == 1 and after['category_id'] is None and
                        after['color_mode'] == 'override' and not after['has_time'] and after['reminder'] is None)
                else:
                    entry['title_only'] = op['preview']['changed_fields'] == ['title'] and after['title'] == '动物实验'
                confirmed = service.confirm_operation(sid, op['operation_id'], revision=op['revision'])
                entry['confirmed_status'] = confirmed['status']
            entry['passed'] = (entry['unconfirmed_bytes_unchanged'] and entry.get('minimal_defaults', entry.get('title_only', False))
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
                  passed=len(results) == (5 if args.followups else 3) and all(item['passed'] for item in results))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
