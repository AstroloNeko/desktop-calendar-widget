"""Agent adapters for the existing owner-thread reminder service."""
from datetime import datetime, time, timedelta
from itertools import islice
import re
import secrets

from desktop_calendar.assistant.calendar_chat_protocol import envelope, fail
from desktop_calendar.assistant.calendar_chat_tools import iso_day, reference_today
from desktop_calendar.reminders.notification_dispatch import read_time
from desktop_calendar.reminders.notification_model import (
    BRIEF_GROUP_LABELS, BRIEF_TITLES, brief_content, brief_item_context,
    brief_today_count, compact_brief_line, select_brief_items,
)
from desktop_calendar.reminders.reminder_config import FIELDS, config_plan, field_lines, impacts, lookup, public_config, put
from desktop_calendar.reminders.reminder_engine import CONFIG_KEY, CATCHUP, ReminderError, normalize_config
from desktop_calendar.reminders.reminder_runtime import channel_members, snooze_time

LIMIT = 10
TTL = 900
LABELS = {"snooze":"稍后提醒", "cancel_snooze":"取消稍后提醒", "acknowledge":"标记提醒已处理"}
QQ_STATE = {"available":"已核验主动推送能力", "unsupported":"当前云端没有主动推送接口",
            "unbound":"当前没有已核验的绑定", "unverified":"当前绑定的主动推送能力尚未核验"}


def is_config_lookup(message):
    return bool(re.search(r"设置|配置|开关|默认几点|默认时间|提前几天|发到哪|在哪.*发送", message)
                and re.search(r"提醒|简报|日报|通知", message)
                and re.search(r"查|看|什么|多少|几点|几天|开了吗|开启了吗|发到哪|在哪", message)
                and not re.search(r"改|设为|帮我|打开|关闭|启用|停用|以后|只发|不要", message))


def configuration_query(gateway, scope):
    engine = reminder_engine(gateway)
    config = public_config(engine.config())
    fields = tuple(FIELDS)
    if scope in ("event", "brief"):
        fields = tuple(field for field in fields if field.startswith("brief.") == (scope == "brief"))
    elif scope == "channels":
        fields = ("windows", "qq", "brief.windows", "brief.qq", "private")
    shown = {}
    lines = ["当前提醒设置"]
    for field in fields:
        value = lookup(config, field)
        put(shown, field, value)
        text = "开启" if value is True else "关闭" if value is False else str(value)
        lines.append(f"{FIELDS[field][0]} · {text}")
    qq_status, _ = engine.delivery.qq_status()
    if scope in ("all", "channels"):
        lines.append("QQ · "+QQ_STATE[qq_status])
    return {"settings":shown, "qq_status":qq_status, "text":"\n".join(lines)}


def configuration_proposal(service, session, owner, request_id, source, changes):
    gateway = service.gateway
    sid = session["session_id"]

    def prepare():
        engine = reminder_engine(gateway)
        try:
            plan = config_plan(engine.configuration_snapshot(), changes)
        except ReminderError as error:
            fail(str(error))
        if plan["remote_authorization"]:
            plan["qq_status"], plan["binding_scope"] = engine.delivery.qq_status()
        return plan

    plan = gateway.dispatch(prepare)
    if not plan["fields"]:
        return envelope(sid, request_id, assistant_message="当前提醒设置已符合要求。", plain_reminder=True)
    previous = service._operation(sid, session["pending"], owner) if session.get("pending") else None
    if previous and previous["status"] in ("pending", "ready", "executing", "uncertain"):
        fail("请先处理当前的待确认操作。", "confirmation_required")
    preview = {"object_type":"reminder_config", "action":"reminder_config", "description":"修改全局提醒设置",
               "event_id":"reminder_config", "after":{}, "configuration_plan":plan}
    op = dict(operation_id=secrets.token_urlsafe(24), session_id=sid, request_id=request_id, source=source,
              status="pending", preview=preview, expires=service.clock()+TTL, revision=secrets.token_urlsafe(24),
              requires_confirmation=True)
    session.pop("event_input", None)
    service.journal.put("operation", op["operation_id"], owner, op)
    session["pending"] = op["operation_id"]
    service._audit(op, owner, source)
    lines = ["待确认的提醒设置", *field_lines(plan["fields"]), impacts(plan["fields"])]
    if plan["remote_authorization"]:
        lines += ["隐私提示 · 这些设置可能向已核验的远程服务发送私人日历内容。",
                  "QQ · "+QQ_STATE[plan["qq_status"]], "同意请回复「授权确认」，或回复「取消」。"]
    else:
        lines.append("不会新增远程数据发送授权。回复「确认」或「取消」。")
    return envelope(sid, request_id, status="confirmation_required", pending_operation=op,
                    plain_reminder=True, assistant_message="\n".join(lines))


