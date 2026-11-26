"""Shared field-level plans for Reminder Center and Calendar Agent."""
from copy import deepcopy
import re

from calendar_core import normalize_reminder_time
from desktop_calendar.reminders.reminder_rules import normalize_event_rule


class ReminderError(ValueError):
    pass


LEADS = (0, 1, 3)
FIELDS = {
    "enabled": ("默认即时提醒", bool),
    "time": ("默认提醒时间", str),
    "general_days": ("一般及紧急事项提前天数", LEADS),
    "ddl_days": ("DDL及末日DDL提前天数", LEADS),
    "aggregate": ("合并同时触发的提醒", bool),
    "merge_backlog": ("恢复运行时合并积压提醒", bool),
    "windows": ("日程及DDL发 Windows", bool),
    "qq": ("日程及DDL发 QQ", bool),
    "private": ("通知隐私保护", bool),
    "brief.enabled": ("晨间简报开关", bool),
    "brief.time": ("晨间简报发送时间", str),
    "brief.evening_enabled": ("晚间简报开关", bool),
    "brief.evening_time": ("晚间简报发送时间", str),
    "brief.completion": ("晚间显示完成统计", bool),
    "brief.timed": ("简报显示定时事项", bool),
    "brief.untimed": ("简报显示全天及无具体时间事项", bool),
    "brief.unfinished": ("简报显示其他未完成事项", bool),
    "brief.upcoming": ("简报显示近期DDL", bool),
    "brief.overdue": ("简报显示逾期DDL", bool),
    "brief.days": ("简报DDL展望天数", range(1, 31)),
    "brief.send_empty": ("无安排时发送简报", bool),
    "brief.windows": ("每日简报发 Windows", bool),
    "brief.qq": ("每日简报发 QQ", bool),
}


def lookup(config, field):
    group, separator, key = field.partition(".")
    return config[group][key] if separator else config[group]


def put(config, field, value):
    group, separator, key = field.partition(".")
    if separator:
        config.setdefault(group, {})[key] = value
    else:
        config[group] = value


def public_config(config):
    result = {}
    for field in FIELDS:
        put(result, field, lookup(config, field))
    return result


def flatten_patch(patch):
    if not isinstance(patch, dict) or "brief" in patch and not isinstance(patch["brief"], dict):
        raise ReminderError("简报配置应使用独立的 brief 对象。")
    values = {key: value for key, value in patch.items() if key != "brief"}
    if any(not isinstance(key, str) or "." in key for key in values):
        raise ReminderError("请使用嵌套的简报配置字段。")
    values.update((f"brief.{key}", value) for key, value in patch.get("brief", {}).items())
    for field, value in values.items():
        if field not in FIELDS:
            raise ReminderError("该字段不属于可修改的提醒设置。")
        label, kind = FIELDS[field]
        if kind is bool:
            valid = type(value) is bool
        elif kind is str:
            valid = isinstance(value, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) and normalize_reminder_time(value) == value
        else:
            valid = type(value) is int and value in kind
        if not valid:
            raise ReminderError(f"「{label}」的配置值无效。")
    return values


def patched(config, values):
    result = deepcopy(config)
    for field, value in values.items():
        put(result, field, value)
    return result


def active_qq(config):
    brief = config["brief"]
    return (config["qq"], brief["enabled"] and brief["qq"], brief["evening_enabled"] and brief["qq"])


def remote_risk(before, after):
    return (before["private"] and not after["private"]
            or not before["qq"] and after["qq"]
            or not before["brief"]["qq"] and after["brief"]["qq"]
            or not before["enabled"] and after["enabled"] and after["qq"]
            or any(new and not old for old, new in zip(active_qq(before), active_qq(after))))


def config_plan(snapshot, patch, *, rules=None):
    config, versions = snapshot["config"], snapshot["versions"]
    values = {field:value for field,value in flatten_patch(patch).items() if lookup(config, field) != value}
    plan = {field: {"before":lookup(config, field), "after":value, "version":versions.get(field, 0)}
            for field, value in values.items()}
    rule_edits = {}
    if rules is not None:
        prior = config["events"]
        for identifier in prior.keys() | rules.keys():
            old, new = prior.get(identifier), rules.get(identifier)
            if new is not None and normalize_event_rule(new) != new:
                raise ReminderError("单事项提醒规则无效。")
            if old != new:
                rule_edits[identifier] = {"before":old, "after":new}
    return {"fields":plan, "rules":rule_edits, "remote_authorization":remote_risk(config, patched(config, values))}


def verify_plan(plan, config, versions):
    changes = {}
    for field, edit in plan["fields"].items():
        if lookup(config, field) != edit["before"] or versions.get(field, 0) != edit["version"]:
            raise ReminderError("相关提醒设置已变化，请重新查看和确认。")
        changes[field] = edit["after"]
    updated = patched(config, changes)
    for identifier, edit in plan["rules"].items():
        if updated["events"].get(identifier) != edit["before"]:
            raise ReminderError("相关事项的提醒规则已变化，请重新打开设置。")
        if edit["after"] is None:
            updated["events"].pop(identifier, None)
        else:
            updated["events"][identifier] = edit["after"]
    if remote_risk(config, updated) != plan["remote_authorization"]:
        raise ReminderError("通知授权的影响范围已变化，请重新查看和确认。")
    return updated


def field_lines(fields):
    def format_value(value):
        return "开启" if value is True else "关闭" if value is False else str(value)
    return [f"{FIELDS[field][0]} · {format_value(edit['before'])} → {format_value(edit['after'])}"
            for field, edit in fields.items()]


def impacts(fields):
    groups = []
    if fields.keys() & {"time", "general_days", "ddl_days"}:
        groups.append("重新计算依赖默认时间或提前天数的提醒，保留各事项的自定义规则")
    if any(field.startswith("brief.") for field in fields):
        groups.append("晨间或晚间简报及当日发送计划")
    if "enabled" in fields:
        groups.append("使用默认规则的即时提醒")
    if fields.keys() & {"aggregate", "merge_backlog", "windows", "qq"}:
        groups.append("日程和DDL的计划或待发送任务")
    if "private" in fields:
        groups.append("日程和简报的 Windows 与 QQ 通知内容")
    return "影响范围 · " + "。".join(groups)
