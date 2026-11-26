"""Worker-Token transport for the proposed Antigravity proactive notice endpoint."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import queue
import threading
import uuid

from desktop_calendar.remote_transport import Client, StatusError, Unavailable
from desktop_calendar.reminders.notification_model import Notice, identity, select_brief_items

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
    private = private or notice.kind == "test"
    if len(notice.items) > 500:
        raise ValueError("提醒批次超过主动通知协议上限")
    brief_counts = None
    selected = ()
    if private and notice.kind != "test":
        fallback = (f"共 {len(notice.items)} 项，打开桌面月历查看。"
                    if notice.items else "本次简报没有事项。")
    elif notice.kind == "brief":
        selected, brief_counts = select_brief_items(notice.items)
        fallback = notice.summary(20, selected=selected)
    else:
        selected = () if private else notice.items[:50]
        fallback = notice.summary(20)[:3800]
        if notice.kind in ("event","ddl") and len(notice.items) > 20:
            fallback += f"\n共 {len(notice.items)} 项。知道了作用于本批仍有效的全部提醒。超过 20 项时请按编号引用或到桌面端稍后处理。"
    wire_items = [
        {**{key: item[key] for key in ("title", "group", "date", "time") if key in item}, "index": index}
        for index, item in enumerate(selected, 1)
    ]
    return {
        "protocol_version": 1, "notification_id": notice.key, "type": notice.kind, "attempt":notice.attempt,
        "account_id": binding.account, "channel": "qq",
        "triggered_at": notice.when.astimezone().isoformat(),
        "expires_at": notice.expires.astimezone().isoformat(),
        "private": private, "count": len(notice.items),
        "items": wire_items,
        **({"brief_counts": brief_counts} if brief_counts is not None else {}),
        "fallback_text": ("【测试简报】\n" + fallback) if notice.manual_test else fallback,
        **({"is_test": True} if notice.manual_test else {}),
        "actions": [{"action": action, "target": notice.key} for action in ACTIONS[notice.kind]
                    if not notice.manual_test and action in binding.supported_actions and (not private or action == "acknowledge")],
    }


class DeliveryDetail(str):
    """Safe display text plus durable retry and attempt evidence."""
    def __new__(cls, text, *, retryable=False, delay=None, submission=None, attempt=None):
        detail = super().__new__(cls, text)
        detail.retryable, detail.delay = retryable, delay
        detail.submission, detail.attempt = submission, attempt
        return detail


class QQReminderBackend:
    def __init__(self, get_worker, client_factory=Client):
        self.get_worker, self.client_factory = get_worker, client_factory
        self.jobs, self.events = queue.Queue(), queue.Queue()
        self.thread = None
        self.lock = threading.RLock()
        self.cancelled = set()
        self._capability = None
        self.closed = False
        self.private = False

    def binding(self):
        worker = self.get_worker()
        if worker is not None:
            with worker.condition:
                if worker._current(worker.generation) and getattr(worker,"proactive_enabled",False) is True:
                    url, token, _scope, account = worker.config
                    if account:
                        return VerifiedBinding(worker, worker.generation, url, token, account, worker.proactive_actions)
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

    def configuration_guard(self):
        return self.lock

    def proactive_capability(self, binding):
        with self.lock:
            if self._capability and self._capability[0] == binding.scope:
                return self._capability[1]
        return True

    def _observe_capability(self, binding, available):
        with self.lock:
            self._capability = (binding.scope, available)

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
                    result = self.submit(notice, binding, payload, private=private)
                except Exception:
                    result = "unknown", "远程发送结果未知"
            with self.lock:
                self.cancelled.discard(key)
            event_id = "brief-test:" + notice.key if notice.manual_test else notice.key
            self.events.put((event_id, notice.attempt, *result))

    def submit(self, notice, binding, payload, *, private=None):
        if datetime.now() >= notice.expires:
            return "expired", "通知已过期"
        worker = binding.worker
        with worker.condition:
            if (not worker._current(binding.generation) or worker.config[:2] != (binding.url, binding.token)
                    or worker.config[3] != binding.account or getattr(worker, "proactive_enabled", None) is not True):
                return "cancelled", "发送前远程绑定已变更"
        with self.lock:
            if self.closed or (notice.key,notice.attempt) in self.cancelled:
                return "cancelled", "发送前已取消"
            if private is None and self.private:
                try:
                    private = json.loads(payload).get("private") is True
                except (ValueError, AttributeError, RecursionError):
                    private = False
            if self.private and not private:
                return "cancelled", "隐私策略已更新"
        try:
            response = self.client_factory(binding.url).request(PATH, binding.token, payload)
        except StatusError as error:
            if error.status == 404:
                self._observe_capability(binding, False)
            if error.status in (400,401,403,404,413,422,429) or (
                    error.status == 503 and (error.body or {}).get("error") == "qq_not_configured"):
                return self.delivery_outcome(notice, binding, error.body, error.status)
            return "unknown", f"远程响应不明确（HTTP {error.status}）"
        except Unavailable:
            return "unknown", "网络中断，无法确认服务端是否发送"
        outcome = self.delivery_outcome(notice, binding, response, 200)
        if outcome[0] == "sent":
            self._observe_capability(binding, True)
        return outcome

    @staticmethod
    def delivery_outcome(notice, binding, response, http):
        rejected = http != 200
        if not isinstance(response, dict) or (response.get("account_id"),response.get("notification_id"),response.get("channel")) != (
                binding.account,notice.key,"qq"):
            if rejected:
                return "failed", DeliveryDetail(f"云端发前拒绝（HTTP {http}）",submission="preflight")
            return "unknown", "远程回执与通知身份不匹配"
        status, outcome, attempt = (response.get(key) for key in ("status","outcome","attempt"))
        allowed = {"sent":("sent",), "unknown":("unknown",),
                   "failed":("failed","expired","user_opt_out","rate_limited")}
        prior = (type(attempt) is int and 1 <= attempt < notice.attempt and response.get("duplicate") is True
                 and outcome in ("sent","unknown"))
        if (type(attempt) is not int or attempt != notice.attempt and not prior
                or not isinstance(status,str) or outcome not in allowed.get(status,())
                or rejected and status != "failed"
                or (http == 422 and outcome not in ("expired","failed"))
                or (http == 429 and outcome not in ("rate_limited","failed"))):
            return "unknown", "远程回执的发送尝试或结果无效"
        labels = {"sent":"", "unknown":"QQ 发送结果尚未确认", "expired":"通知已过期",
                  "user_opt_out":"QQ 用户已关闭主动通知", "rate_limited":"主动通知触发限频",
                  "failed":"云端发前拒绝" if rejected else "QQ 明确报告发送失败"}
        detail = labels[outcome]
        if rejected:
            detail += f"（HTTP {http}）"
        delay = response.get("retry_after_seconds")
        retryable = outcome in ("failed","rate_limited") and response.get("retryable") is True and (
            not rejected or http == 429)
        return outcome, DeliveryDetail(detail,retryable=retryable,delay=delay if type(delay) is int and delay > 0 else None,
                                       submission="preflight" if rejected else "qq",attempt=attempt)

    def close(self):
        with self.lock:
            self.closed = True
        if self.thread is not None:
            self.jobs.put(None)
