"""Reproducible Calendar Agent evaluation on synthetic SQLite calendars."""
from argparse import ArgumentParser
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from calendar_core import DATA_DIR, Store
from calendar_runtime import HeadlessRuntime
from desktop_calendar.assistant import calendar_chat_service
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_protocol import ChatError, OWNER
from desktop_calendar.assistant.calendar_chat_tools import request_context, validate_call
from desktop_calendar.assistant.llm_provider import AIConfiguration, DeepSeekProvider
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import EventCommandHistory
from event_input import build_event_candidate


ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name('calendar_agent_eval_cases.json')
DATASET_LIMIT, CASES_LIMIT, REPEAT_LIMIT, LIVE_CALLS_LIMIT = 500, 40, 3, 120
CASE_ID = re.compile(r'[a-z][a-z0-9-]{2,47}')
SUPPORT = {'supported', 'exploratory', 'unsupported'}
INTERACTIONS = {'result', 'clarification', 'selection', 'confirmation', 'final_confirmation',
                'succeeded', 'cancelled', 'error', 'restart'}
INFRA_CODES = {'model_unavailable', 'planning_timeout', 'tool_unavailable',
               'eval_call_budget', 'eval_mock_exhausted', 'service_unavailable'}
EVENT_FIELDS = {'id', 'title', 'date', 'end', 'time', 'event_type', 'category_id', 'category',
                'done', 'reminder', 'deadline', 'duration_days', 'skip_non_working_days',
                'end_as_ddl', 'color', 'color_mode', 'color_override', 'notes',
                'due', 'has_time', 'created_at', 'snooze_until', 'reminder_plan'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def reference_time(case):
    moment = datetime.fromisoformat(case['reference_datetime'])
    require(moment.utcoffset() is not None, 'reference_datetime must have an explicit UTC offset')
    require(case['timezone'] and isinstance(case['timezone'], str), 'timezone must be a named label')
    expected = {'Asia/Singapore': timedelta(hours=8), 'Asia/Shanghai': timedelta(hours=8),
                'UTC': timedelta(0)}.get(case['timezone'])
    require(expected is None or moment.utcoffset() == expected, 'timezone and UTC offset disagree')
    return moment


def validate_cases(cases):
    require(isinstance(cases, list) and 1 <= len(cases) <= DATASET_LIMIT, 'invalid case count')
    seen = set()
    for case in cases:
        case_id = case['case_id']
        require(CASE_ID.fullmatch(case_id) and case_id not in seen, 'invalid or duplicate case_id')
        seen.add(case_id)
        reference_time(case)
        require(case['support'] in SUPPORT and all(isinstance(case[k], str) and case[k]
                for k in ('category', 'description')), f'{case_id}: invalid metadata')
        require(isinstance(case['tags'], list) and all(isinstance(tag, str) for tag in case['tags']),
                f'{case_id}: invalid tags')
        seeds = case['initial_calendar']
        require(isinstance(seeds, list) and len(seeds) <= 100, f'{case_id}: invalid initial_calendar')
        seed_ids = set()
        for seed in seeds:
            identifier = seed['id']
            require(CASE_ID.fullmatch(identifier) and identifier not in seed_ids, f'{case_id}: invalid seed ID')
            seed_ids.add(identifier)
            validate_call('preview_create', {key: value for key, value in seed.items() if key != 'id'})
        if 'reminder_config' in case:
            from desktop_calendar.reminders.reminder_engine import normalize_config
            config = case['reminder_config']
            require(isinstance(config, dict) and set(config) <= {'enabled','time','events'}
                    and type(config.get('enabled')) is bool, f'{case_id}: invalid isolated reminder config')
            normalized = normalize_config(config)
            require(set(normalized['events']) == set(config.get('events', {})) <= seed_ids,
                    f'{case_id}: invalid seeded reminder rules')
        turns = case['turns']
        checks = case['expected_interaction']
        require(isinstance(turns, list) and isinstance(checks, list)
                and 1 <= len(turns) <= 12 and len(checks) == len(turns),
                f'{case_id}: turns and interaction checks must align')
        references = set()
        for index, (turn, check) in enumerate(zip(turns, checks), 1):
            kind = turn['action']
            require(kind in {'message', 'select', 'confirm', 'cancel', 'restart', 'receipt'},
                    f'{case_id}: invalid turn action')
            if kind == 'message':
                require(1 <= len(turn['text'].strip()) <= 4000, f'{case_id}: invalid message')
                require(isinstance(turn['mock'], list) and len(turn['mock']) <= 6, f'{case_id}: invalid mock rounds')
                if 'request_id' in turn:
                    require(isinstance(turn['request_id'], str) and 8 <= len(turn['request_id']) <= 128,
                            f'{case_id}: invalid request_id')
                for batch in turn['mock']:
                    require(isinstance(batch, list) and len(batch) <= 8, f'{case_id}: invalid mock batch')
                    for call in batch:
                        validate_call(call['name'], call['arguments'])
            elif kind == 'select':
                require(type(turn['index']) is int and turn['index'] >= 1, f'{case_id}: invalid selection')
            elif kind in {'confirm', 'cancel', 'receipt'}:
                require(turn['ref'] in references, f'{case_id}: unknown operation reference')
            if 'when' in turn:
                gate = turn['when']
                require(index > 1 and gate['kind'] in INTERACTIONS, f'{case_id}: invalid turn gate')
                require(all(isinstance(word, str) and word for word in gate.get('question_any', [])),
                        f'{case_id}: invalid question matcher')
            if 'save_as' in turn:
                require(turn['save_as'] not in references, f'{case_id}: duplicate operation reference')
                references.add(turn['save_as'])
            require(check['kind'] in INTERACTIONS and type(check['writes']) is int and check['writes'] >= 0,
                    f'{case_id}: invalid interaction contract')
            if 'also_accept' in check:
                accepted = check['also_accept']
                require(check['writes'] == 0 and isinstance(accepted, list) and accepted
                        and check['kind'] in {'result', 'clarification'}
                        and all(kind in {'result', 'clarification'} for kind in accepted),
                        f'{case_id}: alternatives are limited to read-only replies')
            if 'review_reply' in check:
                require(isinstance(check['review_reply'], str) and bool(check['review_reply'].strip()),
                        f'{case_id}: review_reply needs a reason')
            if 'update_created_on' in check:
                source = check['update_created_on']
                require(type(source) is int and 1 <= source < index and check['writes'] > 0,
                        f'{case_id}: update_created_on must reference an earlier creation turn')
        final = case['expected_final_state']
        require(set(final) == {'create', 'update', 'delete', 'writes'} and type(final['writes']) is int
                and final['writes'] >= 0, f'{case_id}: missing explicit final state/write expectation')
        require(isinstance(final['create'], list) and isinstance(final['update'], dict) and
                isinstance(final['delete'], list) and set(final['delete']) | set(final['update']) <= seed_ids
                and not set(final['delete']) & set(final['update']), f'{case_id}: invalid final state IDs')
        for fields in [*final['create'], *final['update'].values()]:
            require(isinstance(fields, dict) and bool(fields) and set(fields) <= EVENT_FIELDS,
                    f'{case_id}: invalid expected event fields')
        require(isinstance(case['expected_plan'], list), f'{case_id}: expected_plan must be a list')
        for plan in case['expected_plan']:
            require(type(plan['turn']) is int and 1 <= plan['turn'] <= len(turns)
                    and plan['action'] in {'none', 'create', 'update', 'complete', 'delete', 'query', 'revise', 'clarify'},
                    f'{case_id}: invalid expected plan')
            if 'resolved_id' in plan:
                require(plan['resolved_id'] in seed_ids and plan['action'] == 'update'
                        and plan.get('target', {}).get('quantity') == 'one',
                        f'{case_id}: resolved_id needs a seeded single update target')
    return cases


def match_fields(actual, expected):
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual:
            return False
        if key == 'title' and isinstance(value, dict) and set(value) == {'one_of'}:
            if actual[key] not in value['one_of']:
                return False
        elif key == 'notes' and isinstance(value, dict) and set(value) == {'contains'}:
            if not isinstance(actual[key], str) or value['contains'] not in actual[key]:
                return False
        elif isinstance(value, dict):
            if not match_fields(actual[key], value):
                return False
        elif actual[key] != value:
            return False
    return True


def match_members(actual, expected):
    """Match exact multiplicity, independent of member order and generated IDs."""
    if len(actual) != len(expected):
        return False
    matching = [[i for i, row in enumerate(actual) if match_fields(row, fields)] for fields in expected]
    allocated = {}

    def assign(member, visited):
        for candidate in matching[member]:
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in allocated or assign(allocated[candidate], visited):
                allocated[candidate] = member
                return True
        return False

    return all(assign(member, set()) for member in range(len(expected)))


@contextmanager
def fixed_time(moment):
    class CaseDate(date):
        @classmethod
        def today(cls):
            return moment.date()

    class CaseDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    with ExitStack() as stack:
        for name in ('calendar_action_gateway', 'calendar_batch_delete', 'calendar_chat_attributes',
                     'calendar_chat_service', 'calendar_habit_gateway', 'calendar_task_plan'):
            stack.enter_context(patch(f'desktop_calendar.assistant.{name}.date', CaseDate))
        stack.enter_context(patch('calendar_core.date', CaseDate))
        stack.enter_context(patch('calendar_core.datetime', CaseDateTime))
        stack.enter_context(patch.object(calendar_chat_service, 'request_context', lambda: request_context(moment)))
        yield


def checked_path(path, root):
    resolved = Path(path).resolve()
    profile = DATA_DIR.resolve()
    require(not resolved.is_relative_to(profile), 'evaluation paths cannot be in the user calendar profile')
    require(resolved.is_relative_to(Path(root).resolve()), 'evaluation database escapes its temporary directory')
    require(not Path(path).is_symlink(), 'evaluation paths cannot be symlinks')
    return resolved


class SyntheticProtection:
    """Only the generated temporary journal uses this test-only codec."""
    encrypt = decrypt = staticmethod(bytes)


class MockProvider:
    def __init__(self):
        self.rounds = iter(())

    def set_turn(self, rounds, revision=None):
        self.rounds = iter(deepcopy(rounds))
        self.revision = revision

    def complete_bounded(self, messages, tools, remaining):
        calls = next(self.rounds, None)
        if calls is None:
            raise ChatError('eval_mock_exhausted', 'No scripted model response left')
        result = []
        for index, tool in enumerate(calls):
            args = tool['arguments']
            if args.get('revision') == '$target_revision':
                args['revision'] = self.revision
            result.append(dict(id=f'call-{index}', type='function',
                               function=dict(name=tool['name'], arguments=json.dumps(args, ensure_ascii=False))))
        return dict(content=None, tool_calls=result)


class RecordingProvider:
    def __init__(self, provider, budget):
        self.provider = provider
        self.budget = budget  # Mutable counter shared by all attempts in a run.
        self.calls = []

    def complete_bounded(self, messages, tools, remaining):
        if not self.budget[0]:
            raise ChatError('eval_call_budget', 'Model-call budget exhausted')
        self.budget[0] -= 1
        started = perf_counter()
        trace = dict(tools=[], usage=None)
        self.calls.append(trace)
        try:
            result = self.provider.complete_bounded(messages, tools, min(remaining, 30))
            raw_tools = result.get('tool_calls') if isinstance(result, dict) else None
            if not isinstance(result, dict) or (raw_tools and not isinstance(raw_tools, list)):
                trace['malformed'] = True
                return result
            for item in raw_tools or []:
                if not isinstance(item, dict) or not isinstance(item.get('function'), dict):
                    trace['tools'].append(dict(malformed=True))
                    continue
                function = item['function']
                try:
                    arguments = json.loads(function.get('arguments', ''))
                except (ValueError, TypeError):
                    arguments = None
                trace['tools'].append(dict(name=function.get('name'), arguments=arguments))
            return result
        except Exception as error:
            trace['error'] = getattr(error, 'code', type(error).__name__)
            raise
        finally:
            trace['seconds'] = round(perf_counter()-started, 4)


class LiveConfiguration:
    """Provider-only config copied in memory. Never pass it to the Runtime."""
    def __init__(self, values):
        self._values = {key: values[key] for key in ('endpoint', 'model', 'api_key', 'timeout', 'temperature', 'consent')}

    def load(self):
        return dict(self._values)


def live_provider(config_file):
    path = Path(config_file)
    require(path.name == 'configuration.dpapi' and path.is_file(), 'select a saved configuration.dpapi file')
    values = AIConfiguration(path.parent).load()
    endpoint = urlsplit(values['endpoint'])
    require(endpoint.scheme == 'https' and endpoint.hostname and not (
        endpoint.username or endpoint.password or endpoint.query or endpoint.fragment), 'invalid configured HTTPS endpoint')
    require(values.get('consent') is True and isinstance(values.get('api_key'), str) and values['api_key'],
            'saved model key and remote consent are required')
    return DeepSeekProvider(LiveConfiguration(values)), (values['api_key'], values.get('token', '')), values['model']


SECRET_FIELD = re.compile(r'api.?key|authorization|password|(?:access|refresh|bearer|http)[_-]?token|secret|credential|^token$', re.I)
SECRET_TEXT = re.compile(r'(?i)\bBearer\s+[^\s,;"\x27]+|\bsk-[A-Za-z0-9_-]{8,}')


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {redact(key, secrets): '[REDACTED]' if SECRET_FIELD.search(key) else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return SECRET_TEXT.sub('[REDACTED]', value)
    return value


class IsolatedReminderBackend:
    """Preflight may cancel stale work, but evaluation never submits notifications."""
    def remove(self, _tag):
        pass

    def send(self, *_args, **_kwargs):
        raise AssertionError('evaluation must not send notifications')


def read_calendar(path, root, *, reminder_moment=None):
    """Reopen the persisted SQLite state and apply the normal effective-date/color view."""
    path = checked_path(path, root)
    require(path.is_file(), 'isolated SQLite file is missing')
    store = Store(path, repository_factory=SqliteCalendarRepository)
    try:
        require(not store.load_error, 'isolated SQLite read failed')
        reminders = None
        if reminder_moment is not None:
            from desktop_calendar.reminders.reminder_engine import ReminderEngine
            reminders = ReminderEngine(store, IsolatedReminderBackend())
        gateway = CalendarActionGateway(store, EventCommandHistory(store), reminders=reminders,
            **({'reminder_now':lambda: reminder_moment.replace(tzinfo=None)} if reminders is not None else {}))
        summaries = {event.id: gateway.detail(event.id) for event in store.events}
        raw = store.repository.read_document()
        raw['events'] = {row['id']: {**row, **summaries[row['id']]} for row in raw['events']}
        if reminders is not None:
            rules = raw['settings']['native_reminders'].get('events', {})
            # Live rules are compared under their Event; retain orphaned changes separately.
            raw['settings']['native_reminders']['events'] = {k:v for k,v in rules.items() if k not in raw['events']}
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            raw['receipts'] = {identifier: dict(revision=revision, affected_ids=json.loads(affected))
                               for identifier, revision, affected in connection.execute(
                                   'SELECT operation_id,committed_revision,affected_ids FROM operation_receipts')}
        return raw
    finally:
        store.repository.close()


def calendar_delta(before, after):
    old, new = before['events'], after['events']
    return dict(
        created=[new[key] for key in sorted(new.keys()-old.keys())],
        deleted=sorted(old.keys()-new.keys()),
        updated={key: {field: {'before': old[key].get(field), 'after': value}
                       for field, value in new[key].items() if value != old[key].get(field)}
                 for key in sorted(old.keys() & new.keys()) if old[key] != new[key]},
        other_changes=sorted(key for key in before if key not in {'events', 'receipts'} and before[key] != after[key]),
        receipts={key: after['receipts'][key] for key in sorted(after['receipts'].keys()-before['receipts'].keys())})


def normalize_plans(calls):
    plans = []
    for call in calls:
        for tool in call['tools']:
            args = tool.get('arguments')
            if not isinstance(args, dict):
                continue
            name = tool['name']
            if not isinstance(name, str):
                continue
            if name == 'propose_events':
                plans.append({**args, 'tool': name})
            elif name == 'clarify':
                plans.append(dict(action='clarify', question=args.get('question')))
                if isinstance(args.get('create_plan'), dict):
                    plans.append({**args['create_plan'], 'tool': name})
            elif name == 'revise_event_target':
                plans.append(dict(action='revise', target={key: value for key, value in args.items() if key != 'revision'}))
            elif name in ('query_events', 'get_event', 'query_habits', 'get_habit'):
                plans.append(dict(action='query', target=args))
            elif name and (name.startswith('preview_') or name.startswith('batch_') or name=='delete_events_batch'):
                action = next((action for action in ('create', 'update', 'complete', 'delete') if action in name), 'complete')
                plans.append(dict(action=action, tool=name, legacy=args))
    return plans


def interaction(response):
    op = response.get('pending_operation') or {}
    if op.get('status') == 'pending':
        return 'final_confirmation' if op.get('stage') == 'awaiting_final_confirmation' else 'confirmation'
    status = response.get('status')
    if status == 'clarification_required':
        return 'selection' if response.get('selection_revision') else 'clarification'
    if status in ('succeeded', 'cancelled', 'restart'):
        return status
    return 'error' if response.get('error_code') or status in ('failed', 'uncertain', 'committed_unverified') else 'result'


def gate_allows(gate, previous):
    return interaction(previous) == gate['kind'] and (
        not gate.get('question_any') or any(word in previous.get('assistant_message', '') for word in gate['question_any']))


def judge(case, turns, final_delta, infrastructure=None, blocked=False):
    issues = []

    def issue(dimension, code, turn=None, *, review=False, detail=None):
        issues.append(dict(dimension=dimension, code=code, turn=turn, review=review, detail=detail))

    if infrastructure:
        issue('infrastructure', infrastructure['code'], infrastructure.get('turn'), detail=infrastructure.get('stage'))
    for turn in turns:
        index = turn['turn']
        expected = case['expected_interaction'][index-1]
        actual = turn['interaction']
        delta = turn['delta']
        writes = len(delta['receipts'])
        if writes > expected['writes']:
            issue('state', 'unexpected_write', index)
        elif writes != expected['writes'] and not turn['infrastructure'] and actual!='clarification':
            issue('state', 'verification_error', index, detail='write count')
        if expected['writes'] == 0 and any(delta[key] for key in ('created', 'deleted', 'updated', 'other_changes')):
            issue('state', 'unexpected_write', index, detail='persisted state changed')
        if turn['infrastructure']:
            continue
        response = turn['response']
        if actual != expected['kind'] and actual not in expected.get('also_accept', []):
            code = 'target_resolution_error' if expected['kind']=='selection' else (
                'pending_lost' if turn['action'] in ('select','confirm','receipt') else 'execution_error')
            if actual == 'clarification' and expected['kind'] == 'succeeded' and writes == 0:
                code = 'unplanned_clarification'
            issue('workflow', code, index, review=actual=='clarification' and writes==0,
                  detail=f"expected {expected['kind']}; got {actual}")
        if 'update_created_on' in expected and actual == 'succeeded':
            source = turns[expected['update_created_on']-1]['delta']
            targets = {event['id'] for event in source['created']}
            if not targets or set(delta['updated']) != targets or delta['created'] or delta['deleted']:
                issue('state', 'verification_error', index, detail='earlier Event ID was not preserved')
        if 'error_code' in expected and response.get('error_code') != expected['error_code']:
            issue('workflow', 'response_mismatch', index, detail='error code')
        if 'candidate_ids' in expected and sorted(row['id'] for row in response.get('candidates', [])) != sorted(expected['candidate_ids']):
            issue('workflow', 'target_resolution_error', index, detail='candidate IDs')
        if 'candidate_fields' in expected and not match_members(response.get('candidates', []), expected['candidate_fields']):
            issue('workflow', 'response_mismatch', index, detail='query facts')
        if 'receipt' in expected and not match_fields(response.get('operation_result'), expected['receipt']):
            issue('workflow', 'response_mismatch', index, detail='execution receipt')
        if actual=='succeeded' and expected['writes']>0 and writes==expected['writes']:
            result = response.get('operation_result') or {}
            if result.get('operation_id') not in delta['receipts'] or not (
                    result.get('committed') and result.get('goal_satisfied')):
                issue('workflow','verification_error',index,detail='SQLite receipt does not corroborate the response')
        if expected.get('message_contains') and not all(
                text in response.get('assistant_message', '') for text in expected['message_contains']):
            issue('response', 'response_mismatch', index, review=True, detail='human-readable reply')
        if expected.get('review_reply'):
            issue('response', 'reply_review', index, review=True, detail=expected['review_reply'])
    for plan in case['expected_plan']:
        observed = next((row for row in turns if row['turn']==plan['turn'] and not row['infrastructure']), None)
        if observed is None:
            continue
        if plan['action']=='none':
            if any(item['action'] not in ('query','clarify') for item in observed['plans']):
                issue('model','intent_mismatch',plan['turn'])
            continue
        candidates = [item for item in observed['plans'] if item['action']==plan['action']]
        if not candidates:
            issue('model','intent_mismatch',plan['turn'],review=observed['interaction']=='clarification')
            continue
        actual = candidates[-1]
        fields = {key: value for key, value in plan.items() if key not in ('turn', 'members', 'resolved_id')}
        items = actual.get('items')
        if 'members' in plan and (not isinstance(items,list) or len(items) != len(plan['members'])):
            issue('model','member_count_mismatch',plan['turn'])
        elif 'members' in plan:
            actual_members = [item.get('fields', item) if isinstance(item, dict) else item for item in items]
            expected_members = plan['members']
            temporal = {'date', 'end_date', 'time'}
            if any(temporal & member.keys() for member in expected_members) and not match_members(
                    actual_members, [{key: value for key, value in member.items() if key in temporal}
                                     for member in expected_members]):
                issue('model','date_time_mismatch',plan['turn'])
            if not match_members(actual_members, expected_members):
                issue('model','plan_incomplete',plan['turn'])
        matches = match_fields(actual, fields)
        if not matches and plan['action'] == 'update' and isinstance(items, list) and len(items) == 1:
            resolved_id = plan.get('resolved_id')
            source = case['expected_interaction'][plan['turn']-1].get('update_created_on')
            if source:
                created = turns[source-1]['delta']['created']
                if len(created) == 1:
                    resolved_id = created[0]['id']
            if resolved_id:
                matches = match_fields(items[0], {'event_id': resolved_id, 'fields': fields.get('fields', {})})
        if not matches:
            issue('model','plan_incomplete',plan['turn'])
    expected = case['expected_final_state']
    last = turns[-1] if turns else None
    incomplete_clarification = bool(last and last['interaction']=='clarification'
                                    and case['expected_interaction'][last['turn']-1]['kind']!='clarification'
                                    and not last['delta']['receipts'])
    if final_delta is not None:
        for key, fields in expected['update'].items():
            allowed = set(fields)
            if 'reminder_plan' in allowed:
                allowed.add('snooze_until')
            if 'date' in allowed:
                allowed.update({'due','end','deadline'})
            if 'time' in allowed:
                allowed.update({'due','has_time'})
            if 'color' in allowed:
                allowed.update({'color_override','color_mode'})
            changed = final_delta['updated'].get(key, {})
            if set(changed)-allowed:
                issue('state','unexpected_write',detail=f'unrequested fields on {key}')
        if (set(final_delta['updated'])-set(expected['update']) or set(final_delta['deleted'])-set(expected['delete'])
                or final_delta['other_changes'] or len(final_delta['receipts'])>expected['writes']):
            issue('state','unexpected_write',detail='outside expected scope')
        if len(final_delta['created']) > len(expected['create']):
            issue('state','unexpected_write',detail='too many created Events')
        if not infrastructure and not blocked and not incomplete_clarification:
            actual_updates = {key: {field: values['after'] for field, values in changes.items()}
                              for key, changes in final_delta['updated'].items()}
            if (not match_members(final_delta['created'],expected['create'])
                    or final_delta['deleted'] != sorted(expected['delete'])
                    or not all(match_fields(actual_updates.get(key), fields) for key, fields in expected['update'].items())
                    or len(final_delta['receipts']) != expected['writes']):
                issue('state','verification_error',detail='final persisted state')
    if blocked:
        issue('workflow',blocked,review=blocked=='alternate_clarification',detail='scripted follow-up was not sent')

    dimensions = {}
    checked = {'model': any(row['plans'] and not row['infrastructure'] for row in turns)
               or (bool(case['expected_plan']) and bool(turns) and not infrastructure),
               'workflow': any(not row['infrastructure'] for row in turns),
               'state': final_delta is not None and not blocked and not infrastructure and not incomplete_clarification,
               'response': any(check.get('message_contains') or check.get('review_reply')
                               for check in case['expected_interaction'])}
    for dimension, was_checked in checked.items():
        own = [item for item in issues if item['dimension']==dimension]
        dimensions[dimension] = ('fail' if any(not item['review'] for item in own) else
                                 'needs_review' if own else 'pass' if was_checked else 'not_checked')
    if any(item['code']=='unexpected_write' for item in issues):
        verdict = 'fail'
    elif infrastructure:
        verdict = 'infrastructure_error'
    elif 'fail' in dimensions.values():
        verdict = 'fail'
    elif issues:
        verdict = 'needs_review'
    else:
        verdict = 'pass'
    return verdict, dimensions, issues


class EvaluationCalendar:
    """Owns only the paths created from a per-attempt temporary directory."""
    def __init__(self, root, provider, moment, reminder_config=None):
        self.root = Path(root)
        self.path = checked_path(self.root/'calendar.sqlite3', self.root)
        self.journal_path = checked_path(self.root/'journal.sqlite3', self.root)
        require(not (self.path.exists() or self.journal_path.exists()), 'evaluation requires fresh databases')
        self.provider = provider
        self.moment = moment
        self.reminder_config = reminder_config
        self.runtime = None

    def start(self):
        from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
        journal = ChatJournal(checked_path(self.journal_path, self.root), SyntheticProtection())
        self.runtime = HeadlessRuntime(checked_path(self.path, self.root), provider=self.provider,
                                       journal=journal, protector=SyntheticProtection(),
                                       clock=self.moment.timestamp, repository_factory=SqliteCalendarRepository,
                                       notification_backend=IsolatedReminderBackend() if self.reminder_config is not None else None)

    def stop(self):
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    @property
    def service(self):
        return self.runtime.service

    def seed(self, rows):
        for row in rows:
            values = {key: value for key, value in row.items() if key != 'id'}
            event = self.runtime._call(lambda: build_event_candidate(
                values, identifier=row['id'], context=self.runtime._store))
            result = self.runtime.events.upsert(event)
            require(result.changed, 'fixture Event failed to persist')
        if self.reminder_config is not None:
            self.runtime._call(lambda: self.runtime._reminders.configure(self.reminder_config))

    def read(self):
        return read_calendar(self.path, self.root,
                             reminder_moment=self.moment if self.reminder_config is not None else None)


def run_turn(calendar, session, turn, index, previous, references, provider):
    kind = turn['action']
    service = calendar.service
    request = None
    if isinstance(provider.provider, MockProvider):
        saved = service.get_session(session).get('awaiting_target') or {}
        provider.provider.set_turn(turn.get('mock', []), saved.get('revision'))
    if kind == 'message':
        request = turn.get('request_id', f'eval-turn-{index}')
        response = service.send_message(session, turn['text'], request)
    elif kind == 'select':
        candidates = previous.get('candidates', [])
        if turn['index'] > len(candidates) or not previous.get('selection_revision'):
            raise ChatError('target_resolution_error', 'No revision-bound candidate at this index')
        response = service.select_candidate(session, candidates[turn['index']-1]['id'],
                                            revision=previous['selection_revision'])
    elif kind == 'restart':
        calendar.stop()
        calendar.start()
        calendar.service.get_session(session)
        response = dict(status='restart')
    else:
        op = references.get(turn['ref'])
        if not op:
            raise ChatError('pending_lost', 'Saved operation reference unavailable')
        if kind == 'confirm':
            response = service.confirm_operation(session, op['operation_id'], revision=op['revision'])
        elif kind == 'cancel':
            response = service.cancel_operation(session, op['operation_id'])
        else:
            response = service.get_operation_result(session, op['operation_id'])
    return response, request


def run_case(case, provider, budget, attempt=1, *, secrets=()):
    """Attempt a single script. Even harness exceptions produce a non-pass record."""
    started = perf_counter()
    recorder = RecordingProvider(provider, budget)
    turns, initial, final_delta = [], None, None
    infrastructure, blocked = None, False
    stage, index = 'initialize', None
    try:
        validate_cases([case])
        with fixed_time(reference_time(case)), TemporaryDirectory(prefix='calendar-agent-eval-') as temporary:
            calendar = EvaluationCalendar(temporary, recorder, reference_time(case), case.get('reminder_config'))
            try:
                calendar.start()
                calendar.seed(case['initial_calendar'])
                initial = calendar.read()
                require(len(initial['events']) == len(case['initial_calendar']), 'fixture event count mismatch')
                session = calendar.service.new_session()['session_id']
                previous = {}
                before = initial
                references = {}
                for index, turn in enumerate(case['turns'], 1):
                    stage = 'interaction_gate'
                    if 'when' in turn and not gate_allows(turn['when'], previous):
                        blocked = 'alternate_clarification' if interaction(previous)=='clarification' else 'pending_lost'
                        break
                    stage = 'turn'
                    start_call = len(recorder.calls)
                    turn_started = perf_counter()
                    request = None
                    try:
                        response, request = run_turn(calendar, session, turn, index, previous, references, recorder)
                    except ChatError as error:
                        response = dict(status='failed', error_code=error.code, error_message=str(error))
                    except (TimeoutError, OSError) as error:
                        code = 'timeout' if isinstance(error, TimeoutError) else type(error).__name__
                        infrastructure = dict(turn=index, code=code, stage=stage)
                        response = dict(status='failed', error_code=code)
                    calls = recorder.calls[start_call:]
                    if response.get('error_code') in INFRA_CODES:
                        code = response['error_code']
                        timed_out = code=='planning_timeout' or any(call.get('error')=='TimeoutError' for call in calls)
                        provider_error = code in ('model_unavailable','eval_call_budget','eval_mock_exhausted')
                        infrastructure = dict(turn=index,code='timeout' if timed_out else code,
                                              stage='provider' if provider_error else stage)
                    if 'save_as' in turn and response.get('pending_operation'):
                        references[turn['save_as']] = response['pending_operation']
                    stage = 'read_persisted_state'
                    after = calendar.read()
                    delta = calendar_delta(before, after)
                    final_delta = calendar_delta(initial, after)
                    stage = 'record'
                    service = calendar.service
                    context = service.get_session(session)
                    diagnostics = service.get_diagnostics(request) if request and not infrastructure else None
                    operation = response.get('pending_operation') or {}
                    receipt = response.get('operation_result') or {}
                    op_id = operation.get('operation_id') or response.get('operation_id') or receipt.get('operation_id')
                    audit = next((row for row in service.journal.all('audit', OWNER)
                                  if row.get('operation_id') == op_id), None) if op_id else None
                    turns.append(dict(turn=index, action=turn['action'],
                                      input={key: turn[key] for key in ('text','ref','index','request_id') if key in turn},
                                      interaction=interaction(response), response=response, calls=calls,
                                      plans=normalize_plans(calls), diagnostics=diagnostics, audit=audit,
                                      pending={key: context[key] for key in ('pending','awaiting_target','awaiting_create','event_input')
                                               if context.get(key)},
                                      delta=delta, infrastructure=bool(infrastructure),
                                      seconds=round(perf_counter()-turn_started,4)))
                    previous, before = response, after
                    if infrastructure:
                        break
                stage = 'final_persisted_state'
                final_delta = calendar_delta(initial, calendar.read())
            finally:
                calendar.stop()
            stage = 'cleanup'
    except Exception as error:
        infrastructure = dict(turn=index, code=type(error).__name__, stage=stage)
    try:
        verdict, dimensions, issues = judge(case, turns, final_delta, infrastructure, blocked)
    except Exception as error:
        verdict, dimensions = 'infrastructure_error', {}
        issues = [dict(dimension='infrastructure', code=type(error).__name__, detail='judge')]
    # Usage is intentionally null: DeepSeekProvider exposes choices[0].message only.
    return redact(dict(case_id=case.get('case_id','invalid-case'), category=case.get('category','invalid'),
                       support=case.get('support','unsupported'), description=case.get('description'), tags=case.get('tags',[]),
                       expected_plan=case.get('expected_plan'), expected_interaction=case.get('expected_interaction'),
                       expected_final_state=case.get('expected_final_state'),
                       reference_datetime=case.get('reference_datetime'), timezone=case.get('timezone'),
                       attempt=attempt, verdict=verdict, dimensions=dimensions, issues=issues,
                       initial_calendar=case.get('initial_calendar'), turns=turns, final_delta=final_delta,
                       model_calls=len(recorder.calls), usage=None, cost=None,
                       seconds=round(perf_counter()-started,4)), secrets)


def verdict_counts(rows):
    counts = Counter(row['verdict'] for row in rows)
    return {key: counts[key] for key in ('pass','fail','needs_review','infrastructure_error')}


def summarize(rows):
    supported = [row for row in rows if row['support']=='supported']
    per_case = {key: verdict_counts([row for row in rows if row['case_id']==key])
                for key in sorted({row['case_id'] for row in rows})}
    for totals in per_case.values():
        attempts = sum(totals.values())
        totals['pass_rate'] = totals['pass']/attempts
        totals['varied'] = sum(totals[status]>0 for status in verdict_counts([])) > 1
    return dict(total=len(rows), verdicts=verdict_counts(rows),
                by_category={key: verdict_counts([row for row in rows if row['category']==key])
                             for key in sorted({row['category'] for row in rows})},
                by_support=dict(Counter(row['support'] for row in rows)),
                verdicts_by_support={key: verdict_counts([row for row in rows if row['support']==key])
                                     for key in sorted({row['support'] for row in rows})},
                cases_by_issue=dict(Counter(code for row in rows for code in {issue['code'] for issue in row['issues']})),
                per_case=per_case,
                supported_pass_rate=verdict_counts(supported)['pass']/len(supported) if supported else None,
                dimensions={key: dict(Counter(row['dimensions'].get(key,'not_checked') for row in rows))
                            for key in ('model','workflow','state','response')},
                multi_turn=verdict_counts([row for row in rows if len(row['turns'])>1]),
                non_action_unexpected_writes=sum(any(issue['code']=='unexpected_write' for issue in row['issues'])
                                                 for row in rows if row['category']=='non_action'),
                model_calls=sum(row['model_calls'] for row in rows),
                seconds=round(sum(row['seconds'] for row in rows),3),
                mean_calls=round(sum(row['model_calls'] for row in rows)/len(rows),3) if rows else None,
                max_attempt_seconds=max((row['seconds'] for row in rows),default=None), usage=None, cost=None)


def git_metadata():
    try:
        def git(*args):
            return subprocess.check_output(['git','-C',str(ROOT),*args],encoding='utf-8',
                                           stderr=subprocess.DEVNULL,timeout=5).strip()

        return dict(head=git('rev-parse','HEAD'), dirty=bool(git('status','--porcelain')))
    except (OSError, subprocess.SubprocessError):
        return dict(head=None,dirty=None)


def write_report(directory, rows, metadata):
    directory = Path(directory).resolve()
    require(not directory.is_relative_to(DATA_DIR.resolve()), 'report output cannot be in the user calendar profile')
    directory.mkdir(parents=True, exist_ok=False)
    summary = {**metadata, **summarize(rows)}
    (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (directory/'cases.jsonl').open('w',encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    lines = ['# Calendar Agent 评测', '', f"模式 `{metadata['mode']}` · 共 {summary['total']} 次 · "
             f"模型调用 {summary['model_calls']} 次 · 耗时 {summary['seconds']} 秒", '',
             f"通过 {summary['verdicts']['pass']} · 失败 {summary['verdicts']['fail']} · "
             f"待人工复核 {summary['verdicts']['needs_review']} · 基础设施错误 {summary['verdicts']['infrastructure_error']}", '',
             '| 类别 | 通过 | 失败 | 待人工复核 | 基础设施错误 |', '|---|---:|---:|---:|---:|']
    for category, counts in summary['by_category'].items():
        lines.append(f"| {category} | {counts['pass']} | {counts['fail']} | {counts['needs_review']} | {counts['infrastructure_error']} |")
    lines += ['', '| 支持状态 | 通过 | 失败 | 待人工复核 | 基础设施错误 |', '|---|---:|---:|---:|---:|']
    for support, counts in summary['verdicts_by_support'].items():
        lines.append(f"| {support} | {counts['pass']} | {counts['fail']} | {counts['needs_review']} | {counts['infrastructure_error']} |")
    lines += ['', '失败与复核类型（按每次尝试去重）']
    lines += [f'- `{code}` {count}' for code, count in sorted(summary['cases_by_issue'].items())]
    supported = summary['supported_pass_rate']
    lines += ['', '## 分维度结果', '', f'支持范围通过率 {supported:.1%}' if supported is not None else '本次未选支持范围案例',
              f"多轮尝试 {summary['multi_turn']}", f"非操作误写入 {summary['non_action_unexpected_writes']} 次", '']
    lines += [f"- {name} `{counts}`" for name, counts in summary['dimensions'].items()]
    lines += ['', '## 失败与复核', '']
    for row in rows:
        if row['verdict'] != 'pass':
            codes = ', '.join(item['code'] for item in row['issues'])
            lines.append(f"- `{row['case_id']}` 第 {row['attempt']} 次 · {row['verdict']} · {codes} "
                         f"· 复跑用 `--mode {metadata['mode']}{metadata.get('dataset_arg', '')} --cases {row['case_id']}`")
    if all(row['verdict']=='pass' for row in rows):
        lines.append('所选案例均通过。')
    lines += ['', '## 重跑与对比', '',
              '用相同案例 ID、数据集摘要、模式和模型配置对比 `summary.json`。'
              '逐次记录和失败轮次见 `cases.jsonl`，包含每次尝试的计划、DB 差异和耗时。',
              '待人工复核计入尝试总数。Token 和费用暂未记录，Provider 目前仅返回模型消息。',
              'Mock 结果用于验证评测器和宿主路径。真实语言表现以显式 Live 运行结果为准。']
    (directory/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('mock','live'), default='mock')
    parser.add_argument('--cases', default=None, help='all, core, category, or comma-separated case IDs')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--max-calls', type=int, help='Whole-run model call budget (Live defaults to 120)')
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--output', type=Path, default=ROOT/'build'/'calendar-agent-eval')
    parser.add_argument('--config', type=Path, help='Read-only path to the existing configuration.dpapi for Live mode')
    args = parser.parse_args(argv)
    try:
        require(1 <= args.repeat <= REPEAT_LIMIT, f'--repeat must be 1..{REPEAT_LIMIT}')
        limit = LIVE_CALLS_LIMIT if args.mode=='live' else CASES_LIMIT*REPEAT_LIMIT*12*6
        max_calls = limit if args.max_calls is None else args.max_calls
        require(1 <= max_calls <= limit, f'--max-calls must be 1..{limit}')
        require(args.config is None or args.mode == 'live', '--config is available only with --mode live')
        require(args.dataset.suffix.lower() == '.json', 'dataset must be synthetic JSON')
        require(not args.output.resolve().is_relative_to(DATA_DIR.resolve()), 'unsafe report output')
        require(not args.dataset.resolve().is_relative_to(DATA_DIR.resolve()), 'unsafe dataset path')
        dataset_bytes = args.dataset.read_bytes()
        dataset = validate_cases(json.loads(dataset_bytes))
        selections = set((args.cases or ('core' if args.mode=='live' else 'all')).split(','))
        allowed = {'all','core'} | {row[key] for row in dataset for key in ('category','case_id')}
        require(selections <= allowed, 'unknown case selection')
        selected = [row for row in dataset if 'all' in selections or row['case_id'] in selections or row['category'] in selections
                    or ('core' in selections and 'core' in row['tags'])]
        require(1 <= len(selected) <= CASES_LIMIT, f'select 1..{CASES_LIMIT} cases per run')
    except (ChatError, OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f'Invalid evaluation input ({type(error).__name__}): {error}' if type(error) is ValueError
                     else f'Invalid evaluation input ({type(error).__name__})')
    try:
        if args.mode=='live':
            config = args.config or DATA_DIR/'ai-assistant'/'configuration.dpapi'
            provider, secrets, model = live_provider(config)
        else:
            provider, secrets, model = MockProvider(), (), 'scripted'
    except (ChatError, OSError, ValueError, KeyError, TypeError):
        parser.error('Live config unusable. Select the saved configuration.dpapi with a model key and remote consent.')
    budget = [max_calls]
    rows = [run_case(case,provider,budget,attempt,secrets=secrets)
            for case in selected for attempt in range(1,args.repeat+1)]
    metadata = redact(dict(schema_version=1,mode=args.mode,model=model,git=git_metadata(),repeat=args.repeat,
                           dataset_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
                           dataset_arg='' if args.dataset.resolve()==DATASET else f' --dataset "{args.dataset}"',
                           max_calls=max_calls,selected=[row['case_id'] for row in selected]),secrets)
    destination = args.output/f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    summary = write_report(destination,rows,metadata)
    print(f"{destination} · {summary['verdicts']}")
    return int(any(
        (row['support']=='supported' and row['verdict']!='pass')
        or row['verdict']=='infrastructure_error'
        or any(issue['code']=='unexpected_write' for issue in row['issues'])
        for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