def is_reminder_lookup(message):
    """Offer only reminder read tools when the request explicitly asks for reminder facts."""
    return bool(re.search(r"提醒|通知", message) and
                re.search(r"哪[些个项条]|有没有|有.{0,4}什么|查[看询]|下一次|什么时候", message) and
                not re.search(r"简报|添加|新建|创建|修改|删除|取消|推迟|延后|延迟|往后|改[到成]|标记|提醒我|再提醒", message))


def reminder_engine(gateway):
    if gateway.reminders is None:
        fail("当前运行环境没有本地提醒引擎。", "reminder_unavailable")
    return gateway.reminders


def clock_value(value):
    try:
        result = datetime.fromisoformat(value)
        if result.isoformat(timespec="minutes") != value or result.tzinfo:
            raise ValueError
        return result
    except (TypeError, ValueError):
        fail("请提供明确的本地日期和钟点。", "clarification_required")


def recent_notification(state, now, source):
    if source not in ("desktop", "loopback-http"):
        fail("当前远程会话没有可核验的通知关联。请按事项或提醒时刻定位。", "clarification_required")
    batches = []
    for identifier, batch in state["batches"].items():
        windows = batch.get("channels", {}).get("windows", {})
        posted = read_time(windows.get("at"), None)
        if (batch.get("kind") in ("event", "ddl") and windows.get("status") == "submitted" and
                posted is not None and now - timedelta(minutes=30) <= posted <= now and
                read_time(batch.get("expires"), now) > now and channel_members(batch,"windows")):
            batches.append((posted, identifier))
    batches.sort(reverse=True)
    if not batches or len(batches) > 1 and batches[0][0] == batches[1][0]:
        fail("没有唯一且可核验的最近 Windows 提交记录。请按事项或提醒时刻定位。", "clarification_required")
    return batches[0][1]


def serialize(row):
    return {**row, "when":row["when"].isoformat(timespec="seconds"),
            "next_attempt":row["next_attempt"].isoformat(timespec="seconds") if row["next_attempt"] else None}


def row_text(row):
    when = row["when"].replace("T", " ")[:16]
    title = " ".join(row["title"].split())[:55]
    handled = "已处理" if row["processed_at"] else "未标记已处理"
    calendar = "事项已完成" if row["done"] else "事项未完成"
    snoozed = " · 已稍后" if row["snoozed"] else ""
    retry = row["next_attempt"]
    next_attempt = " · 预计重试 "+retry.replace("T"," ")[:16] if retry and retry != row["when"] else ""
    return (f"{when} · {title} · {row['status']}{snoozed}{next_attempt} · {handled} · {calendar}"
            f" · Windows {row['windows']} / QQ {row['qq']}")


def query_reminders(gateway, args, source):
    engine = reminder_engine(gateway)
    now = gateway.reminder_now()
    config, rows, state = engine.runtime_snapshot(now)
    kind = args.get("filter","all")
    batch = recent_notification(state, now, source) if kind == "last_notification" else None
    batch_keys = {key for _,key in channel_members(state["batches"][batch],"windows")} if batch else ()
    scope = bool(args.get("start") or args.get("end"))
    if scope or kind == "all" and not (args.get("title") or args.get("at")):
        start = iso_day(args["start"]) if "start" in args else (
            iso_day(args["end"]) if "end" in args else reference_today(now.date))
        end = iso_day(args["end"]) if "end" in args else start
        if not 0 <= (end-start).days <= 366:
            fail("提醒查询范围应为 0 至 366 天。", "clarification_required")
        rows = {key:row for key,row in rows.items() if start <= row["when"].date() <= end}
    at = clock_value(args["at"]) if "at" in args else None
    title = args.get("title","").strip()
    data = []
    qq_status, _ = engine.delivery.qq_status(state) if config["qq"] and kind == "next" else ("unbound", None)
    has_channel = config["windows"] or config["qq"] and qq_status not in ("unbound","unsupported") and (
        not state["qq_capability_required"].get("event") or qq_status == "available")
    for row in rows.values():
        if title and title.casefold() not in row["title"].casefold() or at and row["when"].replace(second=0, microsecond=0) != at:
            continue
        if args.get("channel"):
            states = [row["send_states"].get(args["channel"])]
        else:
            states = row["send_states"].values()
        if kind == "next" and (not config["enabled"] or not has_channel
                or not row["current"] or not row["next_attempt"] or row["next_attempt"] <= now):
            continue
        if kind == "attention":
            due = row["current"] and timedelta(0) <= now - row["when"] < CATCHUP
            needs_attention = row["group"] == "attention" or any(
                status in ("failed","unknown","expired","rate_limited","user_opt_out") for status in states)
            if row["processed_at"] or not (due or needs_attention):
                continue
        if kind == "missed" and not row["missed"] or kind == "snoozed" and not row["snoozed"]:
            continue
        if (kind == "failed" and not any(status in ("failed","rate_limited","user_opt_out") for status in states)
                or kind == "unknown" and "unknown" not in states or batch and row["key"] not in batch_keys):
            continue
        data.append(row)
    if kind == "next":
        data.sort(key=lambda row:(row["next_attempt"],row["key"]))
        data = data[:1]
    else:
        data.sort(key=lambda row:(row["when"],row["key"]))
    details = []
    if title and kind == "all":
        events = (event for event in engine.store.events if event.title.strip().casefold() == title.casefold())
        for event in islice(events, LIMIT):
            detail = gateway.reminder_detail(event)
            details.append({key:detail[key] for key in ("title","date","time","deadline","reminder","reminder_plan")})
    return {"rows":[serialize(row) for row in data[:LIMIT]],"total":len(data),"enabled":config["enabled"],
            "batch":batch,"item_details":details}


