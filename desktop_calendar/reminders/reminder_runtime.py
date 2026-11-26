"""Owner-thread projections and plans for reminder-instance operations."""
from dataclasses import asdict
from datetime import datetime, timedelta

from desktop_calendar.reminders.notification_dispatch import read_time
from desktop_calendar.reminders.notification_model import identity
from desktop_calendar.reminders.reminder_engine import CATCHUP, ReminderError, local_wall, snoozed_plan
from desktop_calendar.reminders.reminder_overview import CHANNEL_LABELS, build_overview
from desktop_calendar.reminders.reminder_rules import stored_event_rule


def instance_revision(engine, event, plan, state):
    entry = state["deliveries"].get(plan.key, {})
    batch = state["batches"].get(entry.get("batch"), {}) if isinstance(entry, dict) else {}
    channels = {name: (row.get("status"), row.get("attempts"))
                for name, row in batch.get("channels", {}).items()}
    return identity(asdict(event), stored_event_rule(engine.store.settings, event.id),
                    plan.key, plan.when.isoformat(), entry, channels, state["handled"].get(plan.key))


def delivery_member_revision(store, event_id, plan_key):
    event = store.event_by_id(event_id)
    return identity(asdict(event), stored_event_rule(store.settings,event_id), plan_key) if event else None


def channel_members(batch, channel):
    entry = batch.get("channels", {}).get(channel, {})
    return entry.get("members", ())


def channel_states(batch, key):
    """New receipts identify attempted members; legacy receipts are batch-only."""
    codes, labels = {}, {}
    for channel, entry in batch.get("channels", {}).items():
        label = CHANNEL_LABELS.get(entry["status"], "未知")
        if "members" not in entry and entry.get("attempts", 0):
            labels[channel] = "批次记录 · " + label
            codes[channel] = entry["status"]
        elif "members" not in entry or any(member[1] == key for member in entry["members"]):
            labels[channel] = label
            codes[channel] = entry["status"]
        else:
            labels[channel] = "本次未列入"
    return codes, labels


def may_have_sent(state, key):
    entry = state["deliveries"].get(key, {})
    batch = state["batches"].get(entry.get("batch"), {})
    channels, _ = channel_states(batch, key)
    return any(status in ("accepted", "submitted", "sent", "dispatching", "unknown")
               for status in (entry.get("status"), *channels.values()))


def processed_at(state, batch, key):
    qq = batch.get("channels", {}).get("qq", {})
    return state["handled"].get(key) or (qq.get("acknowledged_at") if "handled_keys" not in qq else None)


def live_instances(engine, plans, state, events=None):
    """Only current base/snooze instances are actionable."""
    current = {}
    if events is None:
        events = {event.id:event for event in engine.store.events}
    for base in plans.values():
        try:
            plan = snoozed_plan(base, state["snoozes"].get(base.event_id))
        except (ValueError, TypeError, KeyError):
            continue
        entry = state["deliveries"].get(plan.key, {})
        event = events.get(plan.event_id)
        if event is not None and not event.done and isinstance(entry, dict) and entry.get("status") != "snoozed":
            current[plan.key] = (plan, instance_revision(engine, event, plan, state))
    return current


def runtime_snapshot(engine, now):
    """Reuse the overview for live rows and the actual batch ledger for history."""
    now = local_wall(now)
    config, plans, state = engine.read_snapshot()
    events = {event.id: event for event in engine.store.events}
    current = live_instances(engine, plans, state, events)
    overview = build_overview(engine.store, tuple(plans.values()), state, now, config, include_legacy=True)
    rows = {}
    for row in overview.rows:
        batch = state["batches"].get(row.batch, {})
        entry = state["deliveries"].get(row.plan_key, {})
        codes, labels = channel_states(batch, row.plan_key)
        if not batch and entry.get("status"):
            codes["windows"] = {"accepted":"submitted"}.get(entry["status"], entry["status"])
        rows[row.plan_key] = dict(
            key=row.plan_key, event_id=row.event_id, title=row.title, when=row.when,
            next_attempt=row.next_attempt, group=row.group, status="派发记录" if batch else row.status,
            windows=labels.get("windows", row.windows), qq=labels.get("qq", row.qq), send_states=codes, batch=row.batch,
            channel_reasons={name:entry.get("reason") for name,entry in batch.get("channels",{}).items()
                             if name in codes and entry.get("reason")},
            snoozed=bool(row.plan_key in current and row.plan_key != plans[row.event_id].key),
            done=events[row.event_id].done, current=row.plan_key in current,
            revision=current[row.plan_key][1] if row.plan_key in current else None,
            processed_at=processed_at(state,batch,row.plan_key),
            missed=(row.status == "已错过提醒" or missed(codes, row.when, now)),
        )
    for identifier, batch in state["batches"].items():
        if not isinstance(batch, dict) or batch.get("kind") not in ("event", "ddl"):
            continue
        when = read_time(batch.get("when"), None)
        if when is None:
            continue
        for event_id, key in batch.get("members", ()):
            if event_id not in events or key in rows:
                continue
            event = events[event_id]
            codes, labels = channel_states(batch, key)
            planned_at = read_time(batch.get("planned_at", {}).get(key), None)
            rows[key] = dict(key=key, event_id=event_id, title=event.title, when=planned_at or when, next_attempt=None,
                group="history", status="历史实例" if planned_at else "历史批次时间", windows=labels.get("windows","未列入"),
                qq=labels.get("qq","未列入"), send_states=codes, batch=identifier,
                channel_reasons={name:entry.get("reason") for name,entry in batch["channels"].items()
                                 if name in codes and entry.get("reason")},
                snoozed=False, done=event.done, current=False, revision=None,
                processed_at=processed_at(state,batch,key),
                missed=missed(codes, planned_at or when, now))
    return config, rows, state


def missed(codes, when, now):
    return ("expired" in codes.values() or now - when >= CATCHUP and
            any(value in ("pending", "failed", "exhausted") for value in codes.values())) and not any(
            value in ("sent", "submitted", "unknown", "dispatching") for value in codes.values())


def snooze_time(action, now, scheduled_at=None):
    if "until" in action:
        try:
            when = datetime.fromisoformat(action["until"])
            if when.strftime("%Y-%m-%dT%H:%M") != action["until"]:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ReminderError("稍后时间须为明确的本地日期及钟点。") from error
    else:
        when = max(now, scheduled_at or now) + timedelta(minutes=action["minutes"])
    if not now < when <= now + CATCHUP:
        raise ReminderError("稍后提醒须设在未来 24 小时内。")
    if scheduled_at is not None and when < scheduled_at:
        raise ReminderError("新时刻早于当前提醒，无法作为稍后提醒。")
    return when
