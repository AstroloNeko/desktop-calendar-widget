"""Worker-Token transport for the proposed Antigravity proactive notice endpoint."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import queue
import threading
import uuid

from desktop_calendar.remote_transport import Client, StatusError, Unavailable
from desktop_calendar.reminders.notification_model import Notice, identity

PATH = "/api/notifications/proactive"
ACTIONS = {
    "event": ("acknowledge", "view_items", "snooze_10m"),
    "ddl": ("acknowledge", "view_items", "snooze_10m"),
    "brief": ("view_today", "view_week", "view_ddl"),
    "test": (),
}


@dataclass(frozen=True)
class VerifiedBinding:
    worker: object = field(repr=False)
    generation: int
    url: str
    token: str = field(repr=False)
    account: str
    supported_actions: tuple = ()

    @property
    def scope(self):
        return identity(self.url, self.account, self.token)

    @property
    def account_scope(self):
        return identity(self.url, self.account)


def notice_payload(notice, binding, private):
    if private and notice.kind != "test":
        fallback = (f"共 {len(notice.items)} 项，打开桌面月历查看。"
                    if notice.items else "本次简报没有事项。")
    else:
        fallback = notice.summary(len(notice.items))
    wire_items = [] if private else [
        {**{key: item[key] for key in ("title", "group", "date", "time") if key in item}, "index": index}
        for index, item in enumerate(notice.items, 1)
    ]
    return {
        "protocol_version": 1, "notification_id": notice.key, "type": notice.kind,
        "account_id": binding.account, "channel": "qq",
        "triggered_at": notice.when.astimezone().isoformat(),
        "expires_at": notice.expires.astimezone().isoformat(),
        "private": private, "count": len(notice.items),
        "items": wire_items,
        "fallback_text": ("【测试简报】\n" + fallback) if notice.manual_test else fallback,
        **({"is_test": True} if notice.manual_test else {}),
        "actions": [{"action": action, "target": notice.key} for action in ACTIONS[notice.kind]
                    if not notice.manual_test and action in binding.supported_actions and (not private or action == "acknowledge")],
    }


class QQReminderBackend:
    def __init__(self, get_worker, client_factory=Client):
        self.get_worker, self.client_factory = get_worker, client_factory
        self.jobs, self.events = queue.Queue(), queue.Queue()
        self.thread = None
        self.lock = threading.Lock()
        self.cancelled = set()
        self.closed = False
        self.private = False

    def binding(self):
        worker = self.get_worker()
        if worker is not None:
            with worker.condition:
                if worker._current(worker.generation):
                    url, token, _scope, account = worker.config
                    if account:
                        return VerifiedBinding(worker, worker.generation, url, token, account)
        return None

    def send(self, notice, binding, private):
        if self.closed:
            raise RuntimeError("QQ reminder worker stopped")
        payload = json.dumps(notice_payload(notice, binding, private), ensure_ascii=False).encode("utf-8")
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name="calendar-qq-reminders", daemon=True)
            self.thread.start()
        self.jobs.put((notice, binding, private, payload))

    def set_privacy(self, private):
        with self.lock:
            self.private = private

    def cancel(self, identifier, attempt):
        with self.lock:
            self.cancelled.add((identifier, attempt))

    def test(self):
        binding = self.binding()
        if binding is None:
            raise ValueError("请先启用远程服务并核验已绑定账号。")
        now = datetime.now()
        notice = Notice(uuid.uuid4().hex, "test", now, now + timedelta(minutes=2), ())
        self.send(notice, binding, True)
        return notice.key

    def run(self):
        while (job := self.jobs.get()) is not None:
            notice, binding, private, payload = job
            with self.lock:
                key = notice.key, notice.attempt
                cancelled = self.closed or key in self.cancelled
                redact = self.private and not private
                self.cancelled.discard(key)
            if cancelled or redact:
                result = "cancelled", "隐私策略已更新" if redact else "发送前已取消"
            else:
                try:
                    result = self.submit(notice, binding, payload)
                except Exception:
                    result = "unknown", "远程发送结果未知"
            with self.lock:
                self.cancelled.discard(key)
            event_id = "brief-test:" + notice.key if notice.manual_test else notice.key
            self.events.put((event_id, notice.attempt, *result))

    def submit(self, notice, binding, payload):
        if datetime.now() >= notice.expires:
            return "expired", "通知已过期"
        worker = binding.worker
        with worker.condition:
            if (not worker._current(binding.generation) or worker.config[:2] != (binding.url, binding.token)
                    or worker.config[3] != binding.account):
                return "cancelled", "发送前远程绑定已变更"
        try:
            response = self.client_factory(binding.url).request(PATH, binding.token, payload)
        except StatusError as error:
            if error.status in (400, 401, 403, 404, 413, 422, 429):
                return "failed", f"远程服务拒绝发送（HTTP {error.status}）"
            return "unknown", f"远程响应不明确（HTTP {error.status}）"
        except Unavailable:
            return "unknown", "网络中断，无法确认服务端是否发送"
        if (response.get("account_id") != binding.account or response.get("notification_id") != notice.key
                or response.get("channel") != "qq"):
            return "unknown", "远程回执与通知身份不匹配"
        status = response.get("status")
        if status == "sent":
            return "sent", ""
        if status == "failed":
            return "failed", "远程服务明确报告发送失败"
        return "unknown", "远程服务尚未确认发送结果"

    def close(self):
        with self.lock:
            self.closed = True
        if self.thread is not None:
            self.jobs.put(None)