def query_brief(gateway, args, source):
    now = gateway.reminder_now()
    day = iso_day(args["date"]) if "date" in args else reference_today(now.date)
    config = normalize_config(gateway.store.settings.get(CONFIG_KEY))
    settings = config["brief"]
    slot = args.get("slot", "morning")
    scope = args.get("scope", "agenda")
    if scope == "remaining":
        settings = {**settings, "timed": True, "untimed": True, "unfinished": True,
                    "upcoming": False, "overdue": False}
    items, progress = brief_content(gateway.store, datetime.combine(day, time()), settings,
                                    slot=slot, overdue_at=now, ddl_only=scope == "ddl")
    shown, counts = select_brief_items(items)
    agenda = brief_today_count(items)
    anchor = day.isoformat()
    title = "今日未完成事项" if scope == "remaining" else BRIEF_TITLES[slot]
    lines = [f"{anchor} {title} · 当日待办 {agenda} 项 · 近期 DDL {counts['upcoming']} 项 · 逾期 DDL {counts['overdue']} 项"]
    private = config["private"] and source == "wechat"
    if private:
        progress = None
    else:
        if progress is not None:
            lines.append(progress.summary())
        for group, label in BRIEF_GROUP_LABELS.items():
            matches = [item for item in shown if item["group"] == group]
            if matches:
                lines.append(label)
                for item in matches:
                    line = " · ".join(filter(None, (brief_item_context(item, day), " ".join(item["title"].split())[:55])))
                    lines.append(compact_brief_line(line) if source == "wechat" else line)
        if len(items) > len(shown):
            lines.append(f"已按分类展示 {len(shown)} / {len(items)} 项。")
    return {"date":anchor,"scope":scope,"slot":slot,"private":private,"counts":counts,"agenda_count":agenda,
            "items":[] if private else [dict(item) for item in shown],"text":"\n".join(lines),
            **({"completion": {"total":progress.total,"completed":progress.completed,
                               "remaining":progress.remaining,"percent":progress.percent}} if progress is not None else {})}


