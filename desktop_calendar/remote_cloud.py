"""Cloud contract boundary for activation and short-lived notification credentials."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.parse import quote, urlsplit

from desktop_calendar.assistant.calendar_chat_protocol import fingerprint
from desktop_calendar.remote_interaction import CAPABILITY_REQUEST, LEGACY_CAPABILITIES, capabilities_for
from desktop_calendar.remote_transport import Client, StatusError, Unavailable, normalize_url, valid_token


def valid_account(account):
    return (isinstance(account, str) and 0 < len(account) <= 256
            and all(char.isprintable() and not char.isspace() for char in account))


def display_name(value):
    if isinstance(value, str):
        value = value.strip()
        if value and value.isprintable():
            return value[:64]
    return ''


def remote_scope(url, token, account='', legacy_credential=''):
    if account:
        return fingerprint(['wechat-account', url, account])
    credential = fingerprint([url, token])
    return fingerprint(url) if credential == legacy_credential else fingerprint(['wechat-manual', credential])


class AccountMismatch(ValueError):
    """The authenticated server identified another account for this Worker Token."""


@dataclass(frozen=True)
class Binding:
    account_id: str = field(repr=False)
    worker_token: str = field(repr=False)
    display_name: str = field(default='', repr=False)

    def __post_init__(self):
        if not valid_account(self.account_id) or not valid_token(self.worker_token):
            raise ValueError('绑定响应格式无效。')


@dataclass(frozen=True)
class NotificationSession:
    stream_url: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_at: float
    refresh_at: float = None
    account_id: str = field(default='', repr=False)
    display_name: str = field(default='', repr=False)
    interaction_version: int = 0
    interaction_capabilities: tuple = LEGACY_CAPABILITIES
    proactive_enabled: bool = False
    proactive_actions: tuple = ()

    def __post_init__(self):
        try:
            parts = urlsplit(self.stream_url)
            host = parts.hostname or ''
            safe_url = (parts.scheme == 'https' and parts.port in (None, 443)
                        and host.endswith(('.firebaseio.com', '.firebasedatabase.app'))
                        and parts.username is None and parts.path.endswith('.json')
                        and len(parts.path) > len('/.json')
                        and not any(char.isspace() or char in '\\?#' for char in self.stream_url))
        except (ValueError, TypeError):
            safe_url = False
        if not safe_url or not isinstance(self.id_token, str) or not 0 < len(self.id_token) <= 8192:
            raise ValueError('通知会话格式无效。')
        if any(not 33 <= ord(char) <= 126 for char in self.id_token):
            raise ValueError('通知会话格式无效。')
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise ValueError('通知会话格式无效。')
        if self.refresh_at is not None and (
                type(self.refresh_at) not in (int, float) or not math.isfinite(self.refresh_at)
                or self.refresh_at > self.expires_at):
            raise ValueError('通知会话格式无效。')


def firebase_origin(url):
    origin = normalize_url(url)
    parts = urlsplit(origin)
    if parts.port is not None or not parts.hostname.endswith(('.firebaseio.com', '.firebasedatabase.app')):
        raise ValueError('通知数据库地址无效。')
    return origin


def _notification_seconds(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,5}', value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 3600:
        raise ValueError('通知会话有效期无效。')
    return value


class FirebaseNotificationContract:
    """Cloud Run issues a short-lived ID token for exactly one wakeup path."""

    def notification_session(self, client, token, expected_account=''):
        data = client.request('/api/notifications/session', token, CAPABILITY_REQUEST)
        account = data.get('user_id')
        if expected_account and account != expected_account:
            raise AccountMismatch('通知账户与本机绑定账户不符。')
        if (data.get('provider') != 'firebase_rtdb_sse' or not valid_account(account)
                or any(char in '.$#[]/' for char in account)
                or data.get('wakeup_path') != f'/wakeups/{account}/version'
                or data.get('id_token') == token):
            raise ValueError('通知会话格式无效。')
        origin = firebase_origin(data.get('database_url'))
        ttl = _notification_seconds(data.get('expires_in'))
        refresh_before = _notification_seconds(data.get('recommended_refresh_before_seconds'))
        expires_at = data.get('expires_at_utc')
        if not isinstance(expires_at, str):
            raise ValueError('通知会话有效期无效。')
        expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if expiry.utcoffset() is None:
            raise ValueError('通知会话有效期无效。')
        remaining = min(ttl, (expiry - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            raise ValueError('通知会话已过期。')
        now = time.monotonic()
        refresh_after = remaining - (refresh_before if remaining > refresh_before else remaining / 2)
        stream_url = f"{origin}/wakeups/{quote(account, safe='')}/version.json"
        version = data.get('interaction_schema_version')
        version = version if type(version) is int and version in (1, 2) else 0
        advertised = data.get('interaction_capabilities', LEGACY_CAPABILITIES)
        capabilities = (tuple(cap for cap in capabilities_for(version) if cap in advertised)
                        if version and isinstance(advertised, (list, tuple)) else ())
        proactive = data.get('proactive_notifications')
        enabled = (version == 2 and isinstance(proactive, dict) and proactive.get('enabled') is True
                   and type(proactive.get('protocol_version')) is int and proactive['protocol_version'] == 1
                   and proactive.get('endpoint') == '/api/notifications/proactive'
                   and isinstance(proactive.get('supported_actions'), list))
        actions = tuple(action for action in ('acknowledge','view_items','snooze_10m','view_today','view_week','view_ddl')
                        if action in proactive['supported_actions']) if enabled and 'proactive_action' in capabilities else ()
        return NotificationSession(stream_url, data.get('id_token'), now + remaining, now + refresh_after, account,
                                   display_name(data.get('display_name')), version, capabilities, enabled, actions)


NOTIFICATION_CONTRACT = FirebaseNotificationContract()


def activate_binding(url, code):
    if not isinstance(code, str) or len(code) > 256:
        raise ValueError('请填写有效的客户端激活码。')
    code = code.strip()
    normalized = re.sub(r'[\s-]+', '', code.upper())
    if not normalized or not normalized.isprintable():
        raise ValueError('请填写有效的客户端激活码。')
    body = json.dumps({'activation_code': code}, ensure_ascii=False).encode('utf-8')
    try:
        data = Client(url).request('/api/pairing/activate', body=body)
    except StatusError as error:
        messages = {
            400: '激活码无效、已使用或已过期。请在微信公众号获取新的激活码。',
            403: '该微信账户已停用。',
            429: '绑定尝试次数过多，请稍后再试。',
        }
        raise ValueError(messages.get(error.status, f'绑定服务暂不可用（HTTP {error.status}）。')) from None
    except Unavailable:
        raise ValueError('未收到有效的绑定响应。服务器可能已使用激活码，请核对后再试。') from None
    return Binding(data.get('user_id'), data.get('worker_token'), display_name(data.get('display_name')))
