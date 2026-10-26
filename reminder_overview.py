"""Read-only presentation of the existing scheduler, never a second scheduler."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from reminder_engine import CATCHUP, RETRY, local_wall, snoozed_plan


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


@dataclass(frozen=True)
class ReminderOverview:
    enabled: bool
    rows: tuple[ReminderRow, ...]
    submitted: int = 0


def build_overview(store, plans, state, now, enabled):
    rows, submitted = [], 0
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
                                    "attention", "稍后提醒记录异常", "未派发，请取消稍后提醒后重新设置。", True))
            continue
        entry = state["deliveries"].get(plan.key, {})
        entry = entry if isinstance(entry, dict) else {}
        status = entry.get("status")
        if status == "accepted":
            submitted += 1
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
        if status in ("expired", "exhausted") or now - plan.when > CATCHUP:
            group, next_attempt = "attention", None
            if status == "expired" and now - plan.when <= CATCHUP:
                text, detail = "已结束派发", "此计划已结束，不会自动补发。"
            elif status not in ("failed", "exhausted"):
                text, detail = "已错过提醒", "已超过现有 24 小时补发范围，不会自动补发。"
        # Reflect the existing burst gate without changing its state or heap.
        if next_attempt is not None and next_attempt <= now:
            burst = state["burst"]
            try:
                start = local_wall(datetime.fromisoformat(burst["at"]))
                if 0 <= (now - start).total_seconds() < 300 and int(burst["count"]) >= 3:
                    next_attempt = max(next_attempt, start + timedelta(minutes=5))
                    detail += " 当前处于批量提醒限流，需等待下一窗口。"
            except (ValueError, KeyError, TypeError, OverflowError):
                pass
        rows.append(ReminderRow(plan.event_id, str(event.title), plan.when, plan.anchor, plan.ddl,
                                group, text, detail, is_snoozed, next_attempt))
    order = {"attention": 0, "snoozed": 1, "upcoming": 2}
    rows.sort(key=lambda row: (order[row.group], row.next_attempt or row.when, row.when))
    return ReminderOverview(enabled, tuple(rows), submitted)