def action_targets(gateway, args, context, source, message=None):
    engine = reminder_engine(gateway)
    now = gateway.reminder_now()
    _, rows, state = engine.runtime_snapshot(now)
    target = args["target"]
    kind = args["action"]
    if kind == "snooze":
        if ("minutes" in args) == ("until" in args):
            fail("请给出延后分钟数或下一次提醒的明确日期和钟点。", "clarification_required")
        try:
            snooze_time(args, now)
        except ReminderError as error:
            fail(str(error), "clarification_required")
    elif "minutes" in args or "until" in args:
        fail("当前操作不需要新提醒时间。", "clarification_required")
    action = {key:args[key] for key in ("action","minutes","until") if key in args}
    contextual = any(key in target for key in ("index","key")) or target.get("context") == "last_query"
    latest = target.get("context") == "last_notification"
    if message and not contextual:
        if latest and "通知" not in message:
            fail("请先查询当前提醒，或明确指明最近一条通知。", "clarification_required")
        if not latest:
            title_text = "".join(target.get("title", "").casefold().split())
            body = "".join(message.casefold().split())
            if title_text and title_text not in body or not title_text and re.search(r"[这那]个|[这那]条|刚才", body):
                fail("请先查询当前提醒，或明确给出事项名称和原提醒钟点。", "clarification_required")
    whole = target.get("quantity") == "notification_all"
    if whole and (not latest or set(target) != {"context","quantity"}):
        fail("整条通知操作需要明确的最近通知关联。", "clarification_required")
    if whole and message and not any(term in message for term in ("全部","所有","都","整","全")):
        fail("请明确操作整条聚合通知，或选择其中一项。", "clarification_required")
    if latest and contextual:
        fail("请选择最近通知或当前查询中的实例。", "clarification_required")
    if contextual:
        if not context or context["source"] != source or context["expires"] < now.timestamp():
            fail("当前会话没有可核验的提醒目标，请先查询。", "clarification_required")
        shown = context["rows"]
        if "index" in target:
            index = target["index"]
            shown = shown[index-1:index]
        elif "key" not in target and context["total"] != 1:
            fail("当前查询不只一项，请给出显示的编号。", "clarification_required")
        if "key" in target:
            shown = [item for item in shown if item["key"] == target["key"]]
        if not shown or any(item["key"] not in rows or rows[item["key"]]["revision"] != item["revision"] for item in shown):
            fail("你选的提醒已变化，请重新查询。", "reminder_conflict")
        matches = [rows[item["key"]] for item in shown]
    elif latest:
        batch = recent_notification(state, now, source)
        keys = {key for _, key in channel_members(state["batches"][batch],"windows")}
        matches = [row for row in rows.values() if row["key"] in keys]
    else:
        if not target.get("title","").strip() and "at" not in target:
            fail("请明确事项、提醒钟点或当前查询中的编号。", "clarification_required")
        matches = list(rows.values())
    at = clock_value(target["at"]) if "at" in target else None
    title = target.get("title","").strip().casefold()
    matches = [row for row in matches if row["current"] and (not title or row["title"].strip().casefold() == title)
               and (at is None or row["when"].replace(second=0,microsecond=0) == at)
               and (row["snoozed"] if kind == "cancel_snooze" else now - row["when"] < CATCHUP)]
    if not matches:
        fail("没有符合条件的有效提醒实例。", "reminder_conflict")
    if len(matches) > (30 if whole else LIMIT):
        fail("匹配的提醒过多，请指定更明确的范围。", "clarification_required")
    if kind == "snooze":
        try:
            for row in matches:
                snooze_time(action,now,row["when"])
        except ReminderError as error:
            fail(str(error), "clarification_required")
    return action, [serialize(row) for row in sorted(matches,key=lambda row:(row["when"],row["key"]))], whole


def preview_or_select(service, session, owner, request_id, source, action, rows, whole=False):
    previous = service._operation(session["session_id"],session["pending"],owner) if session.get("pending") else None
    if previous and previous["status"] in ("pending","ready","executing","uncertain"):
        fail("请先处理当前的待确认操作。", "confirmation_required")
    sid = session["session_id"]
    revision = secrets.token_urlsafe(24)
    expires = service.clock()+TTL
    if len(rows) > 1 and not whole:
        candidates = [{"id":row["key"],"title":row["title"],"date":row["when"],"object_type":"reminder"} for row in rows]
        session.update(candidates=candidates, habit_candidates=[], selected=None, selected_kind="reminder")
        session["event_input"] = dict(plan=action, kind="reminder", source=source, rows=rows,
                                      ids=[row["key"] for row in rows], revision=revision, expires=expires)
        return envelope(sid,request_id,status="clarification_required",candidates=candidates,
                        selection_revision=revision,plain_reminder=True,
                        assistant_message="请选择提醒实例。\n"+"\n".join(
                            f"{index}. {row_text(row)}" for index,row in enumerate(rows,1)))
    kind = action["action"]
    label = LABELS[kind]
    that = "（延后 {} 分钟，从原定时刻与确认时刻中较晚者起算）".format(action["minutes"]) if "minutes" in action else (
        "（至 "+action["until"].replace("T"," ")+"）" if "until" in action else "")
    preview = dict(object_type="reminder", action="reminder_action", description=label,
                   event_id=rows[0]["event_id"], after={}, action_plan=action, instances=rows)
    op = dict(operation_id=secrets.token_urlsafe(24), session_id=sid, request_id=request_id,
              status="pending", preview=preview, expires=expires, revision=revision, source=source,
              requires_confirmation=True)
    if session.get("selected_kind") == "reminder":
        session.update(candidates=[], selected=None, selected_kind="event")
    session.pop("event_input", None)
    service.journal.put("operation",op["operation_id"],owner,op)
    session["pending"] = op["operation_id"]
    service._audit(op,owner,source)
    return envelope(sid,request_id,status="confirmation_required",pending_operation=op,plain_reminder=True,
                    assistant_message=f"待确认：{label} {len(rows)} 项{that}。\n"+
                    "\n".join(row_text(row) for row in rows)+
                    "\n回复「确认」或「取消」。")
