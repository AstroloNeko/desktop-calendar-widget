"""Bounded Live DeepSeek checks for reminder runtime and configuration."""
from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from desktop_calendar.assistant.calendar_chat_protocol import ChatError
from desktop_calendar.reminders.reminder_config import lookup
from desktop_calendar.reminders.reminder_engine import CONFIG_KEY, STATE_KEY, normalize_config
from tools.calendar_agent_eval import EvaluationCalendar, RecordingProvider, ROOT, fixed_time, live_provider, redact, require


MOMENT = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
CONFIG = {"enabled":True, "brief":{"enabled":True,"time":"10:00","days":3}}
FIXTURES = [
    dict(id="meeting",title="组会",date="2026-11-26",time="10:30",reminder=0),
    dict(id="experiment",title="HPLC实验",date="2026-11-27",time="09:00",reminder=0),
    dict(id="report",title="合成周报",date="2026-11-28",event_type="ddl"),
    dict(id="late",title="合成作业",date="2026-11-24",event_type="ddl"),
]


def session_check(calendar, case, turns, *, mutate=False, configuration=False):
    service = calendar.service
    sid = service.new_session()["session_id"]
    before, facts = calendar.read(), calendar.runtime.snapshot()
    evidence = []
    for text, check in turns:
        calls = len(calendar.provider.calls)
        response = service.send_message(sid,text,uuid4().hex,reference_now=MOMENT)
        if text in ("确认", "授权确认"):
            require(len(calendar.provider.calls) == calls, "confirmation called the model")
        evidence.append(check(response))
    after = calendar.read()
    require(facts == calendar.runtime.snapshot(), f"{case}: calendar facts changed")
    writable = {STATE_KEY,CONFIG_KEY} if configuration else {STATE_KEY}
    require({key:value for key,value in before["settings"].items() if key not in writable} ==
            {key:value for key,value in after["settings"].items() if key not in writable},
            f"{case}: unrelated preferences changed")
    if not mutate:
        require(before == after, f"{case}: a read wrote to SQLite")
    return evidence


def titles(expected):
    def check(response):
        rows = response["reminder_query"]["rows"]
        actual = [row["title"] for row in rows]
        require(actual == expected, f"wrong reminder rows: {actual}")
        return actual
    return check


def brief(day, group, title):
    def check(response):
        actual = response["brief_result"]
        require(actual["date"] == day and any(
            item["group"] == group and item["title"] == title for item in actual["items"]),
            "wrong brief date or group")
        return actual
    return check


def runtime_plan(kind, identifier, **patch):
    def check(response):
        require(response["status"] == "confirmation_required", "did not reach runtime confirmation")
        preview = response["pending_operation"]["preview"]
        require(preview["object_type"] == "reminder" and preview["action_plan"] == dict(action=kind,**patch)
                and [row["event_id"] for row in preview["instances"]] == [identifier], "wrong runtime plan")
        return preview["action_plan"]
    return check


def committed(calendar, until):
    def check(response):
        result = response["operation_result"]
        require(result["action"] == "reminder_action" and result["goal_satisfied"] is True and
                result["until"] == until, "runtime outcome unverified")
        state = calendar.runtime._call(lambda: calendar.runtime._store.settings[STATE_KEY])
        snooze = state["snoozes"].get("meeting")
        if until:
            require(snooze["at"] == until, "wrong persisted snooze")
        else:
            require(snooze is None, "cancel left a snooze")
        return result
    return check


def confirmed_ack(calendar):
    def check(response):
        result = response["operation_result"]
        require(result["kind"] == "acknowledge" and result["count"] == 1
                and result["goal_satisfied"] is True, "not acknowledged")
        state = calendar.runtime._call(lambda: calendar.runtime._store.settings[STATE_KEY])
        require(len(state["handled"]) == 1 and not state["snoozes"], "unexpected runtime state")
        return result
    return check


def cases(calendar):
    return {
        "today": ([("今天有哪些日程提醒？", titles(["组会"]))],False),
        "date": ([("11月27号的提醒计划有哪些？", titles(["HPLC实验","合成周报"]))],False),
        "next": ([("下一次提醒是什么时候？", titles(["组会"]))],False),
        "missed": ([("有哪些已经错过的提醒？", titles(["合成作业"]))],False),
        "brief": ([("给我看看明天的行程简报。", brief("2026-11-27","timed","HPLC实验"))],False),
        "ddl": ([("最近有哪些DDL需要注意？", brief("2026-11-26","upcoming","合成周报"))],False),
        "snooze-cancel": ([
            ("组会这条提醒推迟10分钟。", runtime_plan("snooze","meeting",minutes=10)),
            ("确认", committed(calendar,"2026-11-26T10:40:00")),
            ("我有哪些已经稍后提醒的事项？", titles(["组会"])),
            ("取消组会的稍后提醒。", runtime_plan("cancel_snooze","meeting")),
            ("确认", committed(calendar,None)),
        ],True),
        "absolute": ([
            ("把组会这条提醒先延后，今天十一点再提醒我。", runtime_plan("snooze","meeting",until="2026-11-26T11:00")),
            ("确认", committed(calendar,"2026-11-26T11:00:00")),
        ],True),
        "acknowledge": ([
            ("把组会的提醒标记为已处理。", runtime_plan("acknowledge","meeting")),
            ("确认", confirmed_ack(calendar)),
        ],True),
    }


