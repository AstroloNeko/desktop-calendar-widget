"""Durable business batches. Calendar data and state stay owned by the desktop thread."""
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta
import heapq

from desktop_calendar.reminders.notification_model import (
    BRIEF_SLOTS, Notice, brief_content, brief_enabled, brief_policy_key, brief_window, event_items, identity,
)
from desktop_calendar.reminders.reminder_engine import (
    CATCHUP, RETRY, SNOOZES, STATE_KEY, local_wall, notification_arguments, plans_for_store, snoozed_plan,
)

ACK_TIMEOUT = timedelta(seconds=45)
MAX_ATTEMPTS = 3
TERMINAL = {"submitted", "sent", "unknown", "expired", "cancelled", "skipped", "user_opt_out"}
STATUS_LABELS = {
    "pending": "待发送", "dispatching": "待发送 · 等待结果", "submitted": "Windows 已提交",
    "sent": "QQ 发送成功", "failed": "明确发送失败", "unknown": "发送结果不确定",
    "expired": "已过期", "cancelled": "已取消", "skipped": "无事项，已跳过",
    "rate_limited": "发送受限频", "user_opt_out": "用户已关闭主动通知",
}


def read_time(value, fallback):
    try:
        return local_wall(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return fallback


class DeliveryQueue:
    def __init__(self, engine, qq):
        self.engine, self.qq = engine, qq
        self.inflight = set()
        self.current_notices = {}
        self.qq_notices = {}
        self.replace_on_cancel = set()
        self.scope = ""

    def binding(self):
        return self.qq.binding() if self.qq is not None else None

    def qq_status(self, state=None):
        binding = self.binding()
        if binding is None:
            return "unbound", None
        capability = getattr(self.qq, "proactive_capability", None)
        verified = capability(binding) if capability else None
        if verified is not True and verified is not False:
            saved = (self.engine._state() if state is None else state)["qq_capabilities"]
            verified = saved.get("available") if saved.get("scope") == binding.scope else None
        return ("available" if verified is True else "unsupported" if verified is False else "unverified"), binding.scope

    def configuration_guard(self):
        stack = ExitStack()
        for backend in (self.qq, self.engine.backend):
            guard = getattr(type(backend), "configuration_guard", None)
            if guard is not None:
                stack.enter_context(guard(backend))
        return stack

    def prepare_config_change(self, state, before, after, now):
        from desktop_calendar.reminders.reminder_runtime import may_have_sent
        policies = [("event", before, after, True, True)]
        for slot in BRIEF_SLOTS:
            old, new = before["brief"], after["brief"]
            policies.append((brief_policy_key(slot), old, new, brief_enabled(old, slot), brief_enabled(new, slot)))
        for kind, old, new, was_enabled, is_enabled in policies:
            if before["private"] and not after["private"] and is_enabled and new["qq"]:
                state["qq_capability_required"][kind] = True
            for channel in ("windows", "qq"):
                if is_enabled and new[channel] and not (was_enabled and old[channel]):
                    state["config_cutovers"][f"{kind}.{channel}"] = now.isoformat()
                    if channel == "qq":
                        state["qq_capability_required"][kind] = True
        if after["enabled"] and not before["enabled"]:
            for channel in ("windows", "qq"):
                state["config_cutovers"][f"default_event.{channel}"] = now.isoformat()
            if after["qq"]:
                state["qq_capability_required"]["event"] = True
        old_plans = {plan.event_id:plan for plan in plans_for_store(self.engine.store, before)}
        history = defaultdict(set)
        for batch in state["batches"].values():
            for identifier, key in batch.get("members", ()):
                history[identifier].add(key)
        for plan in plans_for_store(self.engine.store, after):
            prior = old_plans.get(plan.event_id)
            if prior and prior.key != plan.key and plan.when <= now and plan.key not in state["deliveries"]:
                keys = history[plan.event_id] | {prior.key}
                if any(may_have_sent(state, key) for key in keys):
                    state["deliveries"][plan.key] = {"status":"expired","at":now.isoformat(),"attempts":0}
        for batch in state["batches"].values():
            for channel, entry in batch["channels"].items():
                prefs = after["brief"] if batch["kind"] == "brief" else after
                enabled = brief_enabled(prefs, batch.get("brief_slot", "morning")) if batch["kind"] == "brief" else True
                if not (enabled and prefs[channel]) and entry["status"] in ("pending", "failed"):
                    entry.update(status="cancelled", reason="提醒或通知通道设置已关闭")

    def refresh_configuration(self, before, after):
        engine = self.engine
        engine.invalidate()
        try:
            if self.qq is not None:
                self.qq.set_privacy(after["private"])
            engine._rebuild(refresh_brief=before["brief"] != after["brief"])
            engine.error = ""
        except Exception as error:
            engine.error = str(error)
            try:
                self.cancel_stale_events(plan.event_id for plan in engine.plans.values())
            except Exception:
                pass

    @staticmethod
    def past_cutover(state, kind, channel, scheduled):
        cutoff = read_time(state["config_cutovers"].get(f"{kind}.{channel}"), scheduled)
        return scheduled >= cutoff

    def past_event_cutover(self, state, channel, plan, config):
        if not self.past_cutover(state, "event", channel, plan.when):
            return False
        rule = config["events"].get(plan.event_id, {})
        independent = (rule.get("mode") in ("relative", "absolute", "custom")
                       and (rule.get("explicit") or config["legacy_explicit"]))
        return independent or self.past_cutover(state, "default_event", channel, plan.when)

    def qq_eligible(self, plan, state, now):
        binding = self.binding()
        cutover = state["qq_cutover"]
        return bool(binding and cutover.get("scope") == binding.scope
                    and plan.when >= read_time(cutover.get("at"), now)
                    and self.past_event_cutover(state, "qq", plan, self.engine.config())
                    and (not state["qq_capability_required"].get("event") or self.qq_status(state)[0] == "available"))

    def cancel_stale_events(self, event_ids):
        """Fail closed for affected queued attempts if a rebuild could not finish."""
        ids = set(event_ids)
        for tag, (notice, members, _) in list(self.current_notices.items()):
            if any(identifier in ids for identifier, _ in members):
                if any(identifier not in ids for identifier, _ in members):
                    self.replace_on_cancel.add((notice.key, "windows", notice.attempt))
                self.engine.backend.remove(tag)
                del self.current_notices[tag]
        for task, (members, _, _) in list(self.qq_notices.items()):
            if any(identifier in ids for identifier, _ in members):
                if any(identifier not in ids for identifier, _ in members):
                    self.replace_on_cancel.add((task[0], "qq", task[1]))
                self.qq.cancel(*task)
                del self.qq_notices[task]

    def cancel_changed_notices(self):
        """Invalidate queued QQ payloads after an owner-thread event mutation."""
        if not (self.qq_notices or self.current_notices):
            return
        from desktop_calendar.reminders.reminder_runtime import delivery_member_revision
        state = self.engine._state()
        self.remove_stale(state)
        for task, (members, _private, notice) in list(self.qq_notices.items()):
            if notice.kind == "brief":
                continue
            identifier, attempt = task
            qq = state["batches"][identifier]["channels"]["qq"]
            versions = qq.get("member_revisions",{})
            if versions and any(versions[key] != delivery_member_revision(self.engine.store,event_id,key)
                                for event_id,key in members):
                self.replace_on_cancel.add((identifier,"qq",attempt))
                self.qq.cancel(*task)
                del self.qq_notices[task]

    def tick(self, now):
        from desktop_calendar.reminders.reminder_runtime import delivery_member_revision
        e = self.engine
        config = e._config_view()
        brief = config["brief"]
        binding = self.binding() if config["qq"] or brief["qq"] and any(brief_enabled(brief, slot) for slot in BRIEF_SLOTS) else None
        scope = binding.scope if binding else ""
        if scope != self.scope:
            self.scope = scope
            e.invalidate()
        if e.dirty:
            e._rebuild()
        state = e._state_view()
        mutable = False

        def ensure_mutable():
            nonlocal state, mutable
            if not mutable:
                state = e._state()
                mutable = True
            return state

        changed = False
        due = defaultdict(list)
        dispatched = []
        if scope and state["qq_cutover"].get("scope") != scope:
            ensure_mutable()
            state["qq_cutover"] = {"scope": scope, "at": now.isoformat()}
            changed = True
        qq_scope = scope or state["qq_cutover"].get("scope", "")
        qq_since = read_time(state["qq_cutover"].get("at"), now)
        verified_qq = binding is not None and self.qq_status(state)[0] == "available"

        while e.queue and e.queue[0][0] <= now:
            _, _, plan = heapq.heappop(e.queue)
            event = e.store.event_by_id(plan.event_id)
            if event is None or event.done:
                continue
            record = state["deliveries"].get(plan.key, {})
            record = record if isinstance(record, dict) else {}
            status = record.get("status")
            if status == "dispatching":
                ensure_mutable()
                delivery_entry = state["deliveries"].get(plan.key)
                if isinstance(delivery_entry, dict):
                    delivery_entry["status"] = "unknown"
                changed = True
                continue
            if record.get("batch") or status in ("accepted", "expired", "exhausted", "snoozed", "unknown"):
                continue
            channels = (["windows"] if config["windows"] and self.past_event_cutover(state, "windows", plan, config) else []) + (
                ["qq"] if config["qq"] and qq_scope and plan.when >= qq_since
                 and self.past_event_cutover(state, "qq", plan, config) else [])
            if now - plan.when >= CATCHUP or not channels:
                continue
            attempts = record.get("attempts", 0)
            if record.get("status") == "failed" and (type(attempts) is not int or attempts >= MAX_ATTEMPTS):
                continue
            earliest = read_time(record.get("at"), plan.when) + RETRY
            if record.get("status") == "failed" and earliest > now:
                heapq.heappush(e.queue, (earliest, plan.key, plan))
                continue
            minute = plan.when.replace(second=0, microsecond=0)
            window = (now.replace(second=0, microsecond=0)
                      if config["merge_backlog"] and now - plan.when >= timedelta(minutes=1) else minute)
            group = (window, "ddl" if plan.ddl else "event", tuple(channels))
            if not config["aggregate"]:
                group += (plan.key,)
            due[group].append(plan)

        if due:
            ensure_mutable()
        for group, plans in due.items():
            _, kind, channels = group[:3]
            members = [(plan.event_id, plan.key) for plan in plans]
            identifier = plans[0].key if len(plans) == 1 else identity(kind, sorted(key for _, key in members))
            triggered = min(plan.when for plan in plans)
            expiry = min(plan.when + CATCHUP for plan in plans)
            record = self.new_record(kind, triggered, expiry, members, channels, qq_scope)
            record["planned_at"] = {plan.key: plan.when.isoformat() for plan in plans}
            for _, key in members:
                legacy = state["deliveries"].get(key, {})
                if isinstance(legacy, dict) and legacy.get("status") == "failed" and "windows" in channels:
                    previous = legacy.get("attempts", 0)
                    if type(previous) is int and 0 < previous < MAX_ATTEMPTS:
                        record["channels"]["windows"]["attempts"] = max(previous, record["channels"]["windows"]["attempts"])
                state["deliveries"][key] = {"batch": identifier}
            state["batches"][identifier] = record
            changed = True

        for slot in BRIEF_SLOTS:
            if not brief_enabled(brief, slot):
                continue
            start, expires = brief_window(now.date(), brief, slot)
            if not start <= now < expires:
                continue
            policy = brief_policy_key(slot)
            windows = brief["windows"] and self.past_cutover(state, policy, "windows", start)
            qq = brief["qq"] and binding is not None and self.past_cutover(state, policy, "qq", start)
            for channel, account in ([("windows", "")] if windows else []) + ([("qq", binding.account_scope)] if qq else []):
                identifier = identity(policy, channel, now.date().isoformat(), account)
                if identifier not in state["batches"]:
                    ensure_mutable()
                    record = self.new_record("brief", start, expires, (), (channel,), scope)
                    record["brief_slot"] = slot
                    state["batches"][identifier] = record
                    changed = True

        for identifier in tuple(state["batches"]):
            record = state["batches"].get(identifier)
            if not isinstance(record, dict):
                continue
            for channel in tuple(record["channels"]):
                record = state["batches"][identifier]
                entry = record["channels"][channel]

                def mutable_batch():
                    ensure_mutable()
                    mut_record = state["batches"][identifier]
                    return mut_record, mut_record["channels"][channel]

                task = (identifier, channel, entry["attempts"])
                status = entry["status"]
                if status != "dispatching" and not (
                    status == "cancelled" and entry.get("paused_for_completion")
                ) and (status in TERMINAL or entry["attempts"] >= MAX_ATTEMPTS):
                    continue
                if status == "dispatching":
                    if task not in self.inflight or read_time(entry["at"], now) + ACK_TIMEOUT <= now:
                        record, entry = mutable_batch()
                        entry.update(status="unknown", reason="提交结果未持久化或等待超时")
                        self.inflight.discard(task)
                        self.replace_on_cancel.discard(task)
                        changed = True
                    continue
                if status == "cancelled" and entry.get("paused_for_completion") and self.current_members(record, state):
                    record, entry = mutable_batch()
                    entry.update(status="pending", paused_for_completion=False)
                    status = "pending"
                    changed = True
                if status in TERMINAL or entry["attempts"] >= MAX_ATTEMPTS:
                    continue
                is_brief = record["kind"] == "brief"
                slot = record.get("brief_slot", "morning")
                if is_brief and read_time(record["when"], now).date() == now.date():
                    start, expires = brief_window(now.date(), brief, slot)
                    if (record["when"], record["expires"]) != (start.isoformat(), expires.isoformat()):
                        record, entry = mutable_batch()
                        record.update(when=start.isoformat(), expires=expires.isoformat())
                        changed = True
                expiry = read_time(record["expires"], now)
                if expiry <= now:
                    record, entry = mutable_batch()
                    entry.update(status="expired", reason="已超过补发有效期")
                    changed = True
                    continue
                prefs = brief if is_brief else config
                if (is_brief and not brief_enabled(brief, slot)) or not prefs[channel] or (channel == "qq" and record["scope"] != qq_scope):
                    record, entry = mutable_batch()
                    entry.update(status="cancelled", reason="提醒、通道或绑定已变更")
                    changed = True
                    continue
                kind = brief_policy_key(slot) if is_brief else "event"
                if not self.past_cutover(state,kind,channel,read_time(record["when"],now)):
                    record, entry = mutable_batch()
                    entry.update(status="cancelled", reason="该历史通知早于当前通道启用时刻")
                    changed = True
                    continue
                if channel == "qq" and not binding:
                    if status == "pending" and entry.get("reason") != "等待核验原绑定账号":
                        record, entry = mutable_batch()
                        entry["reason"] = "等待核验原绑定账号"
                        changed = True
                    continue
                if channel == "qq" and state["qq_capability_required"].get(kind) and not verified_qq:
                    if entry.get("reason") != "等待核验QQ主动推送能力":
                        record, entry = mutable_batch()
                        entry["reason"] = "等待核验QQ主动推送能力"
                        changed = True
                    continue
                if is_brief and read_time(record.get("when"), now) > now:
                    continue
                if status in ("failed","rate_limited"):
                    if not entry.get("retryable", channel == "windows"):
                        continue
                    if read_time(entry.get("retry_at"),read_time(entry["at"],now)+RETRY) > now:
                        continue
                members = self.current_members(record, state)
                if not is_brief:
                    members = [(event_id, key) for event_id, key in members
                               if self.past_event_cutover(state, channel,
                                   snoozed_plan(e.plans[event_id], state["snoozes"].get(event_id)), config)]
                if not is_brief and not members:
                    completed = any(event.done for event_id, _ in record["members"]
                                    if (event := e.store.event_by_id(event_id)) is not None)
                    record, entry = mutable_batch()
                    entry.update(status="cancelled", reason="原事项提醒已失效", paused_for_completion=completed)
                    changed = True
                    continue
                private = config["private"]
                has_content = False
                progress = None
                if is_brief:
                    local_items, progress = brief_content(e.store, read_time(record["when"], now), brief, slot=slot)
                    has_content = bool(local_items or progress and progress.total)
                    record, entry = mutable_batch()
                    record["brief_items"] = [{key: value for key, value in item.items() if key != "title"}
                                             for item in local_items]
                    items = tuple({} for _ in local_items) if private else local_items
                    if private:
                        progress = None
                else:
                    items = event_items(e.store, members, private)
                if is_brief and not has_content and not brief["send_empty"]:
                    record, entry = mutable_batch()
                    entry.update(status="skipped")
                    changed = True
                    continue
                attempt = entry["attempts"] + 1
                notice = Notice(identifier, record["kind"], read_time(record["when"], now), expiry, items,
                                members[0][0] if len(members) == 1 and len(record["members"]) == 1 else "",
                                attempt, record["scope"], brief_slot=slot, progress=progress)
                record, entry = mutable_batch()
                entry.update(status="dispatching", attempts=attempt, at=now.isoformat(), reason="", tag=notice.tag)
                if not is_brief:
                    entry["members"] = [list(member) for member in members]
                    if channel == "qq":
                        entry["member_revisions"] = {key:delivery_member_revision(e.store,event_id,key)
                                                     for event_id,key in members}
                dispatched.append((channel, notice, members, binding))
                changed = True

        if changed:
            try:
                e._save(STATE_KEY, state)
            except Exception:
                e.invalidate()
                raise
        for channel, notice, members, binding in dispatched:
            task = (notice.key, channel, notice.attempt)
            self.inflight.add(task)
            if channel == "windows":
                self.current_notices[notice.tag] = (notice, members, config["private"])
            else:
                self.qq_notices[(notice.key, notice.attempt)] = (members, config["private"], notice)
            try:
                if channel == "windows":
                    self.send_windows(notice, members)
                else:
                    self.qq.send(notice, binding, config["private"])
            except Exception:
                try:
                    from desktop_calendar.reminders.qq_reminders import DeliveryDetail
                    self.acknowledge(notice.key, channel, notice.attempt, "failed", now,
                                     DeliveryDetail("通知无法加入发送队列"))
                except (OSError, ValueError, RuntimeError) as error:
                    e.error = str(error)

    @staticmethod
    def new_record(kind, when, expires, members, channels, scope):
        return {"kind": kind, "when": when.isoformat(), "expires": expires.isoformat(),
                "members": [list(member) for member in members], "scope": scope if "qq" in channels else "",
                "channels": {channel: {"status": "pending", "attempts": 0} for channel in channels}}

    def current_members(self, record, state):
        members = []
        for event_id, key in record["members"]:
            plan = self.engine.plans.get(event_id)
            event = self.engine.store.event_by_id(event_id)
            if plan is not None and event is not None and not event.done:
                try:
                    entry = state["deliveries"].get(key, {})
                    disabled = isinstance(entry, dict) and entry.get("status") == "snoozed"
                    if not disabled and snoozed_plan(plan, state["snoozes"].get(event_id)).key == key:
                        members.append((event_id, key))
                except (ValueError, TypeError, KeyError):
                    continue
        return members

    def remove_stale(self, state, *, refresh_brief=False):
        config = self.engine.config()
        for tag, (notice, members, private) in list(self.current_notices.items()):
            is_brief = notice.kind == "brief"
            prefs = config["brief"] if is_brief else config
            enabled = prefs["windows"] and (not is_brief or brief_enabled(prefs, notice.brief_slot))
            live = self.current_members({"members": members}, state)
            task = (notice.key, "windows", notice.attempt)
            needs_update = (private != config["private"] or len(live) != len(members)
                            or is_brief and task in self.inflight
                            and (refresh_brief or self.brief_changed(notice, prefs, private)))
            if not enabled or needs_update:
                if enabled and task in self.inflight:
                    self.replace_on_cancel.add(task)
                self.engine.backend.remove(tag)
                del self.current_notices[tag]
            elif datetime.now() >= notice.expires:
                del self.current_notices[tag]
        for task, (members, private, notice) in list(self.qq_notices.items()):
            kind = notice.kind
            prefs = config["brief"] if kind == "brief" else config
            slot = state["batches"].get(task[0], {}).get("brief_slot", "morning")
            enabled = prefs["qq"] and (kind != "brief" or brief_enabled(prefs, slot))
            live = self.current_members({"members": members}, state)
            if not enabled or private != config["private"] or len(live) != len(members) or kind == "brief" and (
                    refresh_brief or self.brief_changed(notice, prefs, private)):
                if enabled and (task[0], "qq", task[1]) in self.inflight:
                    self.replace_on_cancel.add((task[0], "qq", task[1]))
                self.qq.cancel(*task)
                del self.qq_notices[task]

    def brief_changed(self, notice, prefs, private):
        items, progress = brief_content(self.engine.store, notice.when, prefs, slot=notice.brief_slot)
        if private:
            return len(items) != len(notice.items)
        return items != notice.items or progress != notice.progress

    def send_windows(self, notice, members):
        actions = []
        text = notice.windows_summary()
        if notice.event_id:
            plan_key = members[0][1]
            if not any("title" in item for item in notice.items):
                text = "你有一项日程需要处理"
            # Preserve the existing single-item actions and validated plan identity.
            from desktop_calendar.reminders.reminder_engine import ReminderPlan
            plan = ReminderPlan(notice.event_id, plan_key, notice.when, "", notice.kind == "ddl")
            actions = [(f"{minutes} 分钟后", notification_arguments(plan, str(minutes))) for minutes in SNOOZES]
        self.engine.backend.send(notice, "桌面月历 · " + notice.title, text, actions)

    def acknowledge(self, identifier, channel, attempt, status, now, reason=""):
        state = self.engine._state()
        entry = state["batches"].get(identifier, {}).get("channels", {}).get(channel)
        if not entry or attempt != entry["attempts"]:
            return False
        allowed = entry["status"] in ("dispatching", "unknown") or (
            status == "failed" and channel == "windows" and entry["status"] == "submitted")
        if not allowed:
            return False
        task = (identifier, channel, attempt)
        replace = task in self.replace_on_cancel or (channel == "qq" and reason == "隐私策略已更新")
        if status == "cancelled" and replace and attempt < MAX_ATTEMPTS:
            status = "pending"
        entry.update(status=status, at=now.isoformat(), reason=reason[:200])
        if status in ("failed", "rate_limited"):
            entry["retryable"] = getattr(reason,"retryable",status == "failed")
            delay = getattr(reason,"delay",None)
            entry["retry_at"] = (now + (timedelta(seconds=min(delay,172800)) if delay else RETRY)).isoformat()
        if actual_attempt := getattr(reason,"attempt",None):
            entry["delivery_attempt"] = actual_attempt
        if submission := getattr(reason,"submission",None):
            entry["submission"] = submission
        self.engine._save(STATE_KEY, state)
        self.inflight.discard(task)
        self.replace_on_cancel.discard(task)
        if channel == "qq":
            self.qq_notices.pop((identifier, attempt), None)
        return True

    def acknowledge_tag(self, tag, now, reason):
        if len(tag) != 16:
            return
        for identifier, record in self.engine._state()["batches"].items():
            entry = record["channels"].get("windows", {})
            if entry.get("tag") == tag:
                self.acknowledge(identifier, "windows", entry["attempts"], "failed", now, reason)
                return
