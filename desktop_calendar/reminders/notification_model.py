"""Content shared by the reminder channels. No UI or transport dependencies."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import json

from calendar_core import normalize_reminder_time

BRIEF_DEFAULTS = {
    "enabled": False, "time": "08:00", "timed": True, "untimed": True,
    "unfinished": True, "upcoming": True, "overdue": True, "days": 3,
    "send_empty": False, "windows": True, "qq": False,
}
BRIEF_CATCHUP = timedelta(hours=4)
BRIEF_GROUP_LABELS = {"timed": "当天日程", "untimed": "全天及无具体时间", "unfinished": "当天未完成",
                      "upcoming": "近期 DDL", "overdue": "已逾期 DDL"}


def brief_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {key: raw.get(key, default) is True for key, default in BRIEF_DEFAULTS.items()
              if isinstance(default, bool)}
    result["time"] = normalize_reminder_time(raw.get("time")) or BRIEF_DEFAULTS["time"]
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

    @property
    def tag(self):
        return self.key[:16] if self.attempt == 1 else identity(self.key, self.attempt)[:16]

    @property
    def activation(self):
        return "batch=" + self.key

    @property
    def title(self):
        return {"event": "日程提醒", "ddl": "DDL 提醒", "brief": "每日行程简报",
                "test": "测试通知"}[self.kind]

    def summary(self, limit=3):
        if self.kind == "test":
            return "这是一条不含日历数据的测试通知。"
        if not self.items:
            return "今天没有需要列入简报的事项。"
        if not any("title" in item for item in self.items):
            return f"共 {len(self.items)} 项，打开桌面月历查看。"
        lines = []
        group = None
        for item in self.items[:limit]:
            if self.kind == "brief" and limit > 3 and item.get("group") != group:
                group = item.get("group")
                if group in BRIEF_GROUP_LABELS:
                    lines.append(BRIEF_GROUP_LABELS[group])
            context = item.get("time") or (item.get("date") if item.get("group") in ("upcoming", "overdue") else "")
            line = " ".join(filter(None, (context, item.get("title")))).replace("\n", " ")
            lines.append(line[:200 if limit > 3 else 64])
        if len(self.items) > limit:
            lines.append(f"共 {len(self.items)} 项，查看完整列表")
        return "\n".join(lines)


def event_items(store, members, private):
    if private:
        return tuple({} for _ in members)
    return tuple({"event_id": event.id, "title": str(event.title)[:200]}
                 for event_id, _key in members if (event := store.event_by_id(event_id)) is not None)


def brief_items(store, now, config, private):
    """Each item goes to the first enabled section it matches."""
    today = now.date()
    end = today + timedelta(days=config["days"])
    groups = {key: [] for key in ("timed", "untimed", "unfinished", "upcoming", "overdue")}
    for event in store.events:
        try:
            occurs_today = store.event_covers(event, today)
            deadline = store.event_has_deadline(event) and not event.done
            last_day = store.event_end_date(event) if deadline else today
            overdue = deadline and store.is_event_overdue(event, now)
            clock = event.due_at.strftime("%H:%M") if event.has_time else ""
        except (ValueError, TypeError, OverflowError):
            continue
        if occurs_today and event.has_time and config["timed"]:
            section = "timed"
        elif occurs_today and not event.has_time and config["untimed"]:
            section = "untimed"
        elif occurs_today and not event.done and config["unfinished"]:
            section = "unfinished"
        elif overdue and config["overdue"]:
            section = "overdue"
        elif deadline and today <= last_day <= end and not overdue and config["upcoming"]:
            section = "upcoming"
        else:
            continue
        item = {} if private else {"group": section, "event_id": event.id, "title": str(event.title)[:200],
                                    "time": clock, "date": (last_day if deadline else today).isoformat()}
        groups[section].append(((clock, str(event.title), event.id), item))
    return tuple(item for group in groups.values() for _sort, item in sorted(group))
