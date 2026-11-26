"""Persisted event overrides shared by commands and scheduling."""
from datetime import datetime
import re

from calendar_core import normalize_reminder_time

CONFIG_KEY = "native_reminders"
MAX_DAYS = 365
MAX_MINUTES = 10080


def stored_event_rule(settings, event_id):
    config = settings.get(CONFIG_KEY)
    rules = config.get("events") if isinstance(config, dict) else None
    return rules.get(event_id) if isinstance(rules, dict) else None


def normalize_event_rule(rule):
    if not isinstance(rule, dict):
        return None
    mode = rule.get("mode")
    if mode in ("off", "disabled"):
        return {"mode": "off"}
    if mode == "default":
        return {"mode": "default"}
    if mode == "absolute":
        value = rule.get("at")
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
            try:
                datetime.fromisoformat(value)
            except ValueError:
                return None
            return {"mode": "absolute", "at": value}
        return None
    if mode == "relative" and "minutes" in rule:
        minutes = rule["minutes"]
        if set(rule) == {"mode", "minutes"} and type(minutes) is int and 0 <= minutes <= MAX_MINUTES:
            return {"mode": "relative", "minutes": minutes}
        return None
    if mode in ("relative", "custom"):
        days = rule.get("days")
        clock = normalize_reminder_time(rule.get("time"))
        if type(days) is int and 0 <= days <= MAX_DAYS and (clock or mode == "relative" and "time" not in rule):
            normalized = {"mode": mode, "days": days}
            if clock:
                normalized["time"] = clock
            return normalized
    return None


def effective_mode(event, rule):
    if rule:
        return {"off": "disabled", "custom": "relative"}.get(rule["mode"], rule["mode"])
    if not event.has_time:
        return "default"
    return "relative" if type(event.reminder) is int and 0 <= event.reminder <= MAX_MINUTES else "disabled"
