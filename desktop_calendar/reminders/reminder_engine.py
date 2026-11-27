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
from desktop_calendar.reminders.notification_model import brief_enabled, brief_settings
from desktop_calendar.reminders.reminder_config import LEADS as LEADS, ReminderError as ReminderError, active_qq, verify_plan
from desktop_calendar.reminders.reminder_rules import (
    CONFIG_KEY as CONFIG_KEY, MAX_MINUTES, effective_mode, normalize_event_rule, rule_enabled,
)

STATE_KEY = "native_reminder_state"
SNOOZES = (10, 30, 60)
CATCHUP = timedelta(hours=24)
RETRY = timedelta(minutes=2)
DEFAULT_CONFIG = {"version": 1, "enabled": False, "time": "09:00",
                  "general_days": 0, "ddl_days": 1, "private": False, "events": {}}


def normalize_config(raw):
    if not isinstance(raw, dict):
        raw = {}
    if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
        raise ReminderError("提醒设置版本不兼容，已停止提醒；日历数据未改变。")
    result = {**DEFAULT_CONFIG, "events": {}}
    result.update(enabled=raw.get("enabled") is True, private=raw.get("private") is True,
                  time=normalize_reminder_time(raw.get("time")) or "09:00")
    for key in ("windows", "aggregate", "merge_backlog", "qq"):
        result[key] = raw.get(key, key != "qq") is True
    result["legacy_explicit"] = raw.get("legacy_explicit", raw.get("enabled")) is True
    result["brief"] = brief_settings(raw.get("brief"))
    for key in ("general_days", "ddl_days"):
        value = raw.get(key, result[key])
        if type(value) is int and value in LEADS:
            result[key] = value
    rules = raw.get("events", {})
    if isinstance(rules, dict):
        for identifier, rule in rules.items():
            if isinstance(identifier, str) and (normalized := normalize_event_rule(rule)):
                result["events"][identifier] = normalized
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


def _plan_for_event(store, event, config, rule=None, *, preview=False):
    if event.done:
        return None
    rule = config["events"].get(event.id, {}) if rule is None else rule
    if rule.get("mode") == "off" or not preview and not rule_enabled(config, event, rule):
        return None
    ddl = store.event_has_deadline(event)
    anchor = store.event_end_date(event) if ddl else store.event_start_date(event)
    mode = rule.get("mode")
    if mode == "absolute":
        when = datetime.fromisoformat(rule["at"])
        key = hashlib.sha256(f"absolute:{event.id}:{rule['at']}".encode()).hexdigest()
        return ReminderPlan(event.id, key, when, anchor.isoformat(), ddl)
    if mode in ("relative", "custom") and "days" in rule:
        clock = rule.get("time")
        anchor_time = (time.fromisoformat(clock) if clock else
                       event.due_at.time() if mode == "relative" and event.has_time else
                       time.fromisoformat(config["time"]))
        when = datetime.combine(anchor, anchor_time) - timedelta(days=rule["days"])
    elif "minutes" in rule or event.has_time and not mode:
        # Preserve existing explicit minute-based editor settings. Invalid old
        # reminder metadata is skipped, not rewritten or passed to timedelta.
        minutes = rule.get("minutes", event.reminder)
        if not event.has_time or type(minutes) is not int or not 0 <= minutes <= MAX_MINUTES:
            return None
        when = datetime.combine(anchor, event.due_at.time()) - timedelta(minutes=minutes)
    else:
        days = config["ddl_days" if ddl else "general_days"]
        when = datetime.combine(anchor, time.fromisoformat(config["time"])) - timedelta(days=days)
    when = local_wall(when)
    fingerprint = json.dumps([1, event.id, event.event_type, event.due, event.has_time,
                              event.reminder, event.duration_days, event.skip_non_working_days,
                              event.end_as_ddl, anchor.isoformat(), when.isoformat()], ensure_ascii=True)
    key = hashlib.sha256(fingerprint.encode()).hexdigest()
    return ReminderPlan(event.id, key, when, anchor.isoformat(), ddl)


