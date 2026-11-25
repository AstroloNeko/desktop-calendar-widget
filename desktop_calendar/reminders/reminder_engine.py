"""Local-wall-time event reminders. No Tk, Win32, UI redraws or background loop.

Preferences/dispatch ledger live in extensible Store.settings, hence the existing
atomic storage, schema gate and backups apply without changing Event or schema.
History is separate from Store.notified (the legacy habit ledger has a 600 cap).
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import heapq
import json
from urllib.parse import parse_qs, urlencode

from calendar_core import normalize_reminder_time
from desktop_calendar.reminders.notification_model import brief_settings

CONFIG_KEY = "native_reminders"
STATE_KEY = "native_reminder_state"
LEADS = (0, 1, 3)
SNOOZES = (10, 30, 60)
CATCHUP = timedelta(hours=24)
RETRY = timedelta(minutes=2)
DEFAULT_CONFIG = {"version": 1, "enabled": False, "time": "09:00",
                  "general_days": 0, "ddl_days": 1, "private": False, "events": {}}


class ReminderError(ValueError):
    pass


def normalize_config(raw):
    if not isinstance(raw, dict):
        raw = {}
    if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
        raise ReminderError("提醒设置版本不兼容，已停止提醒；日历数据未改变。")
    result = deepcopy(DEFAULT_CONFIG)
    result.update(enabled=raw.get("enabled") is True, private=raw.get("private") is True,
                  time=normalize_reminder_time(raw.get("time")) or "09:00")
    for key in ("windows", "aggregate", "merge_backlog", "qq"):
        result[key] = raw.get(key, key != "qq") is True
    result["brief"] = brief_settings(raw.get("brief"))
    for key in ("general_days", "ddl_days"):
        value = raw.get(key, result[key])
        if type(value) is int and value in LEADS:
            result[key] = value
    rules = raw.get("events", {})
    if isinstance(rules, dict):
        for identifier, rule in rules.items():
            if not isinstance(identifier, str) or not isinstance(rule, dict):
                continue
            mode = rule.get("mode")
            if mode == "off":
                result["events"][identifier] = {"mode": "off"}
            elif mode == "custom":
                days, clock = rule.get("days"), normalize_reminder_time(rule.get("time"))
                if type(days) is int and days in LEADS and clock:
                    result["events"][identifier] = {"mode": "custom", "days": days, "time": clock}
    return result


def local_wall(value):
    return value.astimezone().replace(tzinfo=None) if value.tzinfo else value


@dataclass(frozen=True)
class ReminderPlan:
    event_id: str
    key: str
    when: datetime
    anchor: str
    ddl: bool

    @property
    def tag(self):
        return self.key[:16]


def snoozed_plan(plan, entry):
    """Resolve persisted Snooze identically for dispatch and read-only views."""
    if not isinstance(entry, dict) or entry.get("base") != plan.key:
        return plan
    key = entry.get("key")
    if not isinstance(key, str) or len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
        raise ValueError("Invalid snooze identity")
    when = local_wall(datetime.fromisoformat(entry["at"]))
    return ReminderPlan(plan.event_id, key, when, plan.anchor, plan.ddl)


def _plan_for_event(store, event, config):
    if event.done:
        return None
    rule = config["events"].get(event.id, {})
    if rule.get("mode") == "off":
        return None
    ddl = store.event_has_deadline(event)
    anchor = store.event_end_date(event) if ddl else store.event_start_date(event)
    if rule.get("mode") == "custom":
        clock, days = rule["time"], rule["days"]
        when = datetime.combine(anchor, time.fromisoformat(clock)) - timedelta(days=days)
    elif event.has_time:
        # Preserve existing explicit minute-based editor settings. Invalid old
        # reminder metadata is skipped, not rewritten or passed to timedelta.
        if type(event.reminder) is not int or not 0 <= event.reminder <= 10080:
            return None
        when = datetime.combine(anchor, event.due_at.time()) - timedelta(minutes=event.reminder)
    else:
        days = config["ddl_days" if ddl else "general_days"]
        when = datetime.combine(anchor, time.fromisoformat(config["time"])) - timedelta(days=days)
    when = local_wall(when)
    fingerprint = json.dumps([1, event.id, event.event_type, event.due, event.has_time,
                              event.reminder, event.duration_days, event.skip_non_working_days,
                              event.end_as_ddl, anchor.isoformat(), when.isoformat()], ensure_ascii=True)
    key = hashlib.sha256(fingerprint.encode()).hexdigest()
    return ReminderPlan(event.id, key, when, anchor.isoformat(), ddl)


def plans_for_store(store, config):
    """Called on invalidation, never on every timer tick. Reuse effective dates."""
    if not config["enabled"]:
        return ()
    plans = []
    for event in store.events:
        try:
            plan = _plan_for_event(store, event, config)
        except (ValueError, TypeError, OverflowError):
            continue  # One invalid old event cannot stop all scheduling/Habits.
        if plan:
            plans.append(plan)
    return tuple(plans)


def notification_arguments(plan, action="open"):
    return urlencode({"v": "1", "event": plan.event_id, "plan": plan.key, "action": action})


def parse_activation(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        fields = parse_qs(value, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None
    if set(fields) != {"v", "event", "plan", "action"} or any(len(v) != 1 for v in fields.values()):
        return None
    result = {key: values[0] for key, values in fields.items()}
    if result["v"] != "1" or result["action"] not in ("open", "10", "30", "60"):
        return None
    if len(result["plan"]) != 64 or any(ch not in "0123456789abcdef" for ch in result["plan"]):
        return None
    return result


class ReminderEngine:
    """Calendar plan and ledger owner. Channel workers only receive detached notices."""
    def __init__(self, store, backend, qq=None):
        from desktop_calendar.reminders.notification_dispatch import DeliveryQueue
        self.delivery = DeliveryQueue(self, qq)
        self.store, self.backend = store, backend
        self.dirty = True
        self.plans = {}
        self.queue = []
        self.error = ""
        self.rebuild_count = 0
        self.revision = 0

    def config(self):
        return normalize_config(self.store.settings.get(CONFIG_KEY))

    def _state(self):
        raw = self.store.settings.get(STATE_KEY, {})
        if not isinstance(raw, dict):
            raw = {}
        if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
            raise ReminderError("提醒记录版本不兼容，已停止提醒；日历数据未改变。")
        return {"version": 1, **{
            key: deepcopy(raw[key]) if isinstance(raw.get(key), dict) else {}
            for key in ("deliveries", "snoozes", "batches", "qq_cutover")
        }}

    def _save(self, key, value):
        write_preference = getattr(self.store.repository, "write_reminder_preference", None)
        if write_preference is not None:
            write_preference(key, value)
            self.revision += 1
            return
        missing = key not in self.store.settings
        previous = self.store.settings.get(key)
        self.store.settings[key] = deepcopy(value)
        try:
            self.store.save()
        except Exception:
            if missing:
                self.store.settings.pop(key, None)
            else:
                self.store.settings[key] = previous
            raise
        self.revision += 1

    def configure(self, config):
        normalized = normalize_config(config)
        self._save(CONFIG_KEY, normalized)
        if self.delivery.qq is not None:
            self.delivery.qq.set_privacy(normalized["private"])
        self.invalidate()

    def invalidate(self):
        self.dirty = True

    def _rebuild(self):
        config = self.config()
        previous_plans = self.plans
        self.plans = {plan.event_id: plan for plan in plans_for_store(self.store, config)}
        for identifier, old in previous_plans.items():
            current = self.plans.get(identifier)
            if current is None or current.key != old.key:
                self.backend.remove(old.tag)
        state = self._state()
        changed = self._import_legacy(self.plans, state)
        for identifier, entry in list(state["snoozes"].items()):
            current = self.plans.get(identifier)
            if not isinstance(entry, dict) or current is None or entry.get("base") != current.key:
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    self.backend.remove(entry["key"][:16])
                del state["snoozes"][identifier]
                changed = True
        if changed:
            self._save(STATE_KEY, state)
        self.delivery.remove_stale(state)
        ready = []
        last_in_minute = {}
        for plan in self.plans.values():
            try:
                plan = snoozed_plan(plan, state["snoozes"].get(plan.event_id))
            except (ValueError, TypeError, KeyError):
                continue
            entry = state["deliveries"].get(plan.key, {})
            if isinstance(entry, dict) and (entry.get("batch") or entry.get("status") in
                                            ("accepted", "expired", "exhausted", "snoozed", "unknown")):
                continue
            minute = plan.ddl, plan.when.replace(second=0, microsecond=0)
            last_in_minute[minute] = max(last_in_minute.get(minute, plan.when), plan.when)
            ready.append((minute, plan))
        self.queue = [(last_in_minute[minute] if config["aggregate"] else plan.when, plan.key, plan)
                      for minute, plan in ready]
        heapq.heapify(self.queue)
        self.rebuild_count += 1
        self.dirty = False

    def _import_legacy(self, plans, state):
        """Modify only the caller's detached state; reads never save/import it."""
        changed = False
        # Import only exactly matching legacy reminders/Snooze after explicit
        # opt-in. Never rewrite Event or its original legacy fields.
        for identifier, plan in plans.items():
            if plan.key in state["deliveries"] or identifier in state["snoozes"]:
                continue
            event = self.store.event_by_id(identifier)
            if event and event.snooze_until:
                try:
                    when = local_wall(datetime.fromisoformat(event.snooze_until))
                    key = hashlib.sha256(f"{plan.key}:legacy-snooze:{when.isoformat()}".encode()).hexdigest()
                    state["snoozes"][identifier] = {"base": plan.key, "key": key, "at": when.isoformat()}
                    state["deliveries"][plan.key] = {"status": "snoozed", "at": when.isoformat(), "attempts": 0}
                    changed = True
                except (ValueError, TypeError):
                    pass  # Invalid legacy metadata does not alter the Event.
            elif f"{identifier}:reminder:{plan.when.isoformat(timespec='minutes')}" in self.store.notified:
                state["deliveries"][plan.key] = {"status": "accepted", "at": plan.when.isoformat(), "attempts": 0}
                changed = True
        return changed

    def overview(self, now):
        """Detached read-only projection; no rebuild, writes, send or removal."""
        from desktop_calendar.reminders.reminder_overview import build_overview
        config, state = self.config(), self._state()
        plans = plans_for_store(self.store, config)
        self._import_legacy({plan.event_id: plan for plan in plans}, state)
        return build_overview(self.store, plans, state, local_wall(now), config)

    def tick(self, now):
        self.delivery.tick(local_wall(now))
        return 2000

    def acknowledge(self, key, success, now, attempt=None, reason=""):
        if attempt is None:
            attempt = self._state()["batches"].get(key, {}).get("channels", {}).get("windows", {}).get("attempts")
        if attempt is not None:
            self.delivery.acknowledge(key, "windows", attempt, "submitted" if success else "failed",
                                      local_wall(now), reason)

    def batch_members(self, key):
        if self.dirty:
            self._rebuild()
        state = self._state()
        record = state["batches"].get(key)
        return self.delivery.current_members(record, state) if record else ()

    def batch_record(self, key):
        return self._state()["batches"].get(key)

    def snooze(self, event_id, key, minutes, now):
        return bool(self._snooze_many(((event_id, key),), minutes, local_wall(now)))

    def _snooze_many(self, members, minutes, now):
        if minutes not in SNOOZES:
            raise ReminderError("不支持的延后时间")
        if self.dirty:
            self._rebuild()
        state = self._state()
        when = now + timedelta(minutes=minutes)
        current = self.delivery.current_members({"members": members}, state)
        for event_id, key in current:
            plan = self.plans[event_id]
            snooze_key = hashlib.sha256(f"{plan.key}:snooze:{when.isoformat()}".encode()).hexdigest()
            marker = {"status": "snoozed", "at": now.isoformat(), "attempts": 0}
            state["deliveries"][plan.key] = marker
            state["deliveries"][key] = marker
            state["snoozes"][event_id] = {"base": plan.key, "key": snooze_key, "at": when.isoformat()}
        if current:
            self._save(STATE_KEY, state)
            for _event_id, key in current:
                self.backend.remove(key[:16])
            self.invalidate()
        return len(current)

    def apply_qq_action(self, identifier, action, binding, now):
        """Called on the calendar owner thread by the verified remote-action adapter."""
        now = local_wall(now)
        from desktop_calendar.reminders.notification_dispatch import read_time
        if binding is None or binding != self.delivery.binding():
            raise ReminderError("远程绑定已变更")
        state = self._state()
        batch = state["batches"].get(identifier, {})
        qq = batch.get("channels", {}).get("qq", {})
        config = self.config()
        if (not config["enabled"] or not config["qq"] or batch.get("scope") != binding.scope
                or batch.get("kind") not in ("event", "ddl") or qq.get("status") != "sent"
                or read_time(batch.get("expires"), now) <= now):
            raise ReminderError("提醒交互已失效")
        if action == "acknowledge":
            if not qq.get("acknowledged_at"):
                qq["acknowledged_at"] = now.isoformat()
                self._save(STATE_KEY, state)
            return {"acknowledged": True}
        if config["private"]:
            raise ReminderError("当前通知已启用隐私保护")
        if action == "snooze_10m":
            return {"snoozed": self._snooze_many(batch["members"], 10, now)}
        if action == "view_items":
            from desktop_calendar.reminders.notification_model import event_items
            return {"items": event_items(self.store, self.batch_members(identifier), False)}
        raise ReminderError("不支持的提醒交互")

    def cancel_snooze(self, event_id):
        state = self._state()
        if event_id not in state["snoozes"] and self.config()["enabled"]:
            # A read-only plan view can expose a legacy Snooze before the first
            # scheduling tick. Cancel it atomically without ticking/sending.
            plans = {plan.event_id: plan for plan in plans_for_store(self.store, self.config())
                     if plan.event_id == event_id}
            self._import_legacy(plans, state)
        previous = state["snoozes"].pop(event_id, None)
        if previous:
            self._save(STATE_KEY, state)
            if isinstance(previous, dict) and isinstance(previous.get("key"), str):
                self.backend.remove(previous["key"][:16])
            self.invalidate()

    def activation_is_current(self, event_id, key):
        if self.dirty:
            self._rebuild()
        plan = self.plans.get(event_id)
        state = self._state()
        snooze = state["snoozes"].get(event_id, {})
        snooze = snooze if isinstance(snooze, dict) else {}
        return bool(plan and key == (snooze.get("key") if snooze.get("base") == plan.key else plan.key)
                    and state["deliveries"].get(key, {}).get("status") != "snoozed")
