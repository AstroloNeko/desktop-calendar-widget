from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from holiday_data import is_workday as system_is_workday
from ui_theme import DEFAULT_THEME_NAME, normalize_theme_name


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
EVENT_TYPES = ("general", "urgent", "ddl")
EVENT_TYPE_LABELS = {
    "general": "一般",
    "urgent": "紧急",
    "ddl": "DDL",
}
EVENT_TYPE_OPTIONS = tuple((value, EVENT_TYPE_LABELS[value]) for value in EVENT_TYPES)
EVENT_TYPE_RANK = {"ddl": 0, "urgent": 1, "general": 2}
LEGACY_EVENT_TYPE_MAP = {
    "low": "general",
    "normal": "general",
    "general": "general",
    "urgent": "urgent",
    "ddl": "ddl",
    "低": "general",
    "低优先级": "general",
    "普通": "general",
    "一般": "general",
    "普通优先级": "general",
    "一般优先级": "general",
    "高": "urgent",
    "高优先级": "urgent",
    "紧急": "urgent",
    "紧急优先级": "urgent",
    "DDL": "ddl",
    "deadline": "ddl",
    "截止": "ddl",
}
DATE_STATUSES = ("normal", "leave", "holiday")
DATE_STATUS_LABELS = {
    "normal": "正常",
    "leave": "请假",
    "holiday": "放假",
}
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


def normalize_event_type(value: object) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned in EVENT_TYPES:
            return cleaned
        return LEGACY_EVENT_TYPE_MAP.get(cleaned, "general")
    return "general"


def normalize_date_status(value: object) -> str:
    return value if isinstance(value, str) and value in DATE_STATUSES else "normal"


