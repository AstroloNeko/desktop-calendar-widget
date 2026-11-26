"""Content shared by the reminder channels. No UI or transport dependencies."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json

from calendar_core import normalize_reminder_time

BRIEF_DEFAULTS = {
    "enabled": False, "time": "08:00", "timed": True, "untimed": True,
    "evening_enabled": False, "evening_time": "18:00", "completion": True,
    "unfinished": True, "upcoming": True, "overdue": True, "days": 3,
    "send_empty": False, "windows": True, "qq": False,
}
BRIEF_CATCHUP = timedelta(hours=4)
BRIEF_SLOTS = ("morning", "evening")
BRIEF_TITLES = {"morning": "晨间简报", "evening": "晚间简报"}
BRIEF_CLOCKS = {"morning": "time", "evening": "evening_time"}
BRIEF_SWITCHES = {"morning": "enabled", "evening": "evening_enabled"}


def brief_enabled(config, slot):
    return config[BRIEF_SWITCHES[slot]]


def brief_window(day, config, slot):
    start = datetime.combine(day, time.fromisoformat(config[BRIEF_CLOCKS[slot]]))
    cutoff = datetime.combine(day + timedelta(days=1), time())
    if slot == "morning":
        evening_start = datetime.combine(day, time.fromisoformat(config["evening_time"]))
        morning_limit = datetime.combine(day, time(18))
        if brief_enabled(config, "evening"):
            morning_limit = min(morning_limit, evening_start)
        # Retain a legacy late configured time, with no catch-up once the
        # morning window has closed.  The scheduled minute itself still works.
        cutoff = min(cutoff, max(start + timedelta(minutes=1), morning_limit))
    return start, min(start + BRIEF_CATCHUP, cutoff)


def brief_policy_key(slot):
    # The original daily brief ledger and channel cutovers belong to morning.
    return "brief" if slot == "morning" else "brief_evening"


def brief_heading(slot, when, today_count):
    if slot == "evening":
        return "今天辛苦了！"
    hour = when.hour
    greeting = "凌晨好" if hour < 5 else "早上好" if hour < 11 else "中午好" if hour < 13 else "下午好" if hour < 18 else "晚上好"
    return f"{greeting}，今天有 {today_count} 项待办" if today_count else f"{greeting}，今天暂无待办"


@dataclass(frozen=True)
class BriefProgress:
    total: int
    completed: int

    @property
    def remaining(self):
        return self.total - self.completed

    @property
    def percent(self):
        return round(100 * self.completed / self.total) if self.total else None

    def summary(self):
        if not self.total:
            return "今天没有可统计的事项。"
        return f"今日 {self.total} 项 · 已完成 {self.completed} 项 · 尚未完成 {self.remaining} 项 · 完成率 {self.percent}%"


def brief_item_context(item, day):
    if item["group"] in ("today_ddl", "upcoming", "overdue"):
        days = (datetime.fromisoformat(item["date"]).date() - day).days
        if item["group"] == "today_ddl":
            label = "今天已逾期" if item.get("overdue") else "今天到期"
            return " ".join(filter(None, (label, item.get("time"))))
        if item["group"] == "upcoming":
            return f"剩余 {days} 天" if days else "今天到期"
        return f"逾期 {-days} 天" if days < 0 else "今天逾期"
    return item.get("time", "")


def brief_today_count(items):
    return sum(item.get("group") in ("timed", "untimed", "unfinished", "today_ddl") for item in items)
BRIEF_GROUP_LABELS = {"timed": "当天日程", "untimed": "全天及无具体时间", "unfinished": "当天未完成",
                      "today_ddl": "今日 DDL", "upcoming": "近期 DDL", "overdue": "已逾期 DDL"}
BRIEF_GROUP_QUOTAS = {"timed": 5, "untimed": 3, "unfinished": 3, "today_ddl": 4, "upcoming": 4, "overdue": 3}


def select_brief_items(items):
    """Limit each section independently, preserving its order and full count."""
    shown = {group: [] for group in BRIEF_GROUP_QUOTAS}
    counts = dict.fromkeys(BRIEF_GROUP_QUOTAS, 0)
    for item in items:
        group = item["group"]
        counts[group] += 1
        if counts[group] <= BRIEF_GROUP_QUOTAS[group]:
            shown[group].append(item)
    return tuple(item for section in shown.values() for item in section), counts


def legacy_brief_projection(items, counts):
    """Project today's DDL onto the five groups accepted by older clients and cloud."""
    counts = dict(counts)
    counts["upcoming"] += counts.pop("today_ddl", 0)
    items = tuple({**item, "group": "upcoming"} if item["group"] == "today_ddl" else item for item in items)
    return items, counts


def compact_brief_line(text):
    # Leave space for every selected row and all headings in a remote plain-text reply.
    encoded = text.encode("utf-8")
    return text if len(encoded) <= 80 else encoded[:77].decode("utf-8", errors="ignore") + "…"