def config_preview(values, authorized):
    def check(response):
        require(response["status"] == "confirmation_required", "configuration did not require confirmation")
        plan = response["pending_operation"]["preview"]["configuration_plan"]
        actual = {field:edit["after"] for field,edit in plan["fields"].items()}
        require(actual == values and plan["remote_authorization"] is authorized, "wrong configuration plan or authorization")
        return actual
    return check


def config_committed(calendar, values):
    def check(response):
        result = response["operation_result"]
        require(result["action"] == "reminder_config" and result["goal_satisfied"] is True, "no verified settings receipt")
        config = normalize_config(calendar.read()["settings"][CONFIG_KEY])
        require({field:lookup(config,field) for field in values} == values, "wrong persisted setting")
        return result
    return check


def config_query(response):
    result = response["reminder_config"]
    require(result["settings"]["brief"]["time"] == "10:00", "wrong brief configuration result")
    return result


def config_cases(calendar):
    scenarios = {
        "enable-brief": ("以后每天早上八点发日报。", {"brief.enabled":True,"brief.time":"08:00"}),
        "brief-time": ("日报改到早上九点半。", {"brief.time":"09:30"}),
        "brief-content": ("日报不要显示逾期DDL，近期DDL看未来五天。", {"brief.overdue":False,"brief.days":5}),
        "global-ddl": ("DDL统一提前三天提醒。", {"ddl_days":3}),
        "default-time": ("普通事项默认早上十点提醒。", {"time":"10:00"}),
        "event-channels": ("日程提醒只发QQ，不弹Windows。", {"qq":True,"windows":False}),
        "disable-brief": ("关闭每日简报。", {"brief.enabled":False}),
        "aggregate": ("同时发生的提醒合并发送。", {"aggregate":True}),
        "privacy": ("关闭通知隐私保护。", {"private":False}),
    }
    result = {"query-config": ([("查看一下当前日报设置。",config_query)],False)}
    for key, (text, values) in scenarios.items():
        authorized = key in ("event-channels","privacy")
        turns = [(text,config_preview(values,authorized))]
        if authorized:
            turns.append(("确认",config_preview(values,True)))
        turns.append(("授权确认" if authorized else "确认",config_committed(calendar,values)))
        result[key] = (turns,True)
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--config", type=Path, required=True, help="Explicit read-only QA configuration.dpapi")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--suite", choices=("runtime","config"), default="runtime")
    args = parser.parse_args()
    provider, secrets, name = live_provider(args.config)
    require(name.startswith("deepseek"), "select the saved DeepSeek model for this live test")
    budget = [24]
    outcomes = []
    configuration = args.suite == "config"
    suite = config_cases if configuration else cases
    with fixed_time(MOMENT):
        for case in suite(None):
            if args.cases and case not in args.cases:
                continue
            recorder = RecordingProvider(provider,budget)
            with TemporaryDirectory(prefix="calendar-agent-reminder-runtime-eval-") as root:
                config = CONFIG
                if configuration:
                    config = {**CONFIG, "aggregate":False, "private":case == "privacy",
                              "brief":{**CONFIG["brief"], "enabled":case == "disable-brief"}}
                calendar = EvaluationCalendar(root,recorder,MOMENT,config)
                try:
                    calendar.start()
                    calendar.seed(FIXTURES)
                    turns, mutate = suite(calendar)[case]
                    evidence = session_check(calendar,case,turns,mutate=mutate,configuration=configuration)
                    outcome = dict(case=case,passed=True,evidence=evidence)
                except (AssertionError, ChatError, KeyError, RuntimeError, TypeError, ValueError) as error:
                    outcome = dict(case=case,passed=False,error=str(error) if type(error) is ValueError
                                   else getattr(error,"code",type(error).__name__))
                finally:
                    calendar.stop()
                outcome["calls"] = recorder.calls
                outcomes.append(redact(outcome,secrets))
                print(f"{case} {'PASS' if outcome['passed'] else 'FAIL'} · {len(recorder.calls)} calls",flush=True)
    require(outcomes, "no matching cases")
    output = ROOT/"build"/"calendar-agent-eval"/f"reminder-{args.suite}-{uuid4().hex[:8]}.json"
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"model":name,"cases":outcomes},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(output)
    return 0 if all(case["passed"] for case in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
