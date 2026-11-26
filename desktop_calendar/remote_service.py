"""Private WeChat input adapter. Calendar access stays behind CalendarChatService."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import threading
import time
from typing import Optional

from desktop_calendar.assistant.calendar_chat_intent import requested_fields
from desktop_calendar.assistant.calendar_chat_protocol import (
    ChatError, calendar_write_guard, event_line, fingerprint, receipt_message, reminder_line, wechat_delete_review,
)
from desktop_calendar.assistant.operation_engine import event_action
from desktop_calendar.remote_channels import conversation_from_job
from desktop_calendar.remote_cloud import NOTIFICATION_CONTRACT, remote_scope, valid_account
from desktop_calendar.remote_history import describe_response
from desktop_calendar.remote_interaction import (
    CAPABILITY_REQUEST, JOBS, UNDO_REPLIES, VERSION as INTERACTION_VERSION, build_business_receipt, execute_interaction,
    execute_undo_reply, interaction_from_job,
)
from desktop_calendar.remote_notifications import ACCOUNT_MISMATCH, AUTH_REJECTED, NotificationListener, VerifiedAccount
from desktop_calendar.remote_transport import (
    CLAIM_PATH, INTERVAL, SAFETY_CHECK_MINUTES, Client, StatusError, Unavailable, normalize_url, transient, valid_token,
)


CLAIM_INCIDENT = 'wechat-claim-incident'
CHINA_TIME = timezone(timedelta(hours=8))
MSG_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
UNCERTAIN = '有任务执行结果待人工核查，已保留台账。'
SSE_FALLBACK_GRACE = 45


@dataclass(frozen=True)
class RemoteCompletion:
    processed_at: float
    result: str
    model_unavailable: bool


@dataclass(frozen=True)
class RemoteStatus:
    connection: str = 'disabled'
    processing_id: str = ''
    history_revision: int = 0
    request_failed: bool = False
    completion: Optional[RemoteCompletion] = None
    last_checked_at: Optional[float] = None
    check_result: str = ''
    account_verified: bool = False
    account_name: str = field(default='', repr=False)


def test_connection(url):
    return Client(url).request('/').get('status') == 'ok'


def claim_checked_at(response, received_at):
    checked_at = response.get('checked_at')
    if isinstance(checked_at, str):
        try:
            parsed = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
            if parsed.utcoffset() is not None:
                stamp = parsed.timestamp()
                datetime.fromtimestamp(stamp)
                return stamp
        except (ValueError, OverflowError, OSError):
            pass
    return received_at


def message_time(job):
    if job.get('created_at_ts') is not None:
        stamp = job['created_at_ts']
        if type(stamp) is int and stamp > 0:
            try:
                return datetime.fromtimestamp(stamp, CHINA_TIME)
            except (ValueError, OverflowError, OSError):
                pass
        raise ValueError('消息发送时间戳无效。')
    local = job.get('message_time_local')
    if isinstance(local, str):
        try:
            parsed = datetime.fromisoformat(local.replace('Z', '+00:00'))
            if parsed.utcoffset() is not None:
                return parsed.astimezone(CHINA_TIME)
        except (ValueError, OverflowError):
            pass
    stamp = job.get('wechat_create_time')
    if type(stamp) is int:
        try:
            return datetime.fromtimestamp(stamp, CHINA_TIME)
        except (ValueError, OverflowError, OSError):
            pass
    raise ValueError('缺少有效的消息发送时间。')


def make_receipt(response, request_text='', *, business=None):
    status = response['status']
    op = response.get('pending_operation') or {}
    task = response.get('task') or {}
    result = response.get('operation_result') or {}
    preview = op.get('preview', {})
    reminder_text = (response.get('plain_reminder') or preview.get('object_type') == 'reminder'
                     or result.get('action') == 'reminder_action')
    if reminder_text:
        business = None
    ok = status in ('ok', 'succeeded', 'completed', 'clarification_required',
                    'confirmation_required', 'cancelled', 'needs_clarification', 'pending')
    error = None if ok else response.get('error_code') or status
    prompt = response.get('assistant_message') if ok else response.get('error_message') or response.get('assistant_message')
    rows = [] if reminder_text else response.get('candidates') or response.get('habit_candidates') or []
    instruction = ''
    if status in ('ok', 'succeeded', 'completed') and result.get('goal_satisfied') is True and result.get('action') in (
            'create', 'update', 'complete', 'delete'):
        explicit = requested_fields(request_text) | set(
            preview.get('field_provenance', {}).get('explicit', preview.get('requested_fields', ())))
        prompt = receipt_message(result, concise=True, explicit=explicit) or prompt
    if not reminder_text and op.get('status') == 'pending' and status in ('confirmation_required', 'pending'):
        if preview.get('before') and event_action(op) == 'delete':
            rows = []
            prompt = wechat_delete_review(op) or '删除清单过长，请回复“取消”，缩小范围后再预览。'
        else:
            summary = preview.get('after_summary') or preview.get('summary')
            rows = preview.get('summaries') or ([summary] if summary else [])
            snapshot = preview.get('after') or preview.get('before')
            if not rows and isinstance(snapshot, dict):
                days = preview.get('dates') or []
                due = snapshot.get('due', '')
                rows = [{**snapshot, 'date': days[0] if days else due[:10],
                         'end': days[-1] if days else '',
                         'time': due[11:16] if snapshot.get('has_time') else '',
                         'schedule': preview.get('schedule')}]
            instruction = '核对后回复“确认”或“取消”。'
    lines = [prompt or ('本条消息已处理。' if ok else '本条消息未完成，请核查日历。')]
    for index, row in enumerate(rows, 1):
        if row.get('date') and row.get('title') and not row.get('schedule'):
            detail = reminder_line(row)
            lines.append(f'{index}. {event_line(row)}' + (' · '+detail if detail else ''))
            continue
        date = row.get('date') or row.get('start', '')[:10]
        if row.get('end') and row['end'] != date:
            date += ' 至 ' + row['end']
        detail = ' · '.join(str(part) for part in (date, row.get('time'), row.get('title'), row.get('schedule')) if part)
        lines.append(f'{index}. {detail}')
    if instruction:
        lines.append(instruction)
    reply = '\n'.join(lines)
    encoded = reply.encode('utf-8')
    if len(encoded) > 1900:
        suffix = '\n…' + ('\n' + instruction if instruction else '')
        reply = encoded[:1900-len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore') + suffix
    ids = list(dict.fromkeys(identifier for identifier in (
        result.get('operation_id'), result.get('task_id'), response.get('operation_id'),
        op.get('operation_id'), task.get('operation_id'),
    ) if identifier))
    operation_id = ids[0] if ids and (result or status == 'committed_unverified') and status != 'pending' else None
    body = dict(ok=ok, reply_text=reply, operation_id=operation_id, error=error)
    if business is not None:
        body['business_receipt'] = business
    return json.dumps(body, ensure_ascii=False, separators=(',', ':')), ids


class RemoteWorker:
    """One queue worker with durable intake and an immutable completion outbox."""

    def __init__(self, service, journal, *, client_factory=Client, interval=INTERVAL, contract=None,
                 notification_factory=NotificationListener, safety_interval=None,
                 fallback_grace=SSE_FALLBACK_GRACE, on_status_change=None, pin_account=None):
        self.service, self.journal = service, journal
        self.agent_journal = str(service.journal.path.resolve())
        self.client_factory, self.interval = client_factory, interval
        self.condition = threading.Condition()
        self.thread = None
        self.config = None
        self.generation = 0
        self.wake_serial = 0
        self.notification_online = False
        self.had_live_connection = False
        self.offline_since = None
        self.auth_error = None
        self.account_verified = False
        self.account_name = ''
        self.interaction_version = 0
        self.interaction_capabilities = ()
        self.last_cloud_ok = None
        self.cloud_unavailable = False
        self.processing_id = ''
        self.history_revision = 0
        self.completion = None
        self.last_checked_at = None
        self.check_result = ''
        self.on_status_change = on_status_change
        self._notified_status = None
        self.stopped = False
        self.status = '远程服务已关闭。'
        self.notice = ''
        self.notification_status = ''
        self.safety_interval = None
        self.safety_interval_override, self.fallback_grace = safety_interval, fallback_grace
        self.pin_account = pin_account
        self.notifications = notification_factory(contract or NOTIFICATION_CONTRACT, client_factory, self._notification)

    def configure(self, config):
        active = None
        safety_interval = None
        if config.get('remote_enabled') is True:
            url, token = config.get('remote_url'), config.get('remote_token')
            if not valid_token(token):
                raise ValueError('缺少有效的 Worker Token。')
            url = normalize_url(url)
            account = config.get('remote_account', '')
            if account != '' and not valid_account(account):
                raise ValueError('绑定账户格式无效。')
            minutes = config.get('remote_safety_check_minutes', 0)
            if type(minutes) is not int or minutes not in SAFETY_CHECK_MINUTES:
                raise ValueError('远程服务兜底检查间隔无效。')
            if minutes:
                safety_interval = self.safety_interval_override or minutes * 60
            active = (url, token, remote_scope(url, token, account, config.get('remote_legacy_credential', '')), account)
        with self.condition:
            if self.stopped:
                return
            if self.safety_interval != safety_interval:
                self.safety_interval = safety_interval
                self.condition.notify_all()
            if self.config == active:
                return
            self.config = active
            self.generation += 1
            self.status = '正在核验绑定账号。' if active else '远程服务已关闭。'
            self.notice = ''
            self.notification_online = False
            self.had_live_connection = False
            self.auth_error = None
            self.account_verified = False
            self.account_name = ''
            self.interaction_version = 0
            self.interaction_capabilities = ()
            self.last_cloud_ok = None
            self.cloud_unavailable = False
            self.processing_id = ''
            self.completion = None
            self.last_checked_at = None
            self.check_result = ''
            self.offline_since = time.monotonic()
            self.notification_status = '正在连接实时通知。' if active and self.notifications else ''
            if self.notifications:
                self.notifications.configure(self.generation, *((url, token, account) if active else ()))
            if active and self.thread is None:
                self.thread = threading.Thread(target=self._run, name='calendar-wechat-worker', daemon=True)
                self.thread.start()
            self.condition.notify_all()
            self._notify_status_change()

    def request_check(self):
        """Wake the queue worker using the saved connection, if enabled."""
        with self.condition:
            if not self._current(self.generation):
                return False
            self.check_result = 'checking'
            self.wake_serial += 1
            self.condition.notify_all()
            return True

    def stop(self):
        with self.condition:
            self.stopped = True
            self.config = None
            self.account_verified = False
            self.account_name = ''
            self.interaction_version = 0
            self.interaction_capabilities = ()
            self.check_result = ''
            self.status = '远程服务已停止。'
            self.notification_status = ''
            if self.notifications:
                self.notifications.stop()
            self.condition.notify_all()
            self._notify_status_change()

    def _configured(self, generation):
        return (not self.stopped and self.generation == generation and self.config is not None
                and self.auth_error is None)

    def _current(self, generation):
        with self.condition:
            return self._configured(generation) and self.account_verified

    def snapshot(self):
        with self.condition:
            if self.stopped or self.config is None:
                connection = 'disabled'
            elif self.auth_error:
                connection = 'mismatch' if self.auth_error == ACCOUNT_MISMATCH else 'invalid'
            elif not self.account_verified:
                connection = 'verifying'
            elif self.notification_online:
                connection = 'live'
            elif self.cloud_unavailable:
                connection = 'unavailable'
            elif not self.notifications or time.monotonic() - self.offline_since >= self.fallback_grace:
                backup_since = self.offline_since + (self.fallback_grace if self.notifications else 0)
                if self.last_cloud_ok is not None and self.last_cloud_ok >= backup_since:
                    connection = 'backup'
                else:
                    connection = 'reconnecting' if self.had_live_connection else 'connecting'
            else:
                connection = 'reconnecting' if self.had_live_connection else 'connecting'
            return RemoteStatus(connection, self.processing_id, self.history_revision, self.cloud_unavailable,
                                self.completion, self.last_checked_at, self.check_result, self.account_verified,
                                self.account_name)

    def _notify_status_change(self):
        if self.on_status_change is None:
            return
        snapshot = self.snapshot()
        status = (snapshot.connection, snapshot.request_failed, snapshot.completion, snapshot.last_checked_at,
                  snapshot.account_name)
        if status != self._notified_status:
            self._notified_status = status
            self.on_status_change()

    def _remember_completion(self, record):
        when = record.get('processed_at')
        result = record.get('history_result')
        if when and result and (self.completion is None or when > self.completion.processed_at):
            self.completion = RemoteCompletion(when, result, record.get('model_unavailable') is True)
            self._notify_status_change()

    def _reject_authentication(self, generation, reason=AUTH_REJECTED, *, check_failed=False):
        with self.condition:
            if self._configured(generation):
                self.auth_error = reason
                self.account_verified = False
                self.account_name = ''
                self.interaction_version = 0
                self.interaction_capabilities = ()
                self.notification_online = False
                if check_failed or self.check_result == 'checking':
                    self.check_result = 'failed'
                self.status = ('服务器账号与本机绑定不符。' if reason == ACCOUNT_MISMATCH
                               else '远程服务绑定已失效。')
                self.notification_status = ''
                if self.notifications:
                    self.notifications.configure(generation)
                self.condition.notify_all()
                self._notify_status_change()

    def _cloud_status(self, generation, ok, *, check_result='', checked_at=None):
        with self.condition:
            if self._current(generation):
                self.cloud_unavailable = not ok
                if ok:
                    self.last_cloud_ok = time.monotonic()
                if checked_at is not None:
                    self.last_checked_at = checked_at
                if check_result:
                    self.check_result = check_result
                self._notify_status_change()

    def _publish(self, generation, status=None, notice=None):
        with self.condition:
            if self._current(generation):
                if status is not None:
                    self.status = status
                if notice is not None:
                    self.notice = notice

    def _verify_account(self, generation, profile):
        account = profile.account_id
        if not valid_account(account):
            return False
        with self.condition:
            if not self._configured(generation):
                return False
            url, token, _, expected = self.config
            if expected:
                if expected != account:
                    self._reject_authentication(generation, ACCOUNT_MISMATCH)
                    return False
                self.account_name = profile.display_name
                self.interaction_version = profile.interaction_version
                self.interaction_capabilities = (profile.interaction_capabilities
                                                 if profile.interaction_version == INTERACTION_VERSION else ())
                self.account_verified = True
                self.condition.notify_all()
                self._notify_status_change()
                return True
        if self.pin_account:
            self.pin_account(generation, url, token, account)
        # An unbound manual credential is pinned before starting a fresh generation.
        return False

    def _notification(self, generation, online):
        if online in (AUTH_REJECTED, ACCOUNT_MISMATCH):
            self._reject_authentication(generation, online)
            return
        if isinstance(online, VerifiedAccount):
            return self._verify_account(generation, online)
        with self.condition:
            if not self._current(generation):
                return
            if online:
                self.had_live_connection = True
                self.notification_status = '实时通知已连接。'
                self.wake_serial += 1
            elif self.notification_online:
                self.notification_status = '正在重连实时通知。'
                self.offline_since = time.monotonic()
            self.notification_online = online
            self.condition.notify_all()
            self._notify_status_change()

    def _wait_idle(self, generation, seen):
        with self.condition:
            while self._current(generation) and self.wake_serial == seen:
                online = self.notification_online
                safety_interval = self.safety_interval
                delay = self.interval
                if self.notifications:
                    grace = self.fallback_grace - (time.monotonic() - self.offline_since)
                    if online:
                        delay = safety_interval
                    elif grace > 0:
                        delay = grace
                    else:
                        self.notification_status = '实时通知暂不可用，正在备用轮询。'
                if not self.condition.wait_for(
                        lambda: not self._current(generation) or self.wake_serial != seen or
                        self.notification_online != online or (online and self.safety_interval != safety_interval),
                        timeout=delay):
                    return

    def _run(self):
        previous = None
        unrecorded = None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self._current(self.generation))
                if self.stopped:
                    return
                generation, (url, token, server, account) = self.generation, self.config
                seen = self.wake_serial
            idle = checking_queue = False
            try:
                if generation != previous:
                    if unrecorded:
                        self._claim_incident(unrecorded[1], previous)
                        unrecorded = None
                    client = self.client_factory(url)
                    rows = self.journal.all(JOBS, server)
                    with self.condition:
                        if self._current(generation):
                            verified = (row for row in rows if row.get('verified_scope') == server)
                            self._remember_completion(max(verified, key=lambda row: row.get('processed_at', 0), default={}))
                    backlog = deque(row for row in sorted(rows, key=lambda row: row['received_at'])
                                    if row['state'] != 'submitted' and row.get('verified_scope') == server)
                    claim_uncertain = False
                    previous = generation
                    try:
                        incident = self.journal.get(CLAIM_INCIDENT, server, server)
                    except ChatError:
                        pass
                    else:
                        self._publish(generation, notice=f"上次领取待核查（UTC {incident['at']}），请检查云端 processing。")
                if unrecorded:
                    job, original_server = unrecorded
                    with self.condition:
                        if not self._current(generation):
                            continue
                        backlog.append(self._intake(job, original_server))
                        unrecorded = None
                if backlog:
                    if self._handle_active(backlog[0], client, token, server, generation):
                        backlog.popleft()
                        if self.notifications:
                            continue
                elif self.service.gateway.dispatch(self.service.gateway.writable) and self._current(generation):
                    if claim_uncertain:
                        checking_queue = True
                        if client.request('/').get('status') != 'ok':
                            raise Unavailable
                        claim_uncertain = False
                        checking_queue = False
                    if self._current(generation):
                        self.journal.check_capacity()
                        if not self._current(generation):
                            continue
                        checking_queue = True
                        try:
                            response = client.request(CLAIM_PATH, token, CAPABILITY_REQUEST)
                            received_at = time.time()
                            job = response['job']
                            if job is not None and not self._identified(job):
                                raise ValueError('领取响应缺少有效 MsgId。')
                        except (Unavailable, KeyError, ValueError):
                            claim_uncertain = True
                            self._claim_incident(server, generation)
                            raise
                        except StatusError as error:
                            claim_uncertain = transient(error.status)
                            if claim_uncertain:
                                self._claim_incident(server, generation)
                            raise
                        with self.condition:
                            if not self._current(generation):
                                if job is not None:
                                    self._claim_incident(server, generation)
                                continue
                            if job is not None and 'owner_id' in job and job['owner_id'] != account:
                                self._reject_authentication(generation, ACCOUNT_MISMATCH, check_failed=True)
                                self._claim_incident(server, generation)
                                continue
                            idle = job is None
                            self._cloud_status(generation, True, check_result='empty' if idle else 'claimed',
                                               checked_at=claim_checked_at(response, received_at))
                            checking_queue = False
                            if job is not None:
                                # Keep the decoded job until intake succeeds for this generation.
                                unrecorded = (job, server)
                                backlog.append(self._intake(job, server))
                                unrecorded = None
                        self._publish(generation, 'Worker 已认证，等待消息。' if job is None else '已领取远程消息。')
                        if backlog and self._handle_active(backlog[0], client, token, server, generation):
                            backlog.popleft()
                            if self.notifications:
                                continue
            except StatusError as error:
                if error.status in (401, 403):
                    self._reject_authentication(generation, check_failed=checking_queue)
                else:
                    self._cloud_status(generation, False, check_result='failed' if checking_queue else '')
                    self._publish(generation, f'服务返回 HTTP {error.status}，等待下次轮询。')
            except (Unavailable, KeyError, ValueError, OSError, sqlite3.Error):
                if checking_queue:
                    self._cloud_status(generation, False, check_result='failed')
                self._publish(generation, '远程连接或本地台账暂不可用。')
            except Exception:
                self._publish(generation, '远程处理暂不可用，请检查本地台账。')
            if idle:
                self._wait_idle(generation, seen)
            else:
                with self.condition:
                    self.condition.wait_for(lambda: not self._current(generation), timeout=self.interval)

    def _claim_incident(self, server, generation):
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self._publish(generation, notice=f'领取结果待核查（UTC {stamp}），请检查云端 processing。')
        self.journal.put(CLAIM_INCIDENT, server, server, {'at': stamp})

    @staticmethod
    def _identified(job):
        return isinstance(job, dict) and isinstance(job.get('msg_id'), str) and MSG_ID.fullmatch(job['msg_id'])

    def _intake(self, job, server):
        msg_id = job['msg_id']
        identifier = fingerprint([server, msg_id])
        try:
            old = self.journal.get(JOBS, identifier, server)
        except ChatError as error:
            if error.code != 'authentication_failed':
                raise
        else:
            if old.get('owner'):
                try:
                    conversation = conversation_from_job(job)
                    if (conversation.owner(server) != old['owner'] or
                            old.get('route') and conversation.route(server) != old['route']):
                        raise ValueError('duplicate route mismatch')
                except ValueError:
                    self._reject_authentication(self.generation, ACCOUNT_MISMATCH)
            return old
        record = dict(id=identifier, msg_id=msg_id, request_id='wechat-' + identifier,
                      state='received', received_at=time.time(), agent_journal=self.agent_journal)
        if self.account_verified and self.config and self.config[2] == server:
            record['verified_scope'] = server
        try:
            conversation = conversation_from_job(job)
            route = conversation.route(server)
            record.update(channel=conversation.channel, owner=conversation.owner(server), route=route)
            capabilities = (self.interaction_capabilities
                            if route and self.config and job.get('owner_id') == self.config[3] else ())
            if 'business_receipt' in capabilities:
                record['interaction_version'] = INTERACTION_VERSION
            if 'interaction' in job:
                interaction = interaction_from_job(job['interaction'])
                if interaction['kind'] not in capabilities:
                    raise ValueError('服务器未启用远程交互。')
                record['interaction'] = interaction
            kind = (record.get('interaction') or {}).get('kind')
            text = job.get('text', '')
            if not isinstance(text, str) or len(text.strip()) > 4000 or not (
                    kind in ('action', 'quick_query') or text.strip()):
                raise ValueError
            record.update(text=text, reference_now=message_time(job).isoformat())
        except (ValueError, KeyError):
            self._set_receipt(record, dict(status='failed', error_code='invalid_job',
                                           error_message='远程消息格式、会话或发送时间无效。'))
        self._save(server, record)
        return record

    def _save(self, server, record):
        self.journal.put(JOBS, record['id'], server, record)
        with self.condition:
            self.history_revision += 1
            if self.config and self.config[2] == server and record.get('verified_scope') == server:
                self._remember_completion(record)

    def _handle_active(self, record, client, token, server, generation):
        with self.condition:
            if not self._current(generation):
                return False
            self.processing_id = record['id']
        try:
            return self._handle(record, client, token, server, generation)
        finally:
            with self.condition:
                if self.generation == generation:
                    self.processing_id = ''

    def _set_receipt(self, record, response):
        business = None
        preview = (response.get('pending_operation') or {}).get('preview', {})
        runtime = ((response.get('operation_result') or {}).get('action') == 'reminder_action'
                   or preview.get('object_type') == 'reminder' or response.get('plain_reminder'))
        if record.get('interaction_version') == INTERACTION_VERSION and not runtime:
            try:
                business, record['interaction_reply'] = build_business_receipt(self.service, record, response)
            except (ChatError, KeyError, ValueError, TypeError, OSError, sqlite3.Error):
                # Optional rendering cannot make a committed operation fail or change the text receipt.
                pass
        body, ids = make_receipt(response, record.get('text', ''), business=business)
        record.update(state='ready', receipt=body, operation_ids=ids)
        record.update(history_result=describe_response(response), processed_at=time.time(),
                      model_unavailable=response.get('error_code') == 'model_unavailable')
        record.pop('text', None)
        record.pop('interaction', None)

    def _handle(self, record, client, token, server, generation):
        if not self._current(generation):
            return False
        # Read the durable state again if a previous iteration failed to save.
        record = self.journal.get(JOBS, record['id'], server)
        if record.get('verified_scope') != server:
            self._publish(generation, notice='旧任务缺少账号核验记录，已保留本地台账。')
            return True
        state = record['state']
        if state == 'received':
            if record['agent_journal'] != self.agent_journal:
                record['state'] = 'uncertain'
                self._save(server, record)
                self._publish(generation, notice=UNCERTAIN)
                return True
            if not self.service.gateway.dispatch(self.service.gateway.writable) or not self._current(generation):
                return False
            with self.service.lock:
                if not self._current(generation):
                    return False
                self.service.journal.check_capacity()
                owner = record['owner']
                sessions = self.service.list_sessions(owner)
                session = sessions[0] if sessions else self.service.new_session(owner)
                record.update(state='executing', session_id=session['session_id'])
                self._save(server, record)
                if not self._current(generation):
                    record['state'] = 'received'
                    self._save(server, record)
                    return False
                try:
                    with calendar_write_guard(lambda: self._current(generation)):
                        reference_now = datetime.fromisoformat(record['reference_now'])
                        undo_reply = (record.get('interaction_version') == INTERACTION_VERSION and
                                      record['text'].strip(' 。！!') in UNDO_REPLIES)
                        interaction = record.get('interaction')
                        if interaction or undo_reply:
                            kind = interaction['kind'] if interaction else 'action'
                            with self.condition:
                                capability = self._current(generation) and kind in self.interaction_capabilities
                            if not capability:
                                raise ChatError('action_expired', '远程交互已停用，请发送文字指令。')
                            execute = execute_interaction if record.get('interaction') else execute_undo_reply
                            response = execute(self.service, self.journal, server, record, reference_now)
                        else:
                            response = self.service.send_message(
                                record['session_id'], record['text'], record['request_id'], owner, 'wechat',
                                reference_now=reference_now,
                            )
                except ChatError as error:
                    if error.code in ('service_stopped', 'service_unavailable'):
                        raise
                    response = dict(status='failed', error_code=error.code, error_message=str(error))
            if response.get('error_code') != 'execution_uncertain' and response['status'] != 'uncertain':
                self._set_receipt(record, response)
            self._save(server, record)
        if record['state'] in ('executing', 'uncertain'):
            response = None
            if record['agent_journal'] == self.agent_journal and record.get('session_id'):
                response = self.service.recover_message(record['session_id'], record['request_id'], record['owner'])
            if response is None:
                record['state'] = 'uncertain'
                self._save(server, record)
                self._publish(generation, notice=UNCERTAIN)
                return True
            self._set_receipt(record, response)
            self._save(server, record)
        if not self._current(generation):
            return False
        return self._upload(record, client, token, server, generation)

    def _upload(self, record, client, token, server, generation):
        path = '/api/jobs/' + record['msg_id'] + '/complete'
        if record.get('verified_scope') != server or not self._current(generation):
            return False
        try:
            result = client.request(path, token, record['receipt'].encode('utf-8'))
        except StatusError as error:
            if error.status in (401, 403) or transient(error.status):
                raise
            self._publish(generation, notice=f'服务拒绝回执（HTTP {error.status}），已保留原回执。')
            return True
        except Unavailable:
            self._cloud_status(generation, False)
            self._publish(generation, '回执待提交，15 秒后重传。')
            return False
        receipt = json.loads(record['receipt'])
        expected_status = 'done' if receipt['ok'] else 'failed'
        if (result.get('status') != expected_status or result.get('exec_ok') is not receipt['ok']
                or not isinstance(result.get('already_completed'), bool)
                or result.get('operation_id') != receipt['operation_id']):
            self._publish(generation, notice='云端回执与本地记录不符，已保留原回执。')
            return True
        with self.condition:
            if not self._current(generation):
                return False
            self._cloud_status(generation, True)
            notify = result.get('notify_status')
            record.update(state='submitted', notify_status=notify if notify in ('sent', 'failed', 'pending', 'skipped') else 'unknown')
            self._save(server, record)
        if notify != 'sent':
            self._publish(generation, notice='回执已保存，通知未确认送达。')
        self._publish(generation, 'Worker 已认证，回执已提交。')
        return True
