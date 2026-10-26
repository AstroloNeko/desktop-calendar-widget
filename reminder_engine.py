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
    """All methods are owned by the Tk/main thread; backend is injected.

    backend.send(plan, title, text, actions) is asynchronous; results must call
    acknowledge. Reservation is persisted *before* send, limiting crash retries.
    At most three accepted dispatches per poll, three attempts per plan. Stable
    OS tags replace the same history item across the unavoidable send/ack gap.
    """
    def __init__(self, store, backend):
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
        return {"version": 1,
                "deliveries": deepcopy(raw.get("deliveries")) if isinstance(raw.get("deliveries"), dict) else {},
                "snoozes": deepcopy(raw.get("snoozes")) if isinstance(raw.get("snoozes"), dict) else {},
                "burst": deepcopy(raw.get("burst")) if isinstance(raw.get("burst"), dict) else {}}

    def _save(self, key, value):
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
        self.queue = []
        for plan in self.plans.values():
            try:
                plan = snoozed_plan(plan, state["snoozes"].get(plan.event_id))
            except (ValueError, TypeError, KeyError):
                continue  # Corrupt snooze is not silently sent as a base reminder.
            heapq.heappush(self.queue, (plan.when, plan.key, plan))
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
        from reminder_overview import build_overview
        config, state = self.config(), self._state()
        plans = plans_for_store(self.store, config)
        self._import_legacy({plan.event_id: plan for plan in plans}, state)
        return build_overview(self.store, plans, state, local_wall(now), config["enabled"])

    def tick(self, now):
        now = local_wall(now)
        if self.dirty:
            self._rebuild()
        if not self.config()["enabled"]:
            return 15000
        if not self.queue or self.queue[0][0] > now:
            return 15000
        state = self._state()
        deliveries = state["deliveries"]
        sent = 0
        # The heap already excludes completed/deleted events after invalidation.
        while self.queue and self.queue[0][0] <= now and sent < 3:
            _, _, plan = heapq.heappop(self.queue)
            entry = deliveries.get(plan.key, {})
            if not isinstance(entry, dict):
                entry = {}
            if entry.get("status") in ("accepted", "expired", "exhausted", "snoozed"):
                continue
            attempts = entry.get("attempts", 0)
            attempts = attempts if type(attempts) is int and 0 <= attempts <= 3 else 3
            if now - plan.when > CATCHUP:
                continue
            if entry.get("status") in ("dispatching", "failed"):
                try:
                    retry = local_wall(datetime.fromisoformat(entry["at"])) + RETRY
                except (KeyError, ValueError, TypeError):
                    retry = now
                if retry > now:
                    heapq.heappush(self.queue, (retry, plan.key, plan))
                    continue
            if attempts >= 3:
                deliveries[plan.key] = {"status": "exhausted", "at": now.isoformat(), "attempts": attempts}
                self._save(STATE_KEY, state)
                continue
            event = self.store.event_by_id(plan.event_id)
            if event is None or event.done:
                continue
            burst = state["burst"]
            try:
                burst_start = local_wall(datetime.fromisoformat(burst["at"]))
                count = int(burst["count"])
            except (ValueError, TypeError, KeyError):
                burst_start, count = now, 0
            if not timedelta() <= now - burst_start < timedelta(minutes=5):
                burst_start, count = now, 0
            if count >= 3:
                retry = burst_start + timedelta(minutes=5)
                heapq.heappush(self.queue, (retry, plan.key, plan))
                break
            state["burst"] = {"at": burst_start.isoformat(), "count": count + 1}
            deliveries[plan.key] = {"status": "dispatching", "at": now.isoformat(), "attempts": attempts + 1}
            try:
                self._save(STATE_KEY, state)
            except Exception:
                self.invalidate()
                raise  # No notification is sent if the durable reservation fails.
            private = self.config()["private"]
            text = "你有一项日程需要处理" if private else ("截止提醒：" if plan.ddl else "日程提醒：") + str(event.title)[:200]
            try:
                self.backend.send(plan, "桌面月历 · 日程提醒", text,
                                  [(f"{minutes} 分钟后", notification_arguments(plan, str(minutes))) for minutes in SNOOZES])
            except Exception:
                self.acknowledge(plan.key, False, now)
            heapq.heappush(self.queue, (now + RETRY, plan.key, plan))
            sent += 1
        delay = 15000
        if self.queue:
            delay = max(1000, min(delay, int((self.queue[0][0] - now).total_seconds() * 1000)))
        return delay

    def acknowledge(self, key, success, now):
        state = self._state()
        entry = state["deliveries"].get(key)
        if not isinstance(entry, dict):
            return
        # An async Failed event may arrive before the Show return result.
        if success and entry.get("status") != "dispatching":
            return
        entry.update(status="accepted" if success else "failed", at=local_wall(now).isoformat())
        self._save(STATE_KEY, state)
        if not success:
            self.invalidate()

    def snooze(self, event_id, key, minutes, now):
        if minutes not in SNOOZES:
            raise ReminderError("不支持的延后时间")
        if self.dirty:
            self._rebuild()
        plan = self.plans.get(event_id)
        event = self.store.event_by_id(event_id)
        if not plan or not event or event.done:
            return False
        state = self._state()
        previous = state["snoozes"].get(event_id, {})
        previous = previous if isinstance(previous, dict) else {}
        if key not in (plan.key, previous.get("key")):
            return False
        when = local_wall(now) + timedelta(minutes=minutes)
        snooze_key = hashlib.sha256(f"{plan.key}:snooze:{when.isoformat()}".encode()).hexdigest()
        state["deliveries"][plan.key] = {"status": "snoozed", "at": local_wall(now).isoformat(), "attempts": 0}
        if previous.get("key"):
            state["deliveries"][previous["key"]] = {"status": "snoozed", "at": local_wall(now).isoformat(), "attempts": 0}
        state["snoozes"][event_id] = {"base": plan.key, "key": snooze_key, "at": when.isoformat()}
        self._save(STATE_KEY, state)
        self.backend.remove(key[:16])
        self.invalidate()
        return True

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
        return bool(plan and (key == plan.key or (snooze.get("base") == plan.key and key == snooze.get("key"))))