def normalize_reminder_time(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        return None
    return parsed.strftime("%H:%M")


@dataclass
class Event:
    id: str
    title: str
    due: str
    color: str = "#6687F2"
    event_type: str = "general"
    reminder: Optional[int] = 60
    notes: str = ""
    done: bool = False
    created_at: str = ""
    snooze_until: Optional[str] = None
    has_time: bool = True
    duration_days: int = 1
    skip_non_working_days: bool = False
    end_as_ddl: bool = False

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        self.event_type = normalize_event_type(self.event_type)
        if not isinstance(self.color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color):
            self.color = COLORS["海盐蓝"]
        self.has_time = bool(self.has_time)
        try:
            self.duration_days = max(1, min(365, int(self.duration_days)))
        except (TypeError, ValueError):
            self.duration_days = 1
        self.skip_non_working_days = self.skip_non_working_days is True
        self.end_as_ddl = self.end_as_ddl is True and self.duration_days > 1 and self.event_type != "ddl"
        if not self.has_time:
            self.reminder = None

    @property
    def due_at(self) -> datetime:
        return datetime.fromisoformat(self.due)

    @property
    def due_date(self) -> date:
        return self.due_at.date()

    @property
    def end_date(self) -> date:
        return self.end_date_for(system_is_workday)

    def occurrence_dates(
        self,
        workday_predicate: Callable[[date], bool] = system_is_workday,
    ) -> tuple[date, ...]:
        if not self.skip_non_working_days:
            return tuple(self.due_date + timedelta(days=offset) for offset in range(self.duration_days))
        result: list[date] = []
        current = self.due_date
        while len(result) < self.duration_days:
            if workday_predicate(current):
                result.append(current)
            current += timedelta(days=1)
        return tuple(result)

    def end_date_for(self, workday_predicate: Callable[[date], bool] = system_is_workday) -> date:
        return self.occurrence_dates(workday_predicate)[-1]

    @property
    def ends_at(self) -> datetime:
        return self.ends_at_for(system_is_workday)

    def ends_at_for(self, workday_predicate: Callable[[date], bool] = system_is_workday) -> datetime:
        return datetime.combine(self.end_date_for(workday_predicate), self.due_at.time())

    def covers(self, day: date, workday_predicate: Callable[[date], bool] = system_is_workday) -> bool:
        return day in self.occurrence_dates(workday_predicate)

    def day_number(self, day: date, workday_predicate: Callable[[date], bool] = system_is_workday) -> int:
        try:
            return self.occurrence_dates(workday_predicate).index(day) + 1
        except ValueError:
            return 0

    @property
    def is_overdue(self) -> bool:
        return not self.done and self.ends_at < datetime.now()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        allowed = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in allowed}
        raw_type = raw.get("event_type")
        if raw_type is None:
            raw_type = raw.get("type")
        if raw_type is None:
            raw_type = raw.get("priority")
        if raw.get("is_ddl") is True:
            raw_type = "ddl"
        data["event_type"] = normalize_event_type(raw_type)
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
        data["has_time"] = bool(data.get("has_time", True))
        data["skip_non_working_days"] = data.get("skip_non_working_days", False) is True
        data["end_as_ddl"] = data.get("end_as_ddl", False) is True
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
    reminder_enabled: bool = False
    reminder_time: Optional[str] = None

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
        self.reminder_enabled = self.reminder_enabled is True
        self.reminder_time = normalize_reminder_time(self.reminder_time)
        if self.reminder_time is None:
            self.reminder_enabled = False

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
    "theme": DEFAULT_THEME_NAME,
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
        self.date_states: dict[str, str] = {}
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
            date_states = raw.get("date_states", {})
            if isinstance(date_states, dict):
                for day_key, status in date_states.items():
                    if not isinstance(day_key, str):
                        continue
                    try:
                        date.fromisoformat(day_key)
                    except ValueError:
                        continue
                    normalized_status = normalize_date_status(status)
                    if normalized_status != "normal":
                        self.date_states[day_key] = normalized_status
            settings = raw.get("settings", {})
            if isinstance(settings, dict):
                self.settings.update(settings)
            self.settings["theme"] = normalize_theme_name(self.settings.get("theme"))
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
            "version": 6,
            "events": [asdict(event) for event in self.events],
            "routines": [asdict(item) for item in self.routines],
            "date_states": self.date_states,
            "settings": self.settings,
            "notified": notification_history,
        }
        temporary = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.data_file)

    def upsert(self, event: Event) -> None:
        previous = next((item for item in self.events if item.id == event.id), None)
        if previous is None:
            self.events.append(event)
        else:
            self.events[self.events.index(previous)] = event
        if (
            previous is None
            or previous.due != event.due
            or previous.reminder != event.reminder
            or previous.has_time != event.has_time
            or previous.duration_days != event.duration_days
            or previous.skip_non_working_days != event.skip_non_working_days
            or previous.end_as_ddl != event.end_as_ddl
        ):
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
        events = [item for item in self.events if self.event_covers(item, day) and (include_done or not item.done)]
        return sorted(events, key=lambda item: (item.done, EVENT_TYPE_RANK[item.event_type]))

    def upcoming(self, days: int = 7, include_overdue: bool = True) -> list[Event]:
        now = datetime.now()
        end = now + timedelta(days=days)
        return sorted(
            (
                item
                for item in self.events
                if not item.done and self.event_starts_at(item) <= end and (include_overdue or self.event_ends_at(item) >= now)
            ),
            key=lambda item: self.event_starts_at(item),
        )

    def create_quick(
        self,
        title: str,
        day: date,
        *,
        color: Optional[str] = None,
        event_type: str = "general",
    ) -> Event:
        due = datetime.combine(day, datetime.min.time()).replace(hour=23, minute=59)
        event = Event(
            id=str(uuid.uuid4()),
            title=title.strip(),
            due=due.isoformat(timespec="minutes"),
            has_time=False,
            color=color or COLORS["海盐蓝"],
            event_type=event_type,
            reminder=None,
        )
        self.upsert(event)
        return event

    def upsert_routine(self, item: RoutineItem) -> None:
        previous = next((existing for existing in self.routines if existing.id == item.id), None)
        if previous is None:
            self.routines.append(item)
        else:
            self.routines[self.routines.index(previous)] = item
        if previous is not None and (
            previous.reminder_enabled != item.reminder_enabled
            or previous.reminder_time != item.reminder_time
            or previous.enabled != item.enabled
            or previous.kind != item.kind
            or previous.created_on != item.created_on
        ):
            self.clear_routine_notifications(item.id)
        self.save()

    def delete_routine(self, item_id: str) -> None:
        self.routines = [item for item in self.routines if item.id != item_id]
        self.clear_routine_notifications(item_id)
        self.save()

    def clear_routine_notifications(self, item_id: str) -> None:
        prefix = f"routine:{item_id}:"
        self.notified = {key for key in self.notified if not key.startswith(prefix)}

    def routines_on(self, day: date) -> list[RoutineItem]:
        visible: list[RoutineItem] = []
        for item in self.routines:
            if not item.enabled or day < item.created_date:
                continue
            if item.kind == "todo" and item.completed_on and day > date.fromisoformat(item.completed_on):
                continue
            visible.append(item)
        return sorted(visible, key=lambda item: (item.is_done_on(day), item.kind == "todo", item.created_on, item.title))

    def agenda_items_on(self, day: date) -> list[Event | RoutineItem]:
        routines = self.routines_on(day) if self.is_workday(day) else []
        items: list[Event | RoutineItem] = [*self.events_on(day), *routines]

        def sort_key(item: Event | RoutineItem) -> tuple[bool, int]:
            if isinstance(item, Event):
                return item.done, EVENT_TYPE_RANK[item.event_type]
            return item.is_done_on(day), EVENT_TYPE_RANK["general"]

        return sorted(items, key=sort_key)

    def date_status(self, day: date) -> str:
        return self.date_states.get(day.isoformat(), "normal")

    def set_date_status(self, day: date, status: object) -> None:
        normalized = normalize_date_status(status)
        key = day.isoformat()
        if normalized == "normal":
            self.date_states.pop(key, None)
        else:
            self.date_states[key] = normalized
        for event in self.events:
            if event.skip_non_working_days:
                self.clear_notifications(event.id)
        self.save()

    def is_workday(self, day: date) -> bool:
        if self.date_status(day) in ("leave", "holiday"):
            return False
        return system_is_workday(day)

    def event_dates(self, event: Event) -> tuple[date, ...]:
        return event.occurrence_dates(self.is_workday)

    def event_covers(self, event: Event, day: date) -> bool:
        return event.covers(day, self.is_workday)

    def event_start_date(self, event: Event) -> date:
        return self.event_dates(event)[0]

    def event_starts_at(self, event: Event) -> datetime:
        return datetime.combine(self.event_start_date(event), event.due_at.time())

    def event_end_date(self, event: Event) -> date:
        return event.end_date_for(self.is_workday)

    def event_ends_at(self, event: Event) -> datetime:
        return event.ends_at_for(self.is_workday)

    def event_day_number(self, event: Event, day: date) -> int:
        return event.day_number(day, self.is_workday)

    def is_event_overdue(self, event: Event, now: Optional[datetime] = None) -> bool:
        return not event.done and self.event_ends_at(event) < (now or datetime.now())

    @staticmethod
    def event_has_deadline(event: Event) -> bool:
        return event.event_type == "ddl" or event.end_as_ddl

    def has_ddl_on(self, day: date) -> bool:
        return any(
            not item.done and self.event_has_deadline(item) and self.event_end_date(item) == day
            for item in self.events
        )

    def ddl_events(self, now: Optional[datetime] = None) -> list[Event]:
        reference = now or datetime.now()
        ddl_items = [item for item in self.events if not item.done and self.event_has_deadline(item)]
        return sorted(
            ddl_items,
            key=lambda item: (
                not self.is_event_overdue(item, reference),
                self.event_ends_at(item),
            ),
        )

    def grouped_ddl_events(
        self,
        now: Optional[datetime] = None,
        pinned_hours: int = 24,
    ) -> tuple[list[Event], list[Event]]:
        reference = now or datetime.now()
        pinned_deadline = reference + timedelta(hours=max(0, pinned_hours))
        pinned: list[Event] = []
        regular: list[Event] = []
        for item in self.ddl_events(reference):
            if self.event_ends_at(item) <= pinned_deadline:
                pinned.append(item)
            else:
                regular.append(item)
        return pinned, regular

    @staticmethod
    def routine_notification_key(item: RoutineItem, day: date) -> Optional[str]:
        if not item.reminder_enabled or item.reminder_time is None:
            return None
        return f"routine:{item.id}:{day.isoformat()}:{item.reminder_time}"

    def due_routine_reminders(self, now: datetime) -> list[RoutineItem]:
        day = now.date()
        if not self.is_workday(day):
            return []
        due: list[RoutineItem] = []
        for item in self.routines_on(day):
            key = self.routine_notification_key(item, day)
            if key is None or key in self.notified or item.is_done_on(day):
                continue
            reminder_time = datetime.strptime(item.reminder_time, "%H:%M").time()
            if now.time() >= reminder_time:
                due.append(item)
        return due

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
