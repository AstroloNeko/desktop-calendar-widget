"""Cross-boundary v2 checks using a real Worker, Agent, and temporary SQLite."""
from datetime import datetime, timedelta
import json
from unittest import TestCase
from unittest.mock import Mock

from calendar_core import Event
from desktop_calendar.reminders.notification_model import Notice
from desktop_calendar.reminders.qq_reminders import QQReminderBackend, VerifiedBinding, notice_payload
from desktop_calendar.reminders.reminder_engine import ReminderEngine
from desktop_calendar.remote_interaction import CAPABILITIES
from desktop_calendar.remote_notifications import VerifiedAccount
from test_calendar_chat import tool
from test_remote_service import RemoteServiceTests as WorkerFixture


class RemoteReminderV2Tests(TestCase):
    setUp = WorkerFixture.setUp
    worker_for_steps = WorkerFixture.worker_for_steps
    process = WorkerFixture.process

    def enable(self):
        self.now = datetime.now().replace(microsecond=0)
        self.service.clock = self.now.timestamp
        self.runtime.gateway.reminder_now = lambda: self.now
        self.worker._notification(0,VerifiedAccount("owner-one",interaction_version=2,
                                  interaction_capabilities=CAPABILITIES,proactive_enabled=True,
                                  proactive_actions=("view_items","acknowledge","snooze_10m")))
        self.windows, self.qq = Mock(), Mock()
        binding = VerifiedBinding(self.worker,0,self.worker.config[0],self.worker.config[1],"owner-one",
                                  self.worker.proactive_actions)
        self.qq.binding.return_value = binding
        self.engine = self.executor(lambda: ReminderEngine(self.store,self.windows,self.qq))
        self.runtime.gateway.reminders = self.engine
        self.executor(lambda: self.engine.configure({"enabled":True,"qq":True}))

    def job(self, identifier, *, context=None, text="", sender="synthetic-user"):
        value = dict(msg_id=identifier,text=text,owner_id="owner-one",channel="qq",channel_app_id="123456789",
                     conversation_key="qq:123456789:"+sender,created_at_ts=int(self.now.timestamp()))
        return value | ({"interaction":{"schema_version":2,**context}} if context else {})

    def reply(self, message, replies=()):
        return json.loads(self.process(message,replies)["receipt"])

    def seed(self, count):
        def owned():
            for number in range(count):
                self.runtime.gateway.commands.upsert(Event(
                    str(number),f"合成日程 {number}",self.now.isoformat(),reminder=0))
            self.engine.invalidate()
            self.engine.tick(self.now)
        self.executor(owned)
        notice = self.qq.send.call_args.args[0]
        self.executor(lambda: self.engine.delivery.acknowledge(notice.key,"qq",1,"sent",self.now))
        return notice

    def action(self, notice, action, *, attempt=1):
        return dict(kind="proactive_action",notification_id=notice.key,channel="qq",attempt=attempt,
                    action=action,target=notice.key)

    def test_v2_quick_queries_use_receipts_and_never_pass_internal_fields(self):
        self.enable()
        self.seed(3)
        day = self.now.date().isoformat()
        filters = dict(start=day,end=day,filter="all")
        before = self.executor(lambda: self.engine._state())
        reminders = self.reply(self.job("reminder-q",context=dict(kind="quick_query",command="reminders",**filters)))
        self.assertEqual(reminders["business_receipt"]["schema_version"],2)
        receipt = reminders["business_receipt"]
        self.assertEqual(receipt["reminder_summary"]["total"],3)
        self.assertEqual(receipt["reminders"][0]["channels"]["qq"]["status"],"sent")
        self.assertIsNone(reminders["operation_id"])
        for text in ("plan_key","revision","event_id","operation_id"):
            self.assertNotIn(text,json.dumps(receipt))
        brief = self.reply(self.job("brief-q",context=dict(kind="quick_query",command="brief",date=day,scope="agenda")))
        self.assertEqual(brief["business_receipt"]["brief"]["agenda_count"],3)
        self.assertEqual(self.executor(lambda: self.engine._state()),before)
        self.assertEqual(self.provider.messages,[])

    def test_ack_snooze_exclusive_with_current_attempt_and_no_model(self):
        self.enable()
        notice = self.seed(3)
        stale = self.reply(self.job("stale",context=self.action(notice,"snooze_10m",attempt=2)))
        self.assertFalse(stale["ok"])
        ack = self.reply(self.job("ack",context=self.action(notice,"acknowledge")))
        self.assertEqual(ack["business_receipt"]["reminder_summary"]["count"],3)
        self.assertIsNone(ack["operation_id"])
        repeat = self.reply(self.job("ack",context=self.action(notice,"acknowledge")))
        self.assertEqual(ack,repeat)
        rejected = self.reply(self.job("snooze-after-ack",context=self.action(notice,"snooze_10m")))
        self.assertFalse(rejected["ok"])
        self.assertFalse(self.executor(lambda: any(item.done for item in self.store.events)))
        self.assertEqual(self.provider.messages,[])

    def test_reference_selector_only_snoozes_one_original_unchanged_instance(self):
        self.enable()
        notice = self.seed(8)
        context = dict(kind="proactive_reference",notification_id=notice.key,channel="qq",attempt=1,
                       notification_type="event")
        self.assertFalse(self.reply(self.job("ambiguous-reference",text="5分钟后提醒",context=context))["ok"])
        confirmed = self.reply(self.job("selected-reference",text="第2项5分钟后提醒",context=context))
        self.assertEqual(confirmed["business_receipt"]["reminder_summary"]["count"],1)
        self.assertEqual(len(self.executor(lambda: self.engine._state()["snoozes"])),1)
        self.assertEqual(self.provider.messages,[])

    def test_queued_revision_and_expired_quote_are_rejected(self):
        self.enable()
        notice = self.seed(1)
        ctx = self.action(notice,"snooze_10m")
        self.executor(lambda: self.runtime.gateway.commands.upsert(
            Event("0","已在桌面改名",self.now.isoformat(),reminder=0)))
        self.assertFalse(self.reply(self.job("stale-revision",context=ctx))["ok"])
        job = self.job("expired-notice",context=ctx)
        job["created_at_ts"] -= 901
        self.assertEqual(self.reply(job)["error"],"action_expired")
        self.assertEqual(self.provider.messages,[])

    def test_v1_cannot_receive_v2_and_text_fallback_does_not_execute(self):
        self.enable()
        self.worker._notification(0,VerifiedAccount("owner-one",interaction_version=1,
                                  interaction_capabilities=("business_receipt","quick_query")))
        day = self.now.date().isoformat()
        for message in (self.job("v2-brief",context=dict(kind="quick_query",command="brief",date=day,scope="agenda")),
                        self.job("plain-brief",text=f"查询 {day} 的 DDL 简报"),
                        self.job("slash-brief",text="/brief ddl")):
            self.assertFalse(self.reply(message)["ok"])
        self.assertEqual(self.provider.messages,[])

    def test_v2_privacy_authorization_has_no_confirmation_button(self):
        self.enable()
        preview = self.reply(self.job("privacy-config",text="提醒只发QQ"),[
            tool("propose_reminder_config",{"changes":{"windows":False,"brief":{"qq":True}}})])
        self.assertTrue(preview["ok"])
        self.assertFalse(preview["business_receipt"]["available_actions"])
        self.assertIn("授权确认",preview["reply_text"])
        self.assertIn("私人日历",preview["reply_text"])
        plain = self.reply(self.job("wrong-consent",text="确认"))
        self.assertIn("授权确认",plain["reply_text"])
        self.assertFalse(self.executor(lambda: self.engine.config()["brief"]["qq"]))
        final = self.reply(self.job("explicit-consent",text="授权确认"))
        self.assertIn("已核验隐私授权",final["business_receipt"]["summary"])
        self.assertTrue(self.executor(lambda: self.engine.config()["brief"]["qq"]))

    def test_delivery_decisions_require_matching_attempt_and_explicit_retry(self):
        self.enable()
        binding = self.qq.binding.return_value
        notice = Notice("a"*64,"event",self.now,self.now+timedelta(minutes=5),({"title":"合成"},),attempt=2)
        wire = notice_payload(notice,binding,False)
        self.assertEqual(wire["attempt"],2)
        common = dict(account_id=binding.account,notification_id=notice.key,channel="qq",attempt=2,status="failed")
        for outcome,http,retryable in (("rate_limited",429,True),("user_opt_out",403,False),
                                        ("expired",422,False),("failed",200,False)):
            status, detail = QQReminderBackend.delivery_outcome(notice,binding,{
                **common,"outcome":outcome,"retryable":retryable,"retry_after_seconds":60},http)
            self.assertEqual(status,outcome)
            self.assertEqual(detail.retryable,retryable)
        status, _ = QQReminderBackend.delivery_outcome(notice,binding,{
            **common,"attempt":1,"outcome":"failed","retryable":True},200)
        self.assertEqual(status,"unknown")


del WorkerFixture