def describe_event_reminder(store, event, config, rule, now, *, engine=None, preview=False):
    """Project the rule without submitting notifications or touching the ledger."""
    rule = normalize_event_rule(rule) or {}
    plan = _plan_for_event(store, event, config, rule, preview=True)
    enabled = rule_enabled(config, event, rule)
    now = local_wall(now)
    past = bool(plan and plan.when <= now)
    channels = [key for key in ("windows", "qq") if config[key]]
    # A configured QQ channel alone does not prove this plan can use the verified route.
    catchup = bool(enabled and past and now - plan.when < CATCHUP and "windows" in channels)
    snoozed_at = None
    if engine and plan:
        from desktop_calendar.reminders.notification_dispatch import MAX_ATTEMPTS
        try:
            state = engine._state()
            actual = snoozed_plan(plan, state["snoozes"].get(event.id))
            if actual.key != plan.key:
                snoozed_at = actual.when.isoformat(timespec="minutes")
            delivery = state["deliveries"].get(plan.key, {})
            batch = state["batches"].get(delivery.get("batch"), {})
            eligible_channels = {"windows"} if config["windows"] else set()
            if config["qq"] and engine.delivery.qq_eligible(plan, state, now):
                eligible_channels.add("qq")
            catchup = bool(enabled and past and now - plan.when < CATCHUP and eligible_channels)
            if batch:
                catchup &= any(channel in eligible_channels and entry.get("status") in ("pending", "failed")
                               and entry.get("attempts", 0) < MAX_ATTEMPTS
                               for channel, entry in batch["channels"].items())
            elif (delivery.get("status") in ("accepted", "expired", "exhausted", "snoozed", "unknown", "dispatching")
                  or delivery.get("attempts", 0) >= MAX_ATTEMPTS):
                catchup = False
        except (ValueError, TypeError, OSError, KeyError, RuntimeError):
            catchup = False
    return {"mode": effective_mode(event, rule), "rule": rule,
            "planned_at": plan.when.isoformat(timespec="minutes") if plan else None,
            "global_enabled": config["enabled"], "rule_enabled": enabled, "past": past, "catchup_eligible": catchup, "snoozed_at": snoozed_at,
            "schedule_status": "preview" if preview else "pending" if engine is None or engine.dirty else "recalculated"}