def brief_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {key: raw.get(key, default) is True for key, default in BRIEF_DEFAULTS.items()
              if isinstance(default, bool)}
    for field in BRIEF_CLOCKS.values():
        result[field] = normalize_reminder_time(raw.get(field)) or BRIEF_DEFAULTS[field]
    days = raw.get("days")
    result["days"] = days if type(days) is int and 1 <= days <= 30 else BRIEF_DEFAULTS["days"]
    return result


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Notice:
    key: str
    kind: str
    when: datetime
    expires: datetime
    items: tuple
    event_id: str = ""
    attempt: int = 1
    scope: str = ""
    manual_test: bool = False
    brief_slot: str = "morning"
    progress: BriefProgress | None = None
    brief_date: date | None = None

    @property
    def tag(self):
        return self.key[:16] if self.attempt == 1 else identity(self.key, self.attempt)[:16]

    @property
    def activation(self):
        return "batch=" + self.key

    @property
    def title(self):
        if self.kind == "brief":
            return BRIEF_TITLES[self.brief_slot]
        return {"event": "日程提醒", "ddl": "DDL 提醒", "test": "测试通知"}[self.kind]

    def windows_summary(self):
        if self.kind != "brief" or not any("group" in item for item in self.items):
            return self.summary()
        selected, _counts = select_brief_items(self.items)
        shown = sorted(selected, key=lambda item: item["group"] not in ("today_ddl", "overdue"))[:3]
        return self.summary(selected=shown, include_title=False)

    def summary(self, limit=3, *, selected=None, include_title=True):
        if self.kind == "test":
            return "这是一条不含日历数据的测试通知。"
        lines = []
        if self.kind == "brief":
            if self.progress is not None:
                lines.append(self.progress.summary())
            if selected is not None and include_title:
                lines.insert(0, self.title)
        if not self.items:
            if self.progress is None:
                lines.append("今天没有需要列入简报的事项。")
            return "\n".join(lines)
        if not any("title" in item for item in self.items):
            return f"共 {len(self.items)} 项，打开桌面月历查看。"
        shown = self.items[:limit] if selected is None else selected
        group = None
        for item in shown:
            if self.kind == "brief" and limit > 3 and item.get("group") != group:
                group = item.get("group")
                if group in BRIEF_GROUP_LABELS:
                    lines.append(BRIEF_GROUP_LABELS[group])
            context = brief_item_context(item, self.brief_date or self.when.date()) if self.kind == "brief" and "group" in item else item.get("time", "")
            line = " ".join(filter(None, (context, item.get("title")))).replace("\n", " ")
            lines.append(compact_brief_line(line) if selected is not None else line[:200 if limit > 3 else 64])
        if len(self.items) > len(shown):
            lines.append(f"共 {len(self.items)} 项，查看完整列表")
        return "\n".join(lines)


def event_items(store, members, private):
    if private:
        return tuple({} for _ in members)
    return tuple({"event_id": event.id, "title": str(event.title)[:200]}
                 for event_id, _key in members if (event := store.event_by_id(event_id)) is not None)


def brief_content(store, now, config, *, slot="morning", events=None, overdue_at=None, ddl_only=False):
    """Use effective dates and count each calendar ID once, independently of display options."""
    today = now.date()
    overdue_at = now if overdue_at is None else overdue_at
    end = today + timedelta(days=config["days"])
    groups = {key: [] for key in BRIEF_GROUP_LABELS}
    seen = set()
    total = completed = 0
    for event in store.events if events is None else events:
        try:
            deadline = store.event_has_deadline(event)
            last_day = store.event_end_date(event) if deadline else today
            occurs_today = last_day == today if deadline else store.event_covers(event, today)
            overdue = deadline and not event.done and store.is_event_overdue(event, overdue_at)
            clock = event.due_at.strftime("%H:%M") if event.has_time else ""
        except (ValueError, TypeError, OverflowError):
            continue
        if event.id in seen:
            continue
        seen.add(event.id)
        if occurs_today:
            total += 1
            completed += int(bool(event.done))
        if event.done:
            continue
        if deadline and occurs_today:
            section = "today_ddl"
        elif deadline and overdue and config["overdue"]:
            section = "overdue"
        elif deadline and today < last_day <= end and not overdue and config["upcoming"]:
            section = "upcoming"
        elif deadline or ddl_only or not occurs_today:
            continue
        elif event.has_time and config["timed"]:
            section = "timed"
        elif not event.has_time and config["untimed"]:
            section = "untimed"
        elif config["unfinished"]:
            section = "unfinished"
        else:
            continue
        item = {"group": section, "event_id": event.id, "title": str(event.title)[:200],
                "time": clock, "date": last_day.isoformat()}
        if deadline:
            item["overdue"] = overdue
        group_order = (-int(overdue), last_day if section == "upcoming" else today, clock, str(event.title), event.id)
        groups[section].append((group_order, item))
    items = tuple(item for group in groups.values() for _sort, item in sorted(group))
    progress = BriefProgress(total, completed) if slot == "evening" and config["completion"] and not ddl_only else None
    return items, progress


def brief_items(store, now, config, private, *, events=None, overdue_at=None):
    items, _progress = brief_content(store, now, config, events=events, overdue_at=overdue_at)
    return tuple({} for _ in items) if private else items
