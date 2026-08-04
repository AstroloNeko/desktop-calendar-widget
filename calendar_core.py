from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional


APP_NAME = "桌面月历"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "DesktopCalendar"
DATA_FILE = DATA_DIR / "calendar_data.json"

COLORS = {
    "海盐蓝": "#6687F2",
    "薄荷绿": "#52B788",
    "日光黄": "#E5A927",
    "暖橙色": "#EB7C4D",
    "珊瑚红": "#E65D67",
    "鸢尾紫": "#8B70D6",
}
PRIORITIES = ("低", "普通", "高", "紧急")
REMINDERS = {
    "不提醒": None,
    "准时": 0,
    "提前 10 分钟": 10,
    "提前 30 分钟": 30,
    "提前 1 小时": 60,
    "提前 1 天": 1440,
    "提前 3 天": 4320,
    "提前 1 周": 10080,
}
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ROUTINE_KINDS = ("habit", "todo")


@dataclass
class Event:
    id: str
    title: str
    due: str
    color: str = "#6687F2"
    priority: str = "普通"
    reminder: Optional[int] = 60
    notes: str = ""
    done: bool = False
    created_at: str = ""
    snooze_until: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if self.priority not in PRIORITIES:
            self.priority = "普通"
        if not isinstance(self.color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color):
            self.color = COLORS["海盐蓝"]

    @property
    def due_at(self) -> datetime:
        return datetime.fromisoformat(self.due)

    @property
    def due_date(self) -> date:
        return self.due_at.date()

    @property
    def is_overdue(self) -> bool:
        return not self.done and self.due_at < datetime.now()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        allowed = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in allowed}
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ValueError("invalid event id")
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            raise ValueError("invalid event title")
        if not isinstance(data.get("due"), str):
            raise ValueError("invalid due time")
        datetime.fromisoformat(data["due"])
        if data.get("reminder") is not None:
            reminder = data.get("reminder")
            if not isinstance(reminder, int) or reminder < 0:
                data["reminder"] = 60
        if not isinstance(data.get("notes", ""), str):
            data["notes"] = str(data.get("notes", ""))
        data["done"] = bool(data.get("done", False))
        snooze = data.get("snooze_until")
        if snooze:
            try:
                datetime.fromisoformat(snooze)
            except (TypeError, ValueError):
                data["snooze_until"] = None
        return cls(**data)


@dataclass
class RoutineItem:
    id: str
    title: str
    kind: str = "habit"
    color: str = "#52B788"
    created_on: str = ""
    completed_on: Optional[str] = None
    habit_done: list[str] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.created_on:
            self.created_on = date.today().isoformat()
        date.fromisoformat(self.created_on)
        if self.kind not in ROUTINE_KINDS:
            self.kind = "habit"
        if not isinstance(self.color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color):
            self.color = COLORS["薄荷绿"]
        normalized: list[str] = []
        for value in self.habit_done:
            if not isinstance(value, str):
                continue
            try:
                date.fromisoformat(value)
            except ValueError:
                continue
            if value not in normalized:
                normalized.append(value)
        self.habit_done = sorted(normalized)
        if self.completed_on:
            try:
                date.fromisoformat(self.completed_on)
            except (TypeError, ValueError):
                self.completed_on = None
        self.enabled = bool(self.enabled)

    @property
    def created_date(self) -> date:
        return date.fromisoformat(self.created_on)

    def is_done_on(self, day: date) -> bool:
        if self.kind == "habit":
            return day.isoformat() in self.habit_done
        return self.completed_on == day.isoformat()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoutineItem":
        allowed = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in allowed}
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ValueError("invalid routine id")
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            raise ValueError("invalid routine title")
        if not isinstance(data.get("habit_done", []), list):
            data["habit_done"] = []
        return cls(**data)


DEFAULT_SETTINGS = {
    "window_mode": "desktop",
    "agenda_open": True,
    "opacity": 1.0,
    "x": None,
    "y": None,
    "default_reminder": 60,
    "show_holidays": True,
    "routine_reminder_enabled": True,
    "routine_reminder_time": "09:00",
}