def plans_for_store(store, config):
    """Called on invalidation, never on every timer tick. Reuse effective dates."""
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

    def _config_view(self):
        return normalize_config(self.store.settings.get(CONFIG_KEY))

    def config(self):
        return self._config_view()

    def _state_raw(self):
        raw = self.store.settings.get(STATE_KEY, {})
        if not isinstance(raw, dict):
            raw = {}
        if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
            raise ReminderError("提醒记录版本不兼容，已停止提醒；日历数据未改变。")
        return raw

    def _state_view(self):
        raw = self._state_raw()
        return {"version": 1, **{
            key: raw[key] if isinstance(raw.get(key), dict) else {}
            for key in ("deliveries", "snoozes", "batches", "qq_cutover", "handled", "runtime_actions",
                        "config_versions", "config_actions", "config_cutovers", "qq_capability_required", "qq_capabilities")
        }}

    def _state(self):
        raw = self._state_raw()
        return {"version": 1, **{
            key: deepcopy(raw[key]) if isinstance(raw.get(key), dict) else {}
            for key in ("deliveries", "snoozes", "batches", "qq_cutover", "handled", "runtime_actions",
                        "config_versions", "config_actions", "config_cutovers", "qq_capability_required", "qq_capabilities")
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

    def configuration_snapshot(self):
        return {"config": self.config(), "versions": self._state()["config_versions"]}

    def apply_configuration(self, plan, now, *, operation_id=None, authorized=False):
        """Publish the fields, dispatch policy, and execution proof together."""
        from desktop_calendar.reminders.reminder_config import put
        now = local_wall(now)
        current, state = self.config(), self._state()
        if operation_id and operation_id in state["config_actions"]:
            return state["config_actions"][operation_id]
        updated = verify_plan(plan, current, state["config_versions"])
        if plan["remote_authorization"] and not authorized:
            raise ReminderError("请明确授权涉及私人日历数据的设置。")
        qq_status, binding_scope = self.delivery.qq_status()
        if plan["remote_authorization"] and plan.get("binding_scope") != binding_scope:
            raise ReminderError("已核验的通知绑定发生变化，请重新预览并授权。")
        changed = bool(plan["fields"] or plan["rules"])
        proof = {"fields":plan["fields"], "changed":changed, "qq_status":qq_status,
                 "qq_enabled":any(active_qq(updated)), "recorded_at":now.isoformat(),
                 "remote_authorization":plan["remote_authorization"]}
        if not changed:
            return proof
        for field in plan["fields"]:
            versions = state["config_versions"]
            versions[field] = versions.get(field, 0) + 1
        self.delivery.prepare_config_change(state, current, updated, now)
        if operation_id:
            actions = state["config_actions"]
            actions[operation_id] = proof
            if len(actions) > 256:
                actions.pop(min(actions, key=lambda key: actions[key]["recorded_at"]))
        # Retain unrecognized settings and unrelated event overrides.
        raw = self.store.settings.get(CONFIG_KEY)
        stored = deepcopy(raw) if isinstance(raw, dict) else {}
        stored["legacy_explicit"] = updated["legacy_explicit"]
        for field, edit in plan["fields"].items():
            if field.startswith("brief.") and not isinstance(stored.get("brief"), dict):
                stored["brief"] = {}
            put(stored, field, edit["after"])
        if plan["rules"]:
            if not isinstance(stored.get("events"), dict):
                stored["events"] = {}
            for identifier, edit in plan["rules"].items():
                if edit["after"] is None:
                    stored["events"].pop(identifier, None)
                else:
                    stored["events"][identifier] = edit["after"]
        if normalize_config(stored) != updated:
            raise ReminderError("设置未通过保存前核验。")
        repository = self.store.repository
        options = {"operation_id":operation_id} if operation_id and callable(getattr(repository, "receipt", None)) else {}
        with self.delivery.configuration_guard():
            repository.apply_changes({}, preference_updates={CONFIG_KEY:stored, STATE_KEY:state}, **options)
            self.revision += 1
            self.delivery.refresh_configuration(current, updated)
        return proof

    def configuration_receipt(self, operation_id):
        return self._state()["config_actions"].get(operation_id)

    def remember_qq_capability(self):
        state = self._state()
        status, scope = self.delivery.qq_status(state)
        if status not in ("available", "unsupported"):
            return
        known = {"scope":scope, "available":status == "available"}
        if state["qq_capabilities"] != known:
            state["qq_capabilities"] = known
            self._save(STATE_KEY, state)

    def invalidate(self):
        self.dirty = True
        self.delivery.cancel_changed_notices()

    def replan(self, event_ids):
        """Rebuild after a commit. A later tick retries a failed rebuild."""
        self.invalidate()
        try:
            self._rebuild()
        except Exception as error:
            self.error = str(error)
            try:
                self.delivery.cancel_stale_events(event_ids)
            except Exception:
                # An unavailable backend cannot roll back the persisted operation.
                pass
            return False
        self.error = ""
        return True

    def _rebuild(self, *, refresh_brief=False):
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
                    event = self.store.event_by_id(identifier)
                    if event is not None and event.done and "base_delivery" in entry:
                        from desktop_calendar.reminders.reminder_runtime import may_have_sent
                        if not may_have_sent(state, entry["key"]):
                            original = entry["base_delivery"]
                            if isinstance(original, dict):
                                state["deliveries"][entry["base"]] = original
                            elif original is None:
                                state["deliveries"].pop(entry["base"], None)
                del state["snoozes"][identifier]
                changed = True
        self.delivery.remove_stale(state, refresh_brief=refresh_brief)
        if changed:
            self._save(STATE_KEY, state)
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
        config, plans, state = self.read_snapshot()
        return build_overview(self.store, tuple(plans.values()), state, local_wall(now), config)

    def read_snapshot(self):
        config, state = self.config(), self._state()
        plans = {plan.event_id: plan for plan in plans_for_store(self.store, config)}
        self._import_legacy(plans, state)
        return config, plans, state

    def runtime_snapshot(self, now):
        from desktop_calendar.reminders.reminder_runtime import runtime_snapshot
        return runtime_snapshot(self, now)

    def runtime_receipt(self, operation_id):
        return self._state()["runtime_actions"].get(operation_id)

    def tick(self, now):
        self.delivery.tick(local_wall(now))
        return 2000

    def acknowledge(self, key, success, now, attempt=None, reason=""):
        """Record the Windows submission result."""
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
        result = self.apply_runtime_action({"action":"snooze","minutes":minutes},
            [{"key":key,"event_id":identifier} for identifier,key in members], now, strict=False)
        return result["count"]

    def apply_runtime_action(self, action, targets, now, *, operation_id=None, strict=True, qq_batch=None):
        """Revalidate and commit the runtime-only patch and its proof in one save."""
        from desktop_calendar.reminders.reminder_runtime import live_instances, may_have_sent, snooze_time
        now = local_wall(now)
        _, plans, state = self.read_snapshot()
        current = live_instances(self, plans, state)
        if operation_id and operation_id in state["runtime_actions"]:
            return state["runtime_actions"][operation_id]
        kind = action["action"]
        if kind not in ("snooze", "cancel_snooze", "acknowledge"):
            raise ReminderError("不支持的提醒操作。")
        if strict and not targets:
            raise ReminderError("没有可操作的提醒实例。")
        checked = []
        for target in targets:
            match = current.get(target["key"])
            valid = (match and match[0].event_id == target["event_id"] and
                     (not strict or target.get("revision") == match[1]) and
                     (kind != "cancel_snooze" or match[0].key != plans[match[0].event_id].key) and
                     (kind != "snooze" or match[0].key not in state["handled"]) and
                     (kind == "cancel_snooze" or now - match[0].when < CATCHUP))
            if not valid:
                if strict:
                    raise ReminderError("提醒已变化、失效或过期，请重新查询。")
                continue
            checked.append(match[0])
        if len({plan.event_id for plan in checked}) != len(checked):
            raise ReminderError("同一事项有重复提醒目标。")
        when = None
        if kind == "snooze":
            times = {snooze_time(action, now, plan.when if strict else None) for plan in checked}
            if len(times) > 1:
                raise ReminderError("这些提醒的预计延后时刻不同，请分别操作或指定同一个时刻。")
            when = next(iter(times), None)
        changed = False
        for plan in checked:
            base = plans[plan.event_id]
            snooze_may_have_sent = plan.key != base.key and may_have_sent(state, plan.key)
            if kind == "snooze":
                if plan.key != base.key and when == plan.when:
                    continue
                old = state["snoozes"].get(plan.event_id, {})
                original = (old.get("base_delivery") if old.get("base") == base.key
                            else state["deliveries"].get(base.key))
                nonce = operation_id or now.isoformat()
                key = hashlib.sha256(f"{base.key}:snooze:{plan.key}:{when.isoformat()}:{nonce}".encode()).hexdigest()
                marker = {"status":"snoozed","at":now.isoformat(),"attempts":0}
                state["deliveries"][base.key] = state["deliveries"][plan.key] = marker
                state["snoozes"][plan.event_id] = {"base":base.key,"key":key,"at":when.isoformat()}
                if not snooze_may_have_sent and ("base_delivery" in old or strict and old.get("base") != base.key):
                    state["snoozes"][plan.event_id]["base_delivery"] = original
                changed = True
            elif kind == "cancel_snooze":
                previous = state["snoozes"].pop(plan.event_id)
                state["deliveries"][plan.key] = {"status":"snoozed","at":now.isoformat(),"attempts":0}
                if "base_delivery" in previous and not snooze_may_have_sent and now - base.when < CATCHUP:
                    original = previous["base_delivery"]
                    if original is None:
                        state["deliveries"].pop(base.key, None)
                    elif isinstance(original, dict):
                        state["deliveries"][base.key] = original
                changed = True
            elif plan.key not in state["handled"]:
                state["handled"][plan.key] = now.isoformat()
                changed = True
        if qq_batch and checked and kind == "acknowledge":
            entry = state["batches"][qq_batch]["channels"]["qq"]
            if not entry.get("acknowledged_at"):
                entry["acknowledged_at"] = now.isoformat()
                entry["handled_keys"] = [plan.key for plan in checked]
                changed = True
        result = dict(action=kind, count=len(checked), changed=changed, keys=[plan.key for plan in checked], recorded_at=now.isoformat(),
                      until=when.isoformat(timespec="seconds") if when else None)
        if operation_id:
            state["runtime_actions"][operation_id] = result
            while len(state["runtime_actions"]) > 256:
                state["runtime_actions"].pop(min(state["runtime_actions"],
                    key=lambda key:state["runtime_actions"][key].get("recorded_at","")))
        if changed or operation_id:
            self._save(STATE_KEY, state)
        if changed and kind != "acknowledge":
            self.replan(plan.event_id for plan in checked)
        return result

    def verified_qq_batch(self, identifier, binding, now, attempt, notification_type):
        from desktop_calendar.reminders.notification_dispatch import read_time
        if binding is None or binding != self.delivery.binding():
            raise ReminderError("远程绑定已变更")
        state = self._state()
        batch = state["batches"].get(identifier, {})
        qq = batch.get("channels", {}).get("qq", {})
        config = self.config()
        kind = batch.get("kind")
        prefs = config["brief"] if kind == "brief" else config
        enabled = brief_enabled(prefs, batch.get("brief_slot", "morning")) if kind == "brief" else True
        if (not enabled or not prefs["qq"] or batch.get("scope") != binding.scope
                or kind not in ("event", "ddl", "brief") or qq.get("status") != "sent"
                or read_time(batch.get("expires"), now) <= now):
            raise ReminderError("提醒交互已失效")
        if attempt is not None and (qq.get("attempts") != attempt or qq.get("delivery_attempt",attempt) != attempt
                                    or notification_type is not None and kind != notification_type
                                    or kind != "brief" and "member_revisions" not in qq):
            raise ReminderError("通知发送记录已变化，无法核验此提醒交互")
        return state, config, batch, qq

    def apply_qq_action(self, identifier, action, binding, now, *, attempt=None, notification_type=None,
                        operation_id=None, index=None, minutes=10):
        """Apply a verified action to the original attempt's still-current members."""
        from desktop_calendar.reminders.reminder_runtime import delivery_member_revision, live_instances
        now = local_wall(now)
        state, config, batch, qq = self.verified_qq_batch(identifier,binding,now,attempt,notification_type)
        if batch["kind"] == "brief":
            raise ReminderError("每日简报不支持修改提醒实例")
        members = qq.get("members")
        if members is None:
            if qq.get("attempts") != 1:
                raise ReminderError("旧通知没有可核验的发送成员，请按事项查询。")
            members = batch["members"]
        if config["private"] and action != "acknowledge":
            raise ReminderError("当前通知已启用隐私保护")
        if index is not None:
            if not 1 <= index <= min(20,len(members)):
                raise ReminderError("该编号不在原通知展示范围内")
            members = members[index-1:index]
        proof = state["runtime_actions"].get(operation_id) if operation_id else None
        if proof is None:
            _, plans, snapshot = self.read_snapshot()
            current = live_instances(self, plans, snapshot)
            revisions = qq.get("member_revisions", {})
            live = [(event_id,key) for event_id,key in members if key in current
                    and now-current[key][0].when < CATCHUP and
                    (attempt is None or revisions.get(key) == delivery_member_revision(self.store,event_id,key))]
        else:
            live = ()
        if action == "view_items":
            from desktop_calendar.reminders.notification_model import event_items
            return {"items":event_items(self.store,live,False)}
        if action not in ("acknowledge","snooze_10m"):
            raise ReminderError("不支持的提醒交互")
        if action == "snooze_10m" and not 1 <= minutes <= 1440:
            raise ReminderError("稍后提醒须在 24 小时内")
        if proof is None:
            if action == "snooze_10m":
                live = [(event_id,key) for event_id,key in live if key not in state["handled"]]
            if attempt is not None and not live:
                raise ReminderError("原通知中没有仍有效且可操作的提醒实例")
            patch = {"action":"acknowledge"} if action == "acknowledge" else {"action":"snooze","minutes":minutes}
            proof = self.apply_runtime_action(patch,[{"event_id":event_id,"key":key} for event_id,key in live],
                                              now,strict=False,qq_batch=identifier if index is None else None,
                                              operation_id=operation_id)
        if operation_id:
            return proof
        return {"acknowledged":bool(proof["count"])} if action == "acknowledge" else {"snoozed":proof["count"]}

    def cancel_snooze(self, event_id, *, key=None, now=None):
        _, plans, state = self.read_snapshot()
        base = plans.get(event_id)
        record = state["snoozes"].get(event_id)
        if base is None or record is None:
            return False
        try:
            current = snoozed_plan(base, record)
        except (ValueError, TypeError, KeyError):
            # The local reminder center can clear a malformed legacy record.
            if key is not None and key != base.key:
                return False
            del state["snoozes"][event_id]
            self._save(STATE_KEY, state)
            self.replan((event_id,))
            return True
        if current.key == base.key or key is not None and key != current.key:
            return False
        return bool(self.apply_runtime_action({"action":"cancel_snooze"},
            [{"event_id":event_id,"key":current.key}], now or datetime.now(), strict=False)["count"])

    def activation_is_current(self, event_id, key):
        if self.dirty:
            self._rebuild()
        plan = self.plans.get(event_id)
        state = self._state()
        snooze = state["snoozes"].get(event_id, {})
        snooze = snooze if isinstance(snooze, dict) else {}
        return bool(plan and key == (snooze.get("key") if snooze.get("base") == plan.key else plan.key)
                    and state["deliveries"].get(key, {}).get("status") != "snoozed")
