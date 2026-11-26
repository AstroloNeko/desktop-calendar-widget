"""Reminder settings through the real Agent, ReminderEngine and temporary SQLite."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from calendar_core import Event, Store
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.reminders.qq_reminders import VerifiedBinding
from desktop_calendar.reminders.reminder_config import ReminderError, config_plan
from desktop_calendar.reminders.reminder_engine import ReminderEngine, STATE_KEY
from desktop_calendar.reminders.reminder_ui import ReminderSettingsDialog
from desktop_calendar.remote_service import make_receipt
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import EventCommandHistory
from test_calendar_chat import ScriptedProvider, TestProtection, tool


class AgentReminderConfigurationTests(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = Store(root/"calendar.sqlite3", repository_factory=SqliteCalendarRepository)
        self.addCleanup(self.store.repository.close)
        self.now = datetime(2026,11,26,10)
        self.windows, self.qq = Mock(), Mock()
        self.binding = VerifiedBinding(object(),1,"https://example.test","synthetic-token","synthetic-user")
        self.qq.binding.return_value = self.binding
        self.qq.proactive_capability.return_value = True
        self.engine = ReminderEngine(self.store,self.windows,self.qq)
        self.engine.configure({"enabled":True,"time":"10:00"})
        self.history = EventCommandHistory(self.store)
        self.gateway = CalendarActionGateway(self.store,self.history,reminders=self.engine,reminder_now=lambda:self.now)
        self.provider = ScriptedProvider([])
        self.service = CalendarChatService(self.gateway,ChatJournal(root/"journal.sqlite3",TestProtection()),
                                           self.provider,clock=lambda:self.now.timestamp())
        self.session = self.service.new_session()["session_id"]
        self.turn = 0

    def send(self, message, name=None, args=None):
        self.turn += 1
        self.provider.replies = [tool(name,args)] if name else []
        return self.service.send_message(self.session,message,f"config-turn-{self.turn}",
                                         reference_now=self.now.astimezone())

    def proposal(self, changes):
        result = self.send("请修改全局提醒", "propose_reminder_config", {"changes":changes})
        self.assertEqual(result["status"],"confirmation_required",result)
        return result

    def save(self, changes, *, authorized=False):
        self.proposal(changes)
        response = self.send("授权确认" if authorized else "确认")
        self.assertEqual(response["status"],"succeeded",response)
        return response

    def form(self, changes, snapshot=None, *, rules=None):
        return config_plan(snapshot or self.engine.configuration_snapshot(),changes,rules=rules)

    def add(self, identifier, *, day="2026-11-26"):
        self.history.upsert(Event(id=identifier,title=identifier,due=day+"T23:59",
                                  has_time=False,reminder=None))
        self.engine.invalidate()

    def test_read_only_projection_and_direct_query_exclude_credentials_and_rules(self):
        before = deepcopy(self.store.settings)
        reply = self.send("/reminder-settings")
        self.assertEqual(len(self.provider.messages),0)
        config = reply["reminder_config"]["settings"]
        self.assertEqual(config["time"],"10:00")
        self.assertTrue(config["enabled"])
        self.assertEqual(config["brief"]["days"],3)
        self.assertNotIn("events",config)
        encoded, _ = make_receipt(reply,business={"unused":True})
        result = json.loads(encoded)
        self.assertIn("每日简报发 QQ",result["reply_text"])
        self.assertNotIn("business_receipt",result)
        self.assertNotIn("synthetic-token",encoded)
        self.assertEqual(self.store.settings,before)
        self.windows.send.assert_not_called()
        self.qq.send.assert_not_called()

    def test_multi_field_patch_replans_default_and_preserves_override(self):
        self.add("default",day="2026-11-30")
        self.add("override",day="2026-11-30")
        override = {"mode":"absolute","at":"2026-11-27T18:00"}
        self.engine.configure({"enabled":True,"events":{"override":override}})
        self.engine._rebuild()
        old = self.engine.plans["override"]
        request = self.proposal({"time":"08:00","general_days":3,"aggregate":True})
        fields = request["pending_operation"]["preview"]["configuration_plan"]["fields"]
        self.assertEqual(set(fields),{"time","general_days"})
        self.assertNotIn("合并同时触发",request["assistant_message"])
        result = self.send("确认")
        self.assertTrue(result["operation_result"]["goal_satisfied"])
        self.assertEqual(self.engine.plans["default"].when,datetime(2026,11,27,8))
        self.assertEqual(self.engine.plans["override"],old)
        self.assertEqual(self.engine.config()["events"]["override"],override)
        self.assertFalse(self.history.can_undo_operation(result["operation_result"]["operation_id"]))
        self.assertIn("不进入日历撤销",result["assistant_message"])

    def test_brief_switch_and_channels_are_independent(self):
        self.save({"brief":{"enabled":True,"time":"11:00","windows":False,"overdue":False}})
        config = self.engine.config()
        self.assertTrue(config["windows"])
        self.assertFalse(config["qq"])
        self.assertFalse(config["brief"]["windows"])
        self.assertTrue(config["brief"]["enabled"])
        self.save({"brief":{"enabled":False}})
        self.assertTrue(self.engine.config()["enabled"])
        self.assertFalse(self.engine.config()["brief"]["enabled"])
        self.windows.send.assert_not_called()
        self.qq.send.assert_not_called()

    def test_config_requires_explicit_remote_consent_and_reports_capability(self):
        self.qq.proactive_capability.return_value = False
        request = self.proposal({"windows":False,"qq":True})
        preview = json.loads(make_receipt(request,business={"unused":True})[0])
        self.assertNotIn("business_receipt",preview)
        self.assertIn("私人日历内容",preview["reply_text"])
        self.assertIn("授权确认",preview["reply_text"])
        before = deepcopy(self.store.settings)
        self.assertEqual(self.send("确认")["status"],"confirmation_required")
        self.assertEqual(self.store.settings,before)
        result = self.send("授权确认")
        self.assertEqual(result["status"],"succeeded")
        self.assertIn("当前云端没有主动推送接口",result["assistant_message"])
        self.assertTrue(self.engine.config()["qq"])
        self.assertTrue(self.engine.config()["brief"]["windows"])
        self.add("future",day="2026-11-27")
        self.engine.tick(self.now+timedelta(days=1))
        self.qq.send.assert_not_called()

    def test_turning_off_privacy_rechecks_verified_binding(self):
        self.engine.configure({"enabled":True,"qq":True,"private":True})
        request = self.proposal({"private":False})
        self.assertIn("授权确认",request["assistant_message"])
        self.qq.binding.return_value = VerifiedBinding(object(),2,"https://example.test","other-token","other-user")
        result = self.send("授权确认")
        self.assertEqual(result["status"],"failed")
        self.assertTrue(self.engine.config()["private"])

    def test_field_version_conflict_preserves_unrelated_gui_changes_and_blocks_aba(self):
        form = self.engine.configuration_snapshot()
        self.save({"time":"08:00"})
        with self.assertRaisesRegex(ReminderError,"已变化"):
            self.engine.apply_configuration(self.form({"time":"11:00"},form),self.now)
        self.engine.apply_configuration(self.form({"aggregate":False},form),self.now)
        self.assertFalse(self.engine.config()["aggregate"])
        self.assertEqual(self.engine.config()["time"],"08:00")
        self.proposal({"time":"12:00"})
        self.engine.apply_configuration(self.form({"time":"11:00"}),self.now)
        self.engine.apply_configuration(self.form({"time":"08:00"}),self.now)
        self.assertEqual(self.send("确认")["status"],"failed")
        self.assertEqual(self.engine.config()["time"],"08:00")

    def test_open_reminder_center_form_saves_local_difference_only(self):
        snapshot = self.engine.configuration_snapshot()
        config = snapshot["config"]
        form = Mock(config_snapshot=snapshot, rules=config["events"])
        form.app = SimpleNamespace(reminder_engine=self.engine,invalidate_reminders=Mock())
        for name in ("enabled","private","aggregate","merge_backlog","windows","qq"):
            getattr(form,name).get.return_value = config[name]
        form.clock.get.return_value = config["time"]
        form.general.get.return_value = "0 天"
        form.ddl.get.return_value = "1 天"
        form._days.side_effect = ReminderSettingsDialog._days
        form._brief_values.side_effect = lambda: dict(config["brief"])
        self.save({"time":"08:00"})
        form.clock.get.return_value = "09:30"
        self.assertFalse(ReminderSettingsDialog.save(form))
        form.close.assert_not_called()
        form.clock.get.return_value = config["time"]
        form.aggregate.get.return_value = False
        self.assertTrue(ReminderSettingsDialog.save(form))
        self.assertFalse(self.engine.config()["aggregate"])
        self.assertEqual(self.engine.config()["time"],"08:00")

    def test_invalid_fields_and_failed_transaction_leave_snapshot_unchanged(self):
        before = deepcopy(self.store.settings)
        for changes in ({"worker_token":"bad"},{"brief":{"days":0}},{"time":"25:00"},{"windows":1}):
            with self.subTest(changes=changes), self.assertRaises(ReminderError):
                self.form(changes)
        plan = self.form({"time":"08:00","brief":{"time":"09:30"}})
        with patch.object(self.store.repository,"_commit",side_effect=sqlite3.OperationalError("isolated commit failure")):
            with self.assertRaises(OSError):
                self.engine.apply_configuration(plan,self.now,operation_id="isolated-fault")
        self.assertIsNone(self.engine.configuration_receipt("isolated-fault"))
        self.assertEqual(self.store.settings,before)
        reopened = Store(self.store.data_file,repository_factory=SqliteCalendarRepository)
        self.addCleanup(reopened.repository.close)
        self.assertEqual(reopened.settings,before)

    def test_full_remote_preview_keeps_all_fields_and_explicit_authorization(self):
        config = self.engine.config()
        changes = {key:not config[key] for key in ("enabled","windows","qq","aggregate","merge_backlog","private")}
        changes.update(time="07:45",general_days=1,ddl_days=3,
                       brief={key:not val for key,val in config["brief"].items() if type(val) is bool})
        changes["brief"].update(time="07:00",days=30)
        proposal = self.proposal(changes)
        text = json.loads(make_receipt(proposal)[0])["reply_text"]
        self.assertEqual(text,proposal["assistant_message"])
        self.assertIn("授权确认",text)

    def test_expired_pending_and_repeated_confirmation(self):
        old = self.proposal({"ddl_days":3})["pending_operation"]
        self.now += timedelta(minutes=16)
        self.assertEqual(self.send("确认")["status"],"expired")
        result = self.save({"ddl_days":3})
        op = result["pending_operation"]
        self.assertNotEqual(op["operation_id"],old["operation_id"])
        before = deepcopy(self.store.settings)
        again = self.service.confirm_operation(self.session,op["operation_id"],revision=op["revision"])
        self.assertEqual(again["operation_result"],result["operation_result"])
        self.assertEqual(self.store.settings,before)

    def test_default_time_change_never_replays_an_already_submitted_reminder(self):
        self.add("default")
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key,True,self.now)
        self.save({"time":"08:00"})
        self.engine.tick(self.now)
        self.windows.send.assert_called_once()
        self.assertEqual(self.engine.batch_record(notice.key)["channels"]["windows"]["status"],"submitted")

    def test_brief_time_change_preserves_same_day_dedupe_and_original_delivery(self):
        self.add("meeting")
        self.engine.configure({"brief":{"enabled":True,"time":"10:00"}})
        self.engine.tick(self.now)
        original = self.windows.send.call_args.args[0]
        self.engine.acknowledge(original.key,True,self.now)
        self.save({"brief":{"time":"11:00"}})
        self.engine.tick(self.now+timedelta(hours=1))
        self.windows.send.assert_called_once()
        self.assertEqual(self.engine.batch_record(original.key)["when"],self.now.isoformat())

    def test_disable_and_reenable_blocks_stale_queue_and_historical_events(self):
        self.add("meeting")
        self.engine.tick(self.now)
        original = self.windows.send.call_args.args[0]
        self.save({"enabled":False})
        self.windows.remove.assert_any_call(original.tag)
        self.engine.delivery.acknowledge(original.key,"windows",1,"cancelled",self.now)
        self.add("backlog",day="2026-11-25")
        self.now += timedelta(minutes=5)
        self.save({"enabled":True})
        self.engine.tick(self.now)
        self.windows.send.assert_called_once()

    def test_privacy_cancels_old_payload_and_retries_with_redaction(self):
        self.engine.configure({"enabled":True,"time":"10:00","qq":True})
        self.add("private-title")
        self.engine.tick(self.now)
        first = self.windows.send.call_args.args[0]
        self.assertEqual(first.items[0]["title"],"private-title")
        self.save({"private":True})
        self.windows.remove.assert_any_call(first.tag)
        self.qq.cancel.assert_called_once_with(first.key,1)
        self.engine.delivery.acknowledge(first.key,"windows",1,"cancelled",self.now)
        self.engine.delivery.acknowledge(first.key,"qq",1,"cancelled",self.now)
        self.engine.tick(self.now)
        self.assertEqual(self.windows.send.call_args.args[0].items,({},))
        self.assertEqual(self.qq.send.call_args.args[0].items,({},))
        self.assertIs(self.qq.send.call_args.args[-1],True)

    def test_owner_thread_recovers_verified_config_without_replaying_a_commit(self):
        preview = self.proposal({"time":"11:05"})["pending_operation"]
        original = self.service.journal.put

        def fail_receipt(kind, identifier, owner, record):
            if kind == "operation" and record.get("status") == "succeeded":
                raise OSError("isolated journal failure")
            return original(kind,identifier,owner,record)

        with patch.object(self.service.journal,"put",side_effect=fail_receipt):
            self.assertEqual(self.send("确认")["status"],"uncertain")
        reply = self.service.get_operation_result(self.session,preview["operation_id"])
        self.assertEqual(reply["status"],"succeeded")
        self.assertEqual(self.engine.config()["time"],"11:05")
        self.assertEqual(self.store.settings[STATE_KEY]["config_versions"]["time"],1)