class Store:
    def __init__(self, data_file: Optional[Path] = None) -> None:
        self.data_file = data_file or DATA_FILE
        self.events: list[Event] = []
        self.routines: list[RoutineItem] = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.notified: set[str] = set()
        self.load_error: Optional[str] = None
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.data_file.read_text(encoding="utf-8"))
            loaded: list[Event] = []
            for item in raw.get("events", []):
                try:
                    loaded.append(Event.from_dict(item))
                except (TypeError, ValueError, KeyError):
                    continue
            self.events = loaded
            routines: list[RoutineItem] = []
            for item in raw.get("routines", []):
                try:
                    routines.append(RoutineItem.from_dict(item))
                except (TypeError, ValueError, KeyError):
                    continue
            self.routines = routines
            settings = raw.get("settings", {})
            if isinstance(settings, dict):
                self.settings.update(settings)
            # Migrate the first prototype's settings without changing the data.
            if "topmost" in settings and "window_mode" not in settings:
                # The redesigned gadget intentionally defaults to the desktop layer,
                # even when the old oversized window happened to be pinned.
                self.settings["window_mode"] = "desktop"
            if "compact" in settings and "agenda_open" not in settings:
                self.settings["agenda_open"] = not bool(settings.get("compact"))
            self.notified = {item for item in raw.get("notified", []) if isinstance(item, str)}
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.load_error = str(exc)

    def save(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        notification_history = sorted(self.notified, key=lambda item: item[-16:])[-600:]
        self.notified = set(notification_history)
        payload = {
            "version": 3,
            "events": [asdict(event) for event in self.events],
            "routines": [asdict(item) for item in self.routines],
            "settings": self.settings,
            "notified": notification_history,
        }
        temporary = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.data_file)

    def upsert(self, event: Event) -> None:
        previous = next((item for item in self.events if item.id == event.id), None)
        self.events = [item for item in self.events if item.id != event.id]
        self.events.append(event)
        if previous is None or previous.due != event.due or previous.reminder != event.reminder:
            self.clear_notifications(event.id)
        self.save()

    def delete(self, event_id: str) -> None:
        self.events = [item for item in self.events if item.id != event_id]
        self.clear_notifications(event_id)
        self.save()

    def clear_notifications(self, event_id: str) -> None:
        prefix = event_id + ":"
        self.notified = {key for key in self.notified if not key.startswith(prefix)}

    def events_on(self, day: date, include_done: bool = True) -> list[Event]:
        events = [item for item in self.events if item.due_date == day and (include_done or not item.done)]
        rank = {name: index for index, name in enumerate(PRIORITIES)}
        return sorted(events, key=lambda item: (item.done, item.due_at, -rank.get(item.priority, 1)))

    def upcoming(self, days: int = 7, include_overdue: bool = True) -> list[Event]:
        now = datetime.now()
        end = now + timedelta(days=days)
        return sorted(
            (
                item
                for item in self.events
                if not item.done and item.due_at <= end and (include_overdue or item.due_at >= now)
            ),
            key=lambda item: item.due_at,
        )

    def create_quick(self, title: str, day: date) -> Event:
        now = datetime.now()
        if day == now.date():
            due = now.replace(second=0, microsecond=0) + timedelta(minutes=30)
            due = due.replace(minute=(due.minute // 30) * 30)
        else:
            due = datetime.combine(day, datetime.min.time()).replace(hour=18)
        event = Event(
            id=str(uuid.uuid4()),
            title=title.strip(),
            due=due.isoformat(timespec="minutes"),
            color=COLORS["海盐蓝"],
            priority="普通",
            reminder=self.settings.get("default_reminder", 60),
        )
        self.upsert(event)
        return event

    def upsert_routine(self, item: RoutineItem) -> None:
        self.routines = [existing for existing in self.routines if existing.id != item.id]
        self.routines.append(item)
        self.save()

    def delete_routine(self, item_id: str) -> None:
        self.routines = [item for item in self.routines if item.id != item_id]
        self.save()

    def routines_on(self, day: date) -> list[RoutineItem]:
        visible: list[RoutineItem] = []
        for item in self.routines:
            if not item.enabled or day < item.created_date:
                continue
            if item.kind == "todo" and item.completed_on and day > date.fromisoformat(item.completed_on):
                continue
            visible.append(item)
        return sorted(visible, key=lambda item: (item.is_done_on(day), item.kind == "todo", item.created_on, item.title))

    def toggle_routine(self, item: RoutineItem, day: date) -> None:
        day_key = day.isoformat()
        if item.kind == "habit":
            if day_key in item.habit_done:
                item.habit_done.remove(day_key)
            else:
                item.habit_done.append(day_key)
                item.habit_done.sort()
        else:
            item.completed_on = None if item.completed_on == day_key else day_key
        self.upsert_routine(item)
