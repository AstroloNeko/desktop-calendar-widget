"""Read-only presentation of the existing scheduler, never a second scheduler."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from desktop_calendar.reminders.reminder_engine import CATCHUP, RETRY, local_wall, snoozed_plan
from desktop_calendar.reminders.notification_dispatch import STATUS_LABELS, MAX_ATTEMPTS, read_time


@dataclass(frozen=True)
class ReminderRow:
    event_id: str
    title: str
    when: datetime
    anchor: str
    ddl: bool
    group: str
    status: str
    detail: str
    snoozed: bool = False
    next_attempt: datetime | None = None
    batch: str = ""
    windows: str = "—"
    qq: str = "—"
    rule: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ReminderOverview:
    enabled: bool
    rows: tuple[ReminderRow, ...]
    submitted: int = 0
    qq_sent: int = 0


CHANNEL_LABELS = {**STATUS_LABELS, "submitted": "已提交", "sent": "发送成功",
                  "dispatching": "等待结果", "unknown": "结果不确定", "failed": "发送失败"}


def batch_channel_statuses(batch):
    channels = batch["channels"]
    return tuple(CHANNEL_LABELS[channels[key]["status"]] if key in channels else "未列入"
                 for key in ("windows", "qq"))


def rule_label(event, plan, config, snoozed):
    rule = config["events"].get(event.id, {})
    if rule.get("mode") == "custom":
        text = f"单事项规则 · 提前 {rule['days']} 天 · {rule['time']}"
    elif event.has_time:
        text = f"事项编辑器 · 提前 {event.reminder} 分钟"
    else:
        days = config["ddl_days" if plan.ddl else "general_days"]
        text = f"默认规则 · 提前 {days} 天 · {config['time']}"
    return f"稍后提醒 · {text}" if snoozed else text


def build_overview(store, plans, state, now, config):
    rows, old_submitted = [], 0
    events = {event.id: event for event in store.events}
    for base in plans:
        event = events.get(base.event_id)
        if event is None or event.done:
            continue
        snooze = state["snoozes"].get(base.event_id)
        is_snoozed = isinstance(snooze, dict) and snooze.get("base") == base.key
        try:
            plan = snoozed_plan(base, snooze)
        except (ValueError, TypeError, KeyError):
            rows.append(ReminderRow(base.event_id, str(event.title), base.when, base.anchor, base.ddl,
                                    "attention", "稍后提醒记录异常", "未派发，请取消稍后提醒后重新设置。", True,
                                    rule=rule_label(event, base, config, True)))
            continue
        entry = state["deliveries"].get(plan.key, {})
        entry = entry if isinstance(entry, dict) else {}
        status = entry.get("status")
        batch_id = entry.get("batch")
        batch = state["batches"].get(batch_id)
        if batch:
            channels = batch["channels"]
            states = [value["status"] for value in channels.values()]
            needs_attention = any(value in ("failed", "unknown", "expired", "cancelled") for value in states)
            sending = any(value in ("pending", "dispatching") for value in states)
            if needs_attention:
                group = "attention"
            elif sending:
                group = "snoozed" if is_snoozed else "attention"
            else:
                group = "processed"
            statuses = [f"{'Windows' if channel == 'windows' else 'QQ'}: {STATUS_LABELS[value['status']]}"
                        for channel, value in channels.items()]
            details = [label + (f" ({value['reason']})" if value.get("reason") else "")
                       for label, value in zip(statuses, channels.values())]
            expiry = read_time(batch["expires"], now)
            retries = [read_time(value.get("at"), now) + RETRY for value in channels.values()
                       if value["status"] == "failed" and value["attempts"] < MAX_ATTEMPTS]
            retry = min((at for at in retries if at < expiry), default=None)
            details.insert(0, f"同一通知共 {len(batch['members'])} 项")
            windows, qq = batch_channel_statuses(batch)
            reason = "；".join(value["reason"] for value in channels.values()
                              if value.get("reason") and value["status"] in ("failed", "unknown", "expired", "cancelled"))
            rows.append(ReminderRow(plan.event_id, str(event.title), plan.when, plan.anchor, plan.ddl,
                                    group, " / ".join(statuses), "\n".join(details), is_snoozed and (sending or needs_attention),
                                    retry, batch_id, windows, qq, rule_label(event, base, config, is_snoozed), reason))
            continue
        if status == "accepted":
            old_submitted += 1
            continue
        if status == "snoozed":
            continue  # Superseded/cancelled; never promise another dispatch.
        group = "snoozed" if is_snoozed else "upcoming"
        text = "稍后提醒" if is_snoozed else "等待提醒"
        detail = "预计派发时间；实际显示受 Windows 通知和专注设置影响。"
        next_attempt = plan.when
        if status in ("failed", "exhausted"):
            group, text = "attention", "派发失败" if status == "failed" else "已停止重试"
            detail = "请检查系统通知权限；调整事项提醒时间可生成新计划。"
        if status in ("failed", "dispatching"):
            try:
                next_attempt = max(plan.when, local_wall(datetime.fromisoformat(entry["at"])) + RETRY)
            except (ValueError, KeyError, TypeError):
                next_attempt = max(plan.when, now)
            attempts = entry.get("attempts", 0)
            if type(attempts) is not int or not 0 <= attempts < 3:
                next_attempt = None
                detail = "本轮尝试次数已用尽，不会继续自动重试。"
            if status == "dispatching":
                text = "正在提交 / 等待结果"
                detail = ("尚未收到 Windows 接受结果；失败或中断后按原有规则有限重试。"
                          if next_attempt is not None else "尚未收到 Windows 接受结果；已是最后一次尝试，不会继续自动重试。")
        if status == "unknown":
            group, text, next_attempt = "attention", "发送结果不确定", None
            detail = "旧版提交结果未确认。"
        if status in ("expired", "exhausted") or now - plan.when >= CATCHUP:
            group, next_attempt = "attention", None
            if status == "expired" and now - plan.when <= CATCHUP:
                text, detail = "已结束派发", "此计划已结束，不会自动补发。"
            elif status not in ("failed", "exhausted"):
                text, detail = "已错过提醒", "已超过现有 24 小时补发范围，不会自动补发。"
        if group == "upcoming" and plan.when < now:
            group = "attention"
            if text == "等待提醒":
                text = "已到提醒时间"
        elapsed = now - plan.when >= CATCHUP
        waiting = "已过期" if elapsed else "待处理" if plan.when < now else "等待触发"
        windows = text if status else (waiting if config["windows"] else "未启用")
        qq = "未列入" if status else (waiting if config["qq"] else "未启用")
        rows.append(ReminderRow(plan.event_id, str(event.title), plan.when, plan.anchor, plan.ddl,
                                group, text, detail, is_snoozed, next_attempt, windows=windows, qq=qq,
                                rule=rule_label(event, base, config, is_snoozed)))
    order = {"attention": 0, "snoozed": 1, "upcoming": 2, "processed": 3}
    rows.sort(key=lambda row: (order[row.group], row.next_attempt or row.when, row.when))
    submitted = sum(batch["channels"].get("windows", {}).get("status") == "submitted"
                    for batch in state["batches"].values())
    qq_sent = sum(batch["channels"].get("qq", {}).get("status") == "sent"
                  for batch in state["batches"].values())
    return ReminderOverview(config["enabled"], tuple(rows), old_submitted + submitted, qq_sent)
