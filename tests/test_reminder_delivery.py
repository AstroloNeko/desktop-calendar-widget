from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from calendar_core import Event, Store
from desktop_calendar.reminders.notification_model import (
    Notice, brief_content, brief_items, brief_settings, brief_window, identity,
)
from desktop_calendar.reminders.qq_reminders import QQReminderBackend, VerifiedBinding, notice_payload
from desktop_calendar.reminders.reminder_engine import CONFIG_KEY, STATE_KEY, ReminderEngine, ReminderError
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

    def test_morning_upgrade_keeps_legacy_ledger_evening_has_own_slot(self):
        self.add("today", self.now.replace(hour=13))
        self.engine.configure({"brief": {"enabled": True, "time": "08:00", "qq": False}})
        key = identity("brief", "windows", self.now.date().isoformat(), "")
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.assertEqual(notice.key, key)
        self.engine.acknowledge(key, True, self.now)
        state = self.engine._state()
        del state["batches"][key]["brief_slot"]
        self.engine._save(STATE_KEY, state)  # Actual pre-upgrade ledger shape.
        self.store.repository.write_reminder_preference(CONFIG_KEY, {"brief": {
            "enabled": True, "time": "08:00", "windows": True, "qq": False,
        }})
        engine = ReminderEngine(self.store, self.windows, self.qq)
        prefs = engine.config()["brief"]
        self.assertTrue(prefs["enabled"])
        self.assertFalse(prefs["evening_enabled"])
        engine.configure({"brief": {**prefs, "evening_enabled": True}})
        engine.tick(self.now + timedelta(hours=1))
        evening = self.now.replace(hour=18)
        engine.tick(evening)
        late = self.windows.send.call_args.args[0]
        self.assertEqual((late.brief_slot, late.progress.total, late.progress.completed), ("evening", 1, 0))
        self.assertNotEqual(late.key, key)
        engine.acknowledge(late.key, True, evening)
        ReminderEngine(self.store, self.windows, self.qq).tick(evening + timedelta(minutes=1))
        self.assertEqual(self.windows.send.call_count, 2)

    def test_evening_statistics_exclude_external_deadlines_and_future_today_is_not_overdue(self):
        self.add("already", self.now.replace(hour=10), done=True)
        self.add("ddl-done", self.now.replace(hour=13), event_type="ddl", done=True)
        self.add("future-today", self.now.replace(hour=21))
        self.add("today-ddl", self.now.replace(hour=17), event_type="ddl")
        self.add("past-ddl", self.now - timedelta(days=1), event_type="ddl")
        self.add("future-ddl", self.now + timedelta(days=2), event_type="ddl")
        evening = self.now.replace(hour=18)
        prefs = brief_settings({})
        items, progress = brief_content(self.store, evening, prefs, slot="evening")
        self.assertEqual((progress.total, progress.completed, progress.remaining, progress.percent), (4, 2, 2, 50))
        self.assertEqual([(x["event_id"], x["group"], x.get("overdue", False)) for x in items],
                         [("future-today", "timed", False), ("today-ddl", "today_ddl", True),
                          ("future-ddl", "upcoming", False), ("past-ddl", "overdue", True)])
        morning = brief_items(self.store, self.now, prefs, False)
        self.assertEqual([item["event_id"] for item in morning], [item["event_id"] for item in items])
        empty, stats = brief_content(self.store, evening, prefs, slot="evening", events=[])
        self.assertEqual(empty, ())
        self.assertIsNone(stats.percent)

    def test_brief_windows_use_local_day_and_cut_off_morning_at_evening(self):
        prefs = brief_settings({"time": "14:00", "evening_time": "16:30", "evening_enabled": True})
        self.assertEqual(brief_window(self.now.date(), prefs, "morning"),
                         (self.now.replace(hour=14), self.now.replace(hour=16, minute=30)))
        prefs["evening_time"] = "23:00"
        self.assertEqual(brief_window(self.now.date(), prefs, "evening")[1],
                         (self.now + timedelta(days=1)).replace(hour=0))

    def test_completed_queued_notice_is_cancelled_before_dispatch_and_can_replan(self):
        self.engine.configure({"enabled": True, "windows": True})
        event = self.add("finish-in-queue")
        self.engine.tick(self.now)
        notice = self.windows.send.call_args.args[0]
        self.commands.update(event.id, done=True)
        self.engine.invalidate()
        self.windows.remove.assert_any_call(notice.tag)
        self.engine.delivery.acknowledge(notice.key, "windows", 1, "cancelled", self.now)
        self.engine.tick(self.now)
        self.assertEqual(self.engine._state()["batches"][notice.key]["channels"]["windows"]["status"], "cancelled")
        self.windows.send.assert_called_once()
        self.commands.update(event.id, done=False)
        self.engine.invalidate()
        self.engine.tick(self.now + timedelta(minutes=1))
        resent = self.windows.send.call_args.args[0]
        self.assertEqual((resent.key, resent.attempt), (notice.key, 2))
        self.engine.acknowledge(resent.key, True, self.now)
        self.commands.update(event.id, done=True)
        self.engine.replan([event.id])
        self.commands.update(event.id, done=False)
        self.engine.replan([event.id])
        self.engine.tick(self.now + timedelta(minutes=2))
        self.assertEqual(self.windows.send.call_count, 2)

    def test_completing_snoozed_item_cancels_snooze_and_reopening_uses_base_plan(self):
        from desktop_calendar.reminders.reminder_runtime import live_instances
        event = self.add("snoozed-then-done", self.now + timedelta(minutes=30))
        self.engine.tick(self.now)
        _config, plans, state = self.engine.read_snapshot()
        plan = plans[event.id]
        revision = live_instances(self.engine, plans, state)[plan.key][1]
        self.engine.apply_runtime_action({"action":"snooze", "minutes":10},
            [{"key":plan.key, "event_id":event.id, "revision":revision}], self.now)
        self.commands.update(event.id, done=True)
        self.engine.replan([event.id])
        self.assertNotIn(event.id, self.engine._state()["snoozes"])
        self.engine.tick(self.now + timedelta(minutes=30))
        self.windows.send.assert_not_called()
        self.commands.update(event.id, done=False)
        self.engine.replan([event.id])
        self.engine.tick(self.now + timedelta(minutes=40))
        self.assertEqual(self.windows.send.call_args.args[0].key, plan.key)

    def test_evening_qq_compatible_payload_and_negotiated_private_projection(self):
        self.add("completed", self.now, done=True)
        self.add("today-ddl", self.now, event_type="ddl")
        items, progress = brief_content(self.store, self.now, brief_settings({}), slot="evening")
        notice = Notice("b"*64, "brief", self.now, self.now + timedelta(minutes=2), items,
                        brief_slot="evening", progress=progress)
        old = notice_payload(notice, self.binding, False)
        self.assertNotIn("brief_context", old)
        self.assertEqual(old["items"][0]["group"], "upcoming")
        self.assertIn("完成率 50%", old["fallback_text"])
        capable = replace(self.binding, brief_context_version=1)
        self.assertEqual(notice_payload(notice, capable, False)["brief_context"],
                         {"slot":"evening", "completion":{"total":2,"completed":1}})
        private = notice_payload(notice, capable, True)
        self.assertEqual(private["brief_context"], {"slot":"evening"})
        self.assertEqual(private["items"], [])
        self.assertNotIn("50%", private["fallback_text"])

    def test_brief_context_obeys_slot_capability_and_completion_limits(self):
        from desktop_calendar.reminders.notification_model import BriefProgress
        capable = replace(self.binding, brief_context_version=1)
        notice = Notice("b" * 64, "brief", self.now, self.now + timedelta(minutes=2), ())
        self.assertEqual(notice_payload(notice, capable, False)["brief_context"], {"slot": "morning"})
        for total, completed, allowed in ((0, 0, True), (5000, 5000, True), (5001, 2, False),
                                          (3, 4, False), (4, -1, False)):
            with self.subTest(total=total, completed=completed):
                evening = replace(notice, brief_slot="evening", progress=BriefProgress(total, completed))
                public = notice_payload(evening, capable, False)
                self.assertEqual("completion" in public["brief_context"], allowed)
                self.assertNotIn("brief_context", notice_payload(evening, self.binding, False))
                private = notice_payload(evening, capable, True)
                self.assertEqual(private["brief_context"], {"slot": "evening"})
                self.assertNotIn("brief_counts", private)
                self.assertFalse(private["items"])
                self.assertNotIn("完成率", private["fallback_text"])
        self.assertNotIn("brief_context", notice_payload(replace(notice, kind="event"), capable, False))

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

    def test_proactive_brief_preserves_ddl_beyond_first_fifty_and_hides_private_counts(self):
        totals = {"timed": 54, "untimed": 5, "unfinished": 4, "upcoming": 6, "overdue": 4}
        quotas = {"timed": 5, "untimed": 3, "unfinished": 3, "upcoming": 4, "overdue": 3}
        items = tuple({"group": group, "title": f"{group}-{index:02d} " + "长" * 175,
                       "date": "2026-11-25", "time": "08:00"}
                      for group, total in totals.items() for index in range(total))
        notice = Notice("a" * 64, "brief", self.now, self.now + timedelta(minutes=2), items)
        expected = [f"{group}-{index:02d}" for group, quota in quotas.items() for index in range(quota)]

        payload = notice_payload(notice, self.binding, False)
        self.assertEqual(payload["brief_counts"], totals)
        self.assertEqual(payload["count"], sum(totals.values()))
        self.assertEqual([item["title"].split()[0] for item in payload["items"]], expected)
        self.assertEqual([item["index"] for item in payload["items"]], list(range(1, 19)))
        fallback = payload["fallback_text"]
        self.assertEqual([next((word for word in line.split() if word in expected), "")
                          for line in fallback.splitlines() if any(word in expected for word in line.split())], expected)
        self.assertIn("今天到期", fallback)
        self.assertIn("今天逾期", fallback)
        self.assertLessEqual(len(fallback.encode("utf-8")), 1900)
        self.assertIn(f"共 {sum(totals.values())} 项", fallback)

        private = notice_payload(notice, self.binding, True)
        self.assertEqual(private["count"], payload["count"])
        self.assertEqual(private["items"], [])
        self.assertNotIn("brief_counts", private)
        self.assertNotIn("overdue", private["fallback_text"])

        for kind in ("event", "ddl"):
            with self.subTest(kind=kind):
                regular = Notice("b" * 64, kind, self.now, notice.expires, items)
                payload = notice_payload(regular, self.binding, False)
                self.assertNotIn("brief_counts", payload)
                self.assertEqual([item["title"] for item in payload["items"]], [item["title"] for item in items[:50]])

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
