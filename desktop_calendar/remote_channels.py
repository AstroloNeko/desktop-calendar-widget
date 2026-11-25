"""Validated channel routing and the published client's stable conversation key."""
from dataclasses import dataclass
import re

from desktop_calendar.assistant.calendar_chat_protocol import fingerprint


QQ_KEY = re.compile(r'qq:([0-9]{1,32}):([^:\s]{1,210})')
CHANNEL_NAMES = {'qq': 'QQ', 'wechat': '微信', 'unknown': '未知渠道'}


def channel_name(channel):
    return CHANNEL_NAMES.get(channel, CHANNEL_NAMES['unknown'])


def _identifier(value):
    return isinstance(value, str) and 1 <= len(value) <= 256 and value.isprintable() and not any(
        char.isspace() for char in value)


@dataclass(frozen=True)
class Conversation:
    channel: str
    key: str
    app_id: str = ''

    def owner(self, scope):
        return 'wechat-verified-' + fingerprint([scope, self.key])

    def route(self, scope):
        return fingerprint([scope, self.channel, self.app_id, self.key]) if self.app_id else ''


def conversation_from_job(job):
    legacy, key = job.get('openid'), job.get('conversation_key')
    channel, app_id = job.get('channel'), job.get('channel_app_id')
    if channel == 'wechat' and app_id == '':
        app_id = None  # Published legacy jobs can lack a configured app ID.
    if key is not None and not _identifier(key) or legacy is not None and not _identifier(legacy):
        raise ValueError('无效的会话标识。')
    if key is not None and legacy is not None and key != legacy:
        raise ValueError('会话标识与兼容标识不一致。')
    key = key or legacy
    if not key:
        raise ValueError('缺少会话标识。')
    if app_id is not None and not _identifier(app_id):
        raise ValueError('无效的渠道应用标识。')
    qq = QQ_KEY.fullmatch(key)
    if channel == 'qq':
        if not qq or not app_id or app_id != qq[1]:
            raise ValueError('QQ 会话与渠道应用不一致。')
        return Conversation(channel, key, app_id)
    if channel == 'wechat':
        if key.startswith('qq:'):
            raise ValueError('会话与渠道不一致。')
        return Conversation(channel, key, app_id or '')
    if channel is None and app_id is None and legacy is not None:
        return Conversation('qq' if qq else 'unknown', key)
    raise ValueError('不支持的消息渠道。')
