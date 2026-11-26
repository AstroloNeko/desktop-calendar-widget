from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from calendar_core import Event, Store
from desktop_calendar.reminders.notification_model import Notice, brief_items, brief_settings
from desktop_calendar.reminders.qq_reminders import QQReminderBackend, VerifiedBinding, notice_payload
from desktop_calendar.reminders.reminder_engine import ReminderEngine, ReminderError
from desktop_calendar.remote_transport import Unavailable
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import EventCommandHistory


class ReminderDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "calendar.sqlite3"
        self.store = Store(self.path, repository_factory=SqliteCalendarRepository)
        self.commands = EventCommandHistory(self.store)
        self.windows, self.qq = Mock(), Mock()
        self.now = datetime(2026, 11, 25, 8)
        self.binding = VerifiedBinding(object(), 1, "https://example.test", "token-a", "account-a")
        self.qq.binding.return_value = self.binding
        self.engine = ReminderEngine(self.store, self.windows, self.qq)
        self.engine.configure({"enabled": True})

    def add(self, identifier, when=None, **fields):
        values = {"id": identifier, "title": f"title {identifier}", "due": (when or self.now).isoformat(),
                  "has_time": True, "reminder": 0}
        event = Event(**{**values, **fields})
        self.commands.upsert(event)
        self.engine.invalidate()
        return event

    def test_3_8_20_items_aggregate_and_ddl_separate(self):
        for count in (3, 8, 20):
            with self.subTest(count=count):
                for index in range(count):
                    self.add(f"{count}-{index}", self.now)
                self.add(f"ddl-{count}", self.now, event_type="ddl")
                self.windows.reset_mock()
                self.engine.tick(self.now)
                notices = {call.args[0].kind: call.args[0] for call in self.windows.send.call_args_list}
                self.assertEqual({key: len(notice.items) for key, notice in notices.items()}, {"ddl": 1, "event": count})
                self.assertIn(f"共 {count} 项", notices["event"].summary(limit=2))
                for notice in notices.values():
                    self.engine.acknowledge(notice.key, True, self.now)
                self.engine.tick(self.now + timedelta(seconds=2))
                self.assertEqual(self.windows.send.call_count, 2)
                self.now += timedelta(minutes=1)

    def test_minute_cohort_holds_only_for_members_in_the_same_minute(self):
        self.add("a")
        self.add("b", self.now + timedelta(seconds=40))
        self.add("c", self.now + timedelta(minutes=1))
        self.engine.tick(self.now)
        self.windows.send.assert_not_called()
        self.engine.tick(self.now + timedelta(seconds=40))
        self.assertEqual(len(self.windows.send.call_args.args[0].items), 2)
        self.engine.tick(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.windows.send.call_args.args[0].items), 1)

    def test_aggregation_opt_out_dispatches_every_member(self):
        self.engine.configure({"enabled": True, "aggregate": False})
        for index in range(8):
            self.add(str(index))
        self.engine.tick(self.now)
        self.assertEqual(self.windows.send.call_count, 8)

    def test_resume_merges_backlog_and_expires_previous_day(self):
        self.add("first", self.now - timedelta(hours=1))
        self.add("second", self.now - timedelta(hours=2))
        self.add("expired", self.now - timedelta(days=1, seconds=2))
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.assertEqual({item["event_id"] for item in notice.items}, {"first", "second"})

    def test_one_channel_failure_does_not_replay_the_other(self):
        for failed in ("windows", "qq"):
            with self.subTest(failed=failed):
                self.now += timedelta(minutes=5)
                self.add(failed)
                self.engine.configure({"enabled": True, "qq": True})
                self.windows.reset_mock()
                self.qq.send.reset_mock()
                self.engine.tick(self.now)
                notice = self.windows.send.call_args.args[0]
                self.engine.acknowledge(notice.key, failed != "windows", self.now, reason="win error")
                self.engine.delivery.acknowledge(notice.key, "qq", 1, "failed" if failed == "qq" else "sent",
                                                 self.now, "qq error" if failed == "qq" else "")
                self.engine.tick(self.now + timedelta(minutes=2))
                self.assertEqual(self.windows.send.call_count, 2 if failed == "windows" else 1)
                self.assertEqual(self.qq.send.call_count, 2 if failed == "qq" else 1)
                self.engine.acknowledge(notice.key, True, self.now + timedelta(minutes=2))
                self.engine.delivery.acknowledge(notice.key, "qq", 2, "sent", self.now + timedelta(minutes=2))

    def test_unknown_is_not_retried_after_restart(self):
        self.add("a")
        self.engine.configure({"enabled": True, "qq": True})
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.delivery.acknowledge(notice.key, "qq", 1, "unknown", self.now)
        restarted = ReminderEngine(Store(self.path, repository_factory=SqliteCalendarRepository), self.windows, self.qq)
        restarted.tick(self.now + timedelta(minutes=15))
        channels = restarted.batch_record(notice.key)["channels"]
        self.assertEqual({key: value["status"] for key, value in channels.items()}, {"windows": "unknown", "qq": "unknown"})
        self.windows.send.assert_called_once()
        self.qq.send.assert_called_once()

    def test_windows_submits_while_qq_reverifies_then_qq_delivers(self):
        self.engine.configure({"enabled": True, "qq": True})
        self.engine.tick(self.now)
        self.qq.binding.return_value = None
        self.now += timedelta(minutes=1)
        self.add("waiting")
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key, True, self.now)
        self.qq.send.assert_not_called()
        self.assertEqual(self.engine.batch_record(notice.key)["channels"]["qq"]["status"], "pending")
        self.qq.binding.return_value = self.binding
        self.engine.tick(self.now + timedelta(seconds=2))
        self.qq.send.assert_called_once()
        self.windows.send.assert_called_once()

    def test_old_account_definitive_failure_does_not_retry_after_rebind(self):
        self.add("old")
        self.engine.configure({"enabled": True, "qq": True, "windows": False})
        self.engine.tick(self.now)
        notice = self.qq.send.call_args.args[0]
        self.engine.delivery.acknowledge(notice.key, "qq", 1, "failed", self.now)
        self.qq.binding.return_value = VerifiedBinding(object(), 2, self.binding.url, "token-b", "account-b")
        self.now += timedelta(minutes=5)
        self.add("new")
        self.engine.tick(self.now)
        self.assertEqual(self.engine.batch_record(notice.key)["channels"]["qq"]["status"], "cancelled")
        new_notice, new_binding, _ = self.qq.send.call_args.args
        self.assertEqual(new_binding.account, "account-b")
        self.assertEqual([item["event_id"] for item in new_notice.items], ["new"])

    def test_delete_or_complete_only_retries_live_members(self):
        self.add("keep")
        self.add("done")
        self.add("delete")
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.engine.acknowledge(notice.key, False, self.now)
        self.add("done", done=True)
        self.commands.delete("delete")
        self.engine.invalidate()
        self.engine.tick(self.now + timedelta(minutes=2))
        self.assertEqual([item["event_id"] for item in self.windows.send.call_args.args[0].items], ["keep"])

    def test_definitive_cancel_before_windows_send_keeps_other_members(self):
        self.add("keep")
        self.add("delete")
        self.engine.tick(self.now)
        queued = self.windows.send.call_args.args[0]
        self.commands.delete("delete")
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.windows.remove.assert_any_call(queued.tag)
        self.engine.delivery.acknowledge(queued.key, "windows", 1, "cancelled", self.now)
        self.engine.tick(self.now + timedelta(seconds=2))
        replacement = self.windows.send.call_args.args[0]
        self.assertEqual([item["event_id"] for item in replacement.items], ["keep"])
        self.assertEqual(replacement.key, queued.key)

    def test_qq_ack_and_batch_snooze_are_local_and_scoped(self):
        first = self.add("first")
        self.add("second")
        self.engine.configure({"enabled": True, "qq": True})
        self.engine.tick(self.now)
        notice = self.qq.send.call_args.args[0]
        self.engine.delivery.acknowledge(notice.key, "qq", 1, "sent", self.now)
        self.assertEqual(self.engine.apply_qq_action(notice.key, "acknowledge", self.binding, self.now), {"acknowledged": True})
        self.assertFalse(self.store.event_by_id(first.id).done)
        self.assertEqual(self.engine.apply_qq_action(notice.key, "snooze_10m", self.binding, self.now), {"snoozed": 0})
        self.now += timedelta(minutes=1)
        self.add("third")
        self.add("fourth")
        self.engine.tick(self.now)
        notice = self.qq.send.call_args.args[0]
        self.engine.delivery.acknowledge(notice.key, "qq", 1, "sent", self.now)
        self.assertEqual(self.engine.apply_qq_action(notice.key, "snooze_10m", self.binding, self.now), {"snoozed": 2})
        self.assertEqual(self.engine.apply_qq_action(notice.key, "snooze_10m", self.binding, self.now), {"snoozed": 0})
        self.engine.cancel_snooze("third", now=self.now)
        self.engine.tick(self.now + timedelta(minutes=10))
        self.assertEqual([item["event_id"] for item in self.qq.send.call_args.args[0].items], ["fourth"])
        self.assertEqual(first.due, self.store.event_by_id(first.id).due)
        with self.assertRaises(ReminderError):
            self.engine.apply_qq_action(notice.key, "view_items", None, self.now)

    def test_brief_group_priority_and_sorting(self):
        self.add("second", self.now.replace(hour=11))
        self.add("first", self.now.replace(hour=10))
        self.add("untimed", has_time=False)
        self.add("upcoming", self.now + timedelta(days=1), event_type="ddl")
        self.add("overdue", self.now - timedelta(days=1), event_type="ddl")
        prefs = brief_settings({})
        items = brief_items(self.store, self.now, prefs, False)
        self.assertEqual([(item["group"], item["event_id"]) for item in items],
                         [("timed", "first"), ("timed", "second"), ("untimed", "untimed"),
                          ("upcoming", "upcoming"), ("overdue", "overdue")])
        unfinished = brief_items(self.store, self.now, {**prefs, "timed": False}, False)
        self.assertEqual([item["event_id"] for item in unfinished if item["group"] == "unfinished"], ["first", "second"])

    def test_brief_catchup_dedup_cross_midnight_and_expiry(self):
        self.add("today", self.now.replace(hour=13))
        self.engine.configure({"brief": {"enabled": True, "windows": True, "send_empty": True}})
        self.engine.tick(self.now - timedelta(minutes=1))
        self.windows.send.assert_not_called()
        self.engine.tick(self.now + timedelta(hours=3))
        notice = self.windows.send.call_args.args[0]
        self.assertEqual(notice.kind, "brief")
        self.engine.acknowledge(notice.key, True, self.now + timedelta(hours=3))
        restarted = ReminderEngine(Store(self.path, repository_factory=SqliteCalendarRepository), self.windows, self.qq)
        restarted.tick(self.now + timedelta(hours=3, minutes=30))
        restarted.tick(self.now + timedelta(days=1, hours=12))
        self.windows.send.assert_called_once()
        restarted.tick(self.now + timedelta(days=2))
        self.assertEqual(self.windows.send.call_count, 2)

    def test_empty_brief_does_not_send(self):
        self.engine.configure({"brief": {"enabled": True}})
        self.engine.tick(self.now)
        self.windows.send.assert_not_called()
        records = self.engine._state()["batches"].values()
        self.assertEqual([batch["channels"]["windows"]["status"] for batch in records], ["skipped"])

    def test_sqlite_reminder_write_preserves_unrelated_preferences_and_habit_history(self):
        self.store.settings["other"] = "keep"
        self.store.notified.add("routine:example")
        self.store.save()
        self.add("example")
        self.engine.tick(self.now)
        restored = Store(self.path, repository_factory=SqliteCalendarRepository)
        self.assertEqual(restored.settings["other"], "keep")
        self.assertIn("routine:example", restored.notified)

    def test_private_upload_removes_recent_items_ids_and_actions(self):
        now = datetime.now()
        notice = Notice("x" * 64, "event", now, now + timedelta(minutes=2),
                        ({"event_id": "secret-id", "title": "sensitive title"},))
        binding = VerifiedBinding(object(), 1, self.binding.url, self.binding.token, self.binding.account,
                                  ("acknowledge", "view_items", "snooze_10m"))
        payload = notice_payload(notice, binding, True)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"], [])
        self.assertEqual([action["action"] for action in payload["actions"]], ["acknowledge"])
        self.assertNotIn("sensitive", json.dumps(payload))
        self.assertNotIn("secret-id", json.dumps(payload))

    def test_https_transport_preserves_uncertainty_and_checks_queued_generation(self):
        from threading import RLock

        remote = SimpleNamespace(condition=RLock(), generation=7, config=(self.binding.url, "TOKEN", "", "owner"),
                                 proactive_enabled=True,proactive_actions=(),
                                 _current=lambda generation: generation == remote.generation)
        client = Mock()
        client.request.side_effect = Unavailable()
        backend = QQReminderBackend(lambda: remote, client_factory=lambda _: client)
        binding = backend.binding()
        now = datetime.now()
        notice = Notice("a" * 64, "event", now, now + timedelta(minutes=2), ())
        self.assertEqual(backend.submit(notice, binding, b"{}")[0], "unknown")
        remote.generation += 1
        self.assertEqual(backend.submit(notice, binding, b"{}")[0], "cancelled")
        client.request.assert_called_once()
