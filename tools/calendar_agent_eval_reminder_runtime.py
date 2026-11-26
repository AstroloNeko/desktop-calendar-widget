"""Live DeepSeek smoke tests for Phase C on synthetic SQLite calendars."""
from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from calendar_core import DATA_DIR
from desktop_calendar.assistant.calendar_chat_protocol import ChatError
from desktop_calendar.reminders.reminder_engine import STATE_KEY
from tools.calendar_agent_eval import EvaluationCalendar, RecordingProvider, ROOT, fixed_time, live_provider, redact, require


MOMENT = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
CONFIG = {"enabled":True, "brief":{"enabled":True,"time":"10:00","days":3}}
FIXTURES = [
    dict(id="meeting",title="组会",date="2026-11-26",time="10:30",reminder=0),
    dict(id="experiment",title="HPLC实验",date="2026-11-27",time="09:00",reminder=0),
    dict(id="report",title="合成周报",date="2026-11-28",event_type="ddl"),
    dict(id="late",title="合成作业",date="2026-11-24",event_type="ddl"),
]


def session_check(calendar, case, turns, *, mutate=False):
    service = calendar.service
    sid = service.new_session()["session_id"]
    before, facts = calendar.read(), calendar.runtime.snapshot()
    evidence = []
    for text, check in turns:
        calls = len(calendar.provider.calls)
        response = service.send_message(sid,text,uuid4().hex,reference_now=MOMENT)
        if text == "确认":
            require(len(calendar.provider.calls) == calls, "confirmation called the model")
        evidence.append(check(response))
    after = calendar.read()
    require(facts == calendar.runtime.snapshot(), f"{case}: calendar facts changed")
    require({key:value for key,value in before["settings"].items() if key != STATE_KEY} ==
            {key:value for key,value in after["settings"].items() if key != STATE_KEY},
            f"{case}: non-runtime preferences changed")
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


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--config", type=Path, default=DATA_DIR/"ai-assistant"/"configuration.dpapi")
    parser.add_argument("--cases", nargs="+")
    args = parser.parse_args()
    provider, secrets, name = live_provider(args.config)
    require(name.startswith("deepseek"), "select the saved DeepSeek model for this live test")
    budget = [24]
    outcomes = []
    with fixed_time(MOMENT):
        for case in cases(None):
            if args.cases and case not in args.cases:
                continue
            recorder = RecordingProvider(provider,budget)
            with TemporaryDirectory(prefix="calendar-agent-reminder-runtime-eval-") as root:
                calendar = EvaluationCalendar(root,recorder,MOMENT,CONFIG)
                try:
                    calendar.start()
                    calendar.seed(FIXTURES)
                    turns, mutate = cases(calendar)[case]
                    evidence = session_check(calendar,case,turns,mutate=mutate)
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
    output = ROOT/"build"/"calendar-agent-eval"/f"reminder-runtime-{uuid4().hex[:8]}.json"
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"model":name,"cases":outcomes},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(output)
    return 0 if all(case["passed"] for case in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
