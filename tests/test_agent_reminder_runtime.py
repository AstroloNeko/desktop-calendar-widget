from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from calendar_core import Event, Store
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_protocol import ChatError
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.reminders.qq_reminders import VerifiedBinding
from desktop_calendar.reminders.reminder_engine import ReminderEngine, STATE_KEY
from desktop_calendar.remote_interaction import execute_interaction
from desktop_calendar.remote_service import make_receipt
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import EventCommandHistory
from test_calendar_chat import ScriptedProvider, TestProtection, tool


class AgentReminderRuntimeTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.now = datetime(2026, 11, 26, 10)
        self.windows, self.qq = Mock(), Mock()
        self.binding = VerifiedBinding(object(), 1, "https://example.test", "test-token", "test-user")
        self.qq.binding.return_value = self.binding
        self.open_runtime()
        self.engine.configure({"enabled": True, "qq": True})
        self.session = self.service.new_session()["session_id"]
        self.turn = 0

    def open_runtime(self):
        self.store = Store(self.root/"calendar.sqlite3", repository_factory=SqliteCalendarRepository)
        self.addCleanup(self.store.repository.close)
        self.history = EventCommandHistory(self.store)
        self.engine = ReminderEngine(self.store, self.windows, self.qq)
        self.gateway = CalendarActionGateway(self.store, self.history, reminders=self.engine,
                                             reminder_now=lambda: self.now)
        self.provider = ScriptedProvider([])
        self.service = CalendarChatService(self.gateway, ChatJournal(self.root/"journal.sqlite3", TestProtection()),
                                           self.provider, clock=lambda: self.now.timestamp())

    def add(self, identifier, offset=0, **values):
        defaults = dict(id=identifier, title=identifier, due=(self.now+timedelta(minutes=offset)).isoformat(),
                        has_time=True, reminder=0)
        event = Event(**{**defaults, **values})
        self.gateway.events.upsert(event)
        self.engine.invalidate()
        return event

    def send(self, message, name=None, args=None, **kwargs):
        self.turn += 1
        self.provider.replies = [tool(name,args)] if name else []
        return self.service.send_message(self.session, message, f"runtime-{self.turn}",
                                         reference_now=self.now.astimezone(), **kwargs)

    def query(self, **filters):
        return self.send("查提醒", "query_reminders", filters)["reminder_query"]

    def propose(self, action, target, text=None, **fields):
        return self.send(text or f"请操作{target.get('title', '')}的提醒", "propose_reminder_action",
                         dict(action=action,target=target,**fields))

    def confirm(self, proposal):
        self.assertEqual(proposal["status"],"confirmation_required",proposal)
        self.provider.replies = []
        op = proposal["pending_operation"]
        return self.service.confirm_operation(self.session,op["operation_id"],revision=op["revision"])

    def test_dates_next_and_exact_item_detail_without_creating_ledger_rows(self):
        self.add("今天", 20)
        self.add("明天", 1440)
        self.add("无提醒", 5, reminder=None)
        before = deepcopy(self.store.settings)
        self.assertEqual([row["title"] for row in self.query()["rows"]],["今天"])
        self.assertEqual([row["title"] for row in self.query(start="2026-11-26",end="2026-11-27")["rows"]],
                         ["今天","明天"])
        self.assertEqual(self.query(filter="next")["rows"][0]["title"],"今天")
        self.assertEqual(self.query(title="无提醒")["item_details"][0]["reminder_plan"]["mode"],"disabled")
        self.assertEqual(self.store.settings,before)
        self.windows.send.assert_not_called()
        self.qq.send.assert_not_called()

    def test_failed_unknown_and_missed_use_independent_channel_ledger(self):
        self.add("本次提醒")
        self.add("错过", -1441)
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key,False,self.now,reason="isolated")
        self.engine.delivery.acknowledge(notice.key,"qq",1,"unknown",self.now)
        before = deepcopy(self.store.settings)
        self.assertEqual(self.query(filter="missed")["rows"][0]["title"],"错过")
        row = self.query(filter="failed",channel="windows")["rows"][0]
        self.assertEqual(row["send_states"],{"windows":"failed","qq":"unknown"})
        self.assertEqual(self.query(filter="failed",channel="qq")["total"],0)
        self.assertEqual(self.query(filter="unknown",channel="qq")["rows"][0]["key"],row["key"])
        self.assertEqual(self.store.settings,before)

    def test_snooze_absolute_cancel_and_restart_preserve_event(self):
        event = self.add("组会", 30)
        initial = self.query(filter="next")["rows"][0]
        pending = self.propose("snooze",{"title":"组会"},minutes=5)
        result = self.confirm(pending)["operation_result"]
        operation = pending["pending_operation"]
        self.assertEqual(result["until"],"2026-11-26T10:35:00")
        self.assertEqual(self.query(filter="snoozed")["total"],1)
        self.open_runtime()
        self.assertEqual(self.engine.runtime_receipt(operation["operation_id"])["until"],result["until"])
        row = self.query(filter="snoozed")["rows"][0]
        self.assertNotEqual(row["key"],initial["key"])
        replacement = self.propose("snooze",{"at":"2026-11-26T10:35"},until="2026-11-26T10:45")
        self.assertEqual(self.confirm(replacement)["operation_result"]["until"],"2026-11-26T10:45:00")
        self.assertEqual(self.confirm(self.propose("cancel_snooze",{"title":"组会"}))["status"],"succeeded")
        self.assertEqual(self.query(filter="snoozed")["total"],0)
        self.assertEqual(self.query(filter="next")["rows"][0]["key"],initial["key"])
        self.assertEqual(self.store.event_by_id(event.id),event)

    def test_pending_selection_keeps_minutes_and_rejects_rule_or_event_change(self):
        self.add("a", 15, title="实验")
        self.add("b", 20, title="实验")
        options = self.propose("snooze",{"title":"实验"},minutes=10)
        self.assertEqual(options["status"],"clarification_required")
        pending = self.send("第二个")
        self.assertEqual(pending["pending_operation"]["preview"]["instances"][0]["event_id"],"b")
        self.assertEqual(pending["pending_operation"]["preview"]["action_plan"]["minutes"],10)
        self.gateway.events.upsert(Event(id="b",title="实验",due="2026-11-26T10:35",has_time=True,reminder=0))
        outcome = self.confirm(pending)
        self.assertEqual(outcome["status"],"failed",outcome)
        self.assertEqual(self.query(filter="snoozed")["total"],0)

    def test_cancel_after_snoozed_notification_does_not_release_original(self):
        self.add("组会",5)
        self.confirm(self.propose("snooze",{"title":"组会"},minutes=5))
        self.now += timedelta(minutes=10)
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key,True,self.now)
        self.engine.delivery.acknowledge(notice.key,"qq",1,"unknown",self.now)
        self.confirm(self.propose("snooze",{"title":"组会"},minutes=5))
        self.assertEqual(self.confirm(self.propose("cancel_snooze",{"title":"组会"}))["status"],"succeeded")
        self.engine.tick(self.now+timedelta(minutes=6))
        self.windows.send.assert_called_once()
        self.qq.send.assert_called_once()

    def test_trusted_notification_members_acknowledge_without_completing(self):
        self.add("甲")
        self.add("乙")
        self.add("不在通知", 15)
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key,True,self.now)
        self.engine.delivery.acknowledge(notice.key,"qq",1,"sent",self.now)
        self.assertEqual(self.query(filter="attention")["total"],2)
        proposal = self.propose("acknowledge",{"context":"last_notification","quantity":"notification_all"},
                                text="最近的通知全标为已处理")
        result = self.confirm(proposal)
        self.assertEqual(result["operation_result"]["count"],2)
        self.assertEqual(self.confirm(proposal)["operation_result"],result["operation_result"])
        rows = self.query(filter="last_notification")["rows"]
        self.assertEqual({row["event_id"] for row in rows},{"甲","乙"})
        for row in rows:
            self.assertEqual(row["send_states"],{"windows":"submitted","qq":"sent"})
            self.assertTrue(row["processed_at"])
            self.assertFalse(row["done"])
        self.assertEqual(self.engine.apply_qq_action(notice.key,"acknowledge",self.binding,self.now),
                         {"acknowledged":True})
        self.assertFalse(any(event.done for event in self.store.events))
        self.assertEqual(self.query(filter="attention")["total"],0)
        self.assertNotIn("business_receipt",json.loads(make_receipt(result,business={"should":"omit"})[0]))

    def test_aggregate_snooze_and_native_replay_only_apply_to_original_members(self):
        self.add("甲")
        self.add("乙")
        self.add("其他",30)
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key,True,self.now)
        self.engine.delivery.acknowledge(notice.key,"qq",1,"sent",self.now)
        proposal = self.propose("snooze",{"context":"last_notification","quantity":"notification_all"},
                                text="最近的通知整条稍后半小时再提醒",minutes=30)
        self.assertEqual(self.confirm(proposal)["operation_result"]["count"],2)
        before = deepcopy(self.store.settings[STATE_KEY]["snoozes"])
        self.assertEqual(set(before),{"甲","乙"})
        self.assertEqual(self.engine.apply_qq_action(notice.key,"snooze_10m",self.binding,self.now),{"snoozed":0})
        self.assertEqual(self.store.settings[STATE_KEY]["snoozes"],before)
        self.assertEqual(self.confirm(proposal)["operation_result"]["until"],"2026-11-26T10:30:00")
        self.assertEqual(self.store.settings[STATE_KEY]["snoozes"],before)

    def test_runtime_confirmation_expiry_source_and_explicit_revision(self):
        self.add("组会",30)
        proposal = self.propose("snooze",{"title":"组会"},minutes=10)
        operation = proposal["pending_operation"]
        identifier = operation["operation_id"]
        with self.assertRaises(ChatError):
            self.service.confirm_operation(self.session,identifier)
        with self.assertRaises(ChatError):
            self.service.confirm_operation(self.session,identifier,source="loopback-http",revision=operation["revision"])
        self.now += timedelta(minutes=16)
        other = self.propose("snooze",{"title":"组会"},minutes=5)
        self.assertEqual(other["status"],"confirmation_required",other)
        self.assertEqual(self.service.get_operation_result(self.session,identifier)["status"],"expired")

    def test_recover_proof_when_journal_write_fails_after_commit(self):
        self.add("组会",20)
        proposal = self.propose("acknowledge",{"title":"组会"})
        operation = proposal["pending_operation"]
        journal = self.service.journal
        write = journal.put

        def fail_after_commit(kind, identifier, owner, value):
            if kind == "operation" and value["status"] == "succeeded":
                raise OSError("isolated journal fault")
            return write(kind,identifier,owner,value)

        with patch.object(journal,"put",side_effect=fail_after_commit):
            self.assertEqual(self.confirm(proposal)["status"],"uncertain")
        self.open_runtime()
        response = self.service.get_operation_result(self.session,operation["operation_id"])
        self.assertEqual(response["status"],"succeeded")
        self.assertEqual(len(self.store.settings[STATE_KEY]["handled"]),1)
        self.assertEqual(len(self.store.settings[STATE_KEY]["runtime_actions"]),1)

    def test_deictic_and_other_source_do_not_resolve_untrusted_targets(self):
        self.add("组会",20)
        bad = self.propose("snooze",{"title":"组会"},text="刚才那个提醒推迟十分钟",minutes=10)
        self.assertEqual(bad["status"],"clarification_required")
        self.query()
        response = self.send("把这条提醒推迟十分钟", "propose_reminder_action",
                             {"action":"snooze","target":{"index":1},"minutes":10},source="wechat")
        self.assertEqual(response["status"],"clarification_required")
        self.assertFalse(self.store.settings.get(STATE_KEY,{}).get("snoozes"))

    def test_brief_and_shortcuts_never_call_model_or_consume_scheduled_send(self):
        self.engine.configure({"enabled":False,"brief":{"enabled":True,"time":"10:00","days":3}})
        self.add("今日会议",30)
        self.add("明日安排",1460,has_time=False)
        self.add("未来DDL",2880,event_type="ddl")
        self.add("过期DDL",-1440,event_type="ddl")
        before = deepcopy(self.store.settings)
        reminder = self.send("/reminders")
        brief = self.send("/brief")
        self.assertIn("reminder_query",reminder)
        self.assertEqual(brief["brief_result"]["agenda_count"],1)
        self.assertEqual(brief["brief_result"]["counts"]["upcoming"],1)
        self.assertEqual(brief["brief_result"]["counts"]["overdue"],1)
        self.assertEqual(self.provider.messages,[])
        tomorrow = self.send("明天的行程简报","query_brief",{"date":"2026-11-27"})
        self.assertIn("明日安排",tomorrow["assistant_message"])
        self.assertIn("未来DDL",tomorrow["assistant_message"])
        self.assertEqual(self.store.settings,before)
        receipt = json.loads(make_receipt(tomorrow,business={"should":"omit"})[0])
        self.assertNotIn("business_receipt",receipt)
        self.assertIn("明日安排",receipt["reply_text"])
        self.engine.tick(self.now)
        self.windows.send.assert_called_once()
        self.assertEqual(self.windows.send.call_args.args[0].kind,"brief")

    def test_brief_shortcut_uses_full_counts_and_keeps_ddl_in_remote_plain_text(self):
        sections = (("timed", 19, 30, True, "general"), ("untimed", 5, 0, False, "general"),
                    ("upcoming", 6, 1440, True, "ddl"), ("overdue", 4, -1440, True, "ddl"))
        for group, total, offset, has_time, event_type in sections:
            for index in range(total):
                label = f"{group}-{index:02d}"
                self.add(label, offset, title=f"{label} " + "长标题" * 45, has_time=has_time, event_type=event_type)
        before = deepcopy(self.store.settings)

        response = self.send("/brief")
        brief = response["brief_result"]
        self.assertEqual(brief["counts"], {"timed":19, "untimed":5, "unfinished":0, "upcoming":6, "overdue":4})
        self.assertEqual(brief["agenda_count"], 24)
        selected = [item["event_id"] for item in brief["items"]]
        self.assertEqual(selected, [f"{group}-{index:02d}" for group, limit in (
            ("timed", 5), ("untimed", 3), ("upcoming", 4), ("overdue", 3)) for index in range(limit)])
        remote = execute_interaction(self.service, self.service.journal, None,
            {"session_id": self.session, "request_id": "remote-brief", "interaction": {
                "kind": "quick_query", "command": "brief", "date": self.now.date().isoformat(), "scope": "agenda"}},
            self.now.astimezone())
        self.assertEqual(remote["brief_result"]["items"], brief["items"])
        wire = json.loads(make_receipt(remote)[0])["reply_text"]
        self.assertTrue(all(event_id in wire for event_id in selected))
        self.assertIn("已按分类展示 15 / 34 项。", wire)
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.provider.messages, [])
