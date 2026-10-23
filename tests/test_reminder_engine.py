from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from calendar_core import Event, Store
from event_commands import EventCommandHistory
from reminder_engine import (CONFIG_KEY, STATE_KEY, ReminderEngine, ReminderError,
                             normalize_config, notification_arguments, parse_activation, plans_for_store)
from notification_backend import notification_identity


class ReminderEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calendar_data.json"
        self.store = Store(self.path)
        self.backend = Mock()
        self.engine = ReminderEngine(self.store, self.backend)
        self.now = datetime(2026, 10, 23, 9)
        self.config = normalize_config({"enabled": True, "ddl_days": 0})
        self.engine.configure(self.config)

    def event(self, **changes):
        values = dict(id="event-1", title="合成测试", due="2026-10-23T23:59", has_time=False)
        values.update(changes)
        event = Event(**values)
        self.store.events.append(event)
        self.engine.invalidate()
        return event

    def plan(self, event=None):
        plans = plans_for_store(self.store, self.engine.config())
        return next(plan for plan in plans if event is None or plan.event_id == event.id)

    def test_old_settings_default_off_no_write(self):
        other = Store(Path(self.temp.name) / "untouched.json")
        engine = ReminderEngine(other, self.backend)
        engine.tick(self.now)
        self.assertFalse(other.data_file.exists())
        self.backend.send.assert_not_called()
        self.assertFalse(engine.config()["enabled"])

    def test_general_today_clock_not_deadline(self):
        event = self.event()
        self.assertEqual(self.plan().when, self.now)
        self.assertFalse(self.plan().ddl)
        self.assertEqual(event.due, "2026-10-23T23:59")

    def test_urgent_uses_same_general_rule(self):
        self.event(event_type="urgent")
        self.assertEqual(self.plan().when, self.now)
        self.engine.tick(self.now)
        self.engine.tick(self.now + timedelta(seconds=15))
        self.assertEqual(self.backend.send.call_count, 1)

    def test_ddl_days_cross_month_year_and_leap(self):
        for due, days, expected in [("2026-11-01", 1, "2026-10-31"), ("2027-01-01", 3, "2026-12-29"),
                                     ("2028-03-01", 1, "2028-02-29"), ("2026-10-23", 0, "2026-10-23")]:
            with self.subTest(due=due, days=days):
                self.store.events.clear()
                self.event(event_type="ddl", due=due + "T23:59")
                self.engine.configure({**self.config, "ddl_days": days})
                self.assertEqual(self.plan().when.date().isoformat(), expected)

    def test_multiday_general_only_first_effective_day(self):
        self.event(duration_days=5)
        self.assertEqual(len(plans_for_store(self.store, self.config)), 1)
        self.assertEqual(self.plan().anchor, "2026-10-23")

    def test_end_as_ddl_last_actual_workday(self):
        event = self.event(duration_days=5, skip_non_working_days=True, end_as_ddl=True)
        self.store.is_workday = lambda day: day.weekday() < 5 and day != date(2026, 10, 26)
        self.assertEqual(self.plan().anchor, "2026-10-30")
        self.assertTrue(self.plan().ddl)
        self.assertEqual(len(plans_for_store(self.store, self.config)), 1)
        event.event_type = "urgent"
        self.assertEqual(self.plan().anchor, "2026-10-30")

    def test_start_holiday_leave_skipped_by_existing_store(self):
        self.event(due="2026-10-24T23:59", skip_non_working_days=True)
        self.store.set_date_status(date(2026, 10, 26), "leave")
        self.assertEqual(self.plan().anchor, "2026-10-27")

    def test_timed_legacy_minute_setting_preserved(self):
        self.event(has_time=True, due="2026-10-23T12:00", reminder=60)
        self.assertEqual(self.plan().when, self.now.replace(hour=11))

    def test_explicit_timed_no_reminder_preserved(self):
        self.event(has_time=True, reminder=None)
        self.assertFalse(plans_for_store(self.store, self.config))

    def test_single_event_disabled_and_custom_override(self):
        self.event()
        self.engine.configure({**self.config, "events": {"event-1": {"mode": "off"}}})
        self.assertFalse(plans_for_store(self.store, self.engine.config()))
        self.engine.configure({**self.config, "events": {"event-1": {"mode": "custom", "days": 3, "time": "10:30"}}})
        self.assertEqual(self.plan().when, datetime(2026, 10, 20, 10, 30))

    def test_invalid_preferences_safely_normalized(self):
        config = normalize_config({"enabled": "yes", "time": "48:99", "ddl_days": True,
                                   "events": {"a": {"mode": "custom", "days": -1, "time": "09:00"}}})
        self.assertFalse(config["enabled"])
        self.assertEqual(config["time"], "09:00")
        self.assertEqual(config["events"], {})

    def test_future_reminder_preferences_rejected(self):
        with self.assertRaises(ReminderError):
            normalize_config({"version": 999})

    def test_due_once_across_theme_or_view_invalidations(self):
        self.event()
        key = self.plan().key
        self.engine.tick(self.now)
        self.engine.acknowledge(key, True, self.now)
        for _ in range(10):
            self.engine.invalidate()
            self.engine.tick(self.now)
        self.assertEqual(self.backend.send.call_count, 1)

    def test_accepted_dedup_survives_restart_and_backwards_clock(self):
        self.event()
        self.engine.tick(self.now)
        self.engine.acknowledge(self.plan().key, True, self.now)
        restarted = ReminderEngine(Store(self.path), self.backend)
        for offset in (-30, 30, 0, 1440):
            restarted.tick(self.now + timedelta(minutes=offset))
        self.assertEqual(self.backend.send.call_count, 1)

    def test_wake_catchup_and_no_ancient_replay(self):
        self.event()
        self.engine.tick(self.now + timedelta(minutes=20))
        self.assertEqual(self.backend.send.call_count, 1)
        self.store.events.append(Event(id="ancient", title="旧测试", due="2026-10-20T23:59", has_time=False))
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.assertEqual(self.backend.send.call_count, 1)

    def test_midnight_wall_clock_jump(self):
        self.event(due="2026-10-24T23:59")
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()
        self.engine.tick(datetime(2026, 10, 24, 9, 20))
        self.assertEqual(self.backend.send.call_count, 1)

    def test_burst_limit_persists_and_no_repeated_full_scan(self):
        for index in range(1000):
            self.event(id=f"test-{index}")
        self.engine.tick(self.now)
        self.assertEqual(self.backend.send.call_count, 3)
        count = self.engine.rebuild_count
        for _ in range(20):
            self.engine.tick(self.now + timedelta(seconds=15))
        self.assertEqual(self.engine.rebuild_count, count)
        self.assertEqual(self.backend.send.call_count, 3)
        restarted = ReminderEngine(Store(self.path), self.backend)
        restarted.tick(self.now + timedelta(minutes=1))
        self.assertEqual(self.backend.send.call_count, 3)

    def test_failure_retry_bounded_not_permanent_success(self):
        self.event()
        key = self.plan().key
        for minutes in (0, 2, 4, 6):
            now = self.now + timedelta(minutes=minutes)
            self.engine.tick(now)
            self.engine.acknowledge(key, False, now)
        self.engine.tick(self.now + timedelta(hours=1))
        self.assertEqual(self.backend.send.call_count, 3)
        self.assertNotEqual(self.engine._state()["deliveries"][key]["status"], "accepted")

    def test_async_failure_wins_over_late_show_success(self):
        self.event()
        self.engine.tick(self.now)
        key = self.plan().key
        self.engine.acknowledge(key, False, self.now)
        self.engine.acknowledge(key, True, self.now)
        self.assertEqual(self.engine._state()["deliveries"][key]["status"], "failed")

    def test_crash_after_reservation_waits_before_retry(self):
        self.event()
        self.engine.tick(self.now)
        restarted = ReminderEngine(Store(self.path), self.backend)
        restarted.tick(self.now + timedelta(seconds=10))
        self.assertEqual(self.backend.send.call_count, 1)
        restarted.tick(self.now + timedelta(minutes=2))
        self.assertEqual(self.backend.send.call_count, 2)

    def test_persist_failure_prevents_send_and_rolls_back(self):
        self.event()
        before = deepcopy(self.store.settings)
        disk = self.path.read_bytes()
        with patch.object(self.store, "save", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.engine.tick(self.now)
        self.backend.send.assert_not_called()
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.path.read_bytes(), disk)

    def test_configuration_failure_rolls_back(self):
        before = deepcopy(self.engine.config())
        with patch.object(self.store, "save", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.engine.configure({"enabled": False})
        self.assertEqual(self.engine.config(), before)

    def test_done_deleted_and_disabled_do_not_send(self):
        event = self.event(done=True)
        self.engine.tick(self.now)
        event.done = False
        self.store.events.clear()
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.event()
        self.engine.configure({"enabled": False})
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()

    def test_snooze_10_30_60_persists_without_touching_event(self):
        for minutes in (10, 30, 60):
            with self.subTest(minutes=minutes):
                self.store.events.clear()
                self.store.settings.pop(STATE_KEY, None)
                event = self.event()
                original = deepcopy(event.__dict__)
                self.engine.tick(self.now)
                self.assertTrue(self.engine.snooze(event.id, self.plan().key, minutes, self.now))
                other = Store(self.path)
                engine = ReminderEngine(other, self.backend)
                calls = self.backend.send.call_count
                engine.tick(self.now + timedelta(minutes=minutes - 1))
                self.assertEqual(self.backend.send.call_count, calls)
                engine.tick(self.now + timedelta(minutes=minutes))
                self.assertEqual(self.backend.send.call_count, calls + 1)
                self.assertEqual(other.events[0].__dict__, original)

    def test_snooze_canceled_by_completion_deletion_or_date_change(self):
        for change in ("done", "delete", "date"):
            with self.subTest(change=change):
                self.store.events.clear()
                self.store.settings.pop(STATE_KEY, None)
                event = self.event()
                self.engine.snooze(event.id, self.plan().key, 10, self.now)
                if change == "done":
                    event.done = True
                elif change == "delete":
                    self.store.events.clear()
                else:
                    event.due = "2026-11-23T23:59"
                self.engine.invalidate()
                self.engine.tick(self.now)
                self.assertFalse(self.engine._state()["snoozes"])

    def test_cancel_snooze_does_not_resend_base(self):
        event = self.event()
        self.engine.snooze(event.id, self.plan().key, 10, self.now)
        self.engine.cancel_snooze(event.id)
        self.engine.tick(self.now + timedelta(minutes=11))
        self.backend.send.assert_not_called()

    def test_batch_delete_and_undo_no_stale_queue(self):
        self.event()
        history = EventCommandHistory(self.store)
        history.delete("event-1")
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()
        history.undo()
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.assertEqual(self.backend.send.call_count, 1)

    def test_date_change_old_plan_invalidated_new_plan_exists(self):
        event = self.event()
        old = self.plan()
        self.engine.tick(self.now)
        event.due = "2026-11-01T23:59"
        self.engine.invalidate()
        self.engine.tick(self.now)
        self.assertNotEqual(old.key, self.plan().key)
        self.assertFalse(self.engine.activation_is_current(event.id, old.key))
        self.backend.remove.assert_any_call(old.tag)

    def test_private_notification_has_no_title_notes_or_paths(self):
        self.event(title="PRIVATE-TITLE", notes="PRIVATE-NOTES")
        self.engine.configure({**self.config, "private": True})
        self.engine.tick(self.now)
        plan, title, text, actions = self.backend.send.call_args.args
        self.assertEqual(text, "你有一项日程需要处理")
        self.assertNotIn("PRIVATE", title + text + str(actions))

    def test_activation_protocol_rejects_unknown_or_forged_shape(self):
        self.event()
        plan = self.plan()
        arguments = notification_arguments(plan)
        self.assertEqual(parse_activation(arguments)["event"], plan.event_id)
        self.assertIsNone(parse_activation(arguments + "&action=60"))
        self.assertIsNone(parse_activation("v=999&event=x&plan=x&action=open"))
        self.assertIsNone(parse_activation("x" * 3000))

    def test_source_frozen_and_data_paths_have_isolated_identities(self):
        first = notification_identity("app.exe", "data.json", frozen=True)
        self.assertNotEqual(first, notification_identity("app.exe", "data.json", frozen=False))
        self.assertNotEqual(first, notification_identity("app.exe", "other.json", frozen=True))

    def test_backup_roundtrip_includes_rules_and_dispatch_ledger(self):
        from data_backup import create_backup, read_backup, restore_store
        self.event()
        self.engine.tick(self.now)
        self.engine.acknowledge(self.plan().key, True, self.now)
        target = Path(self.temp.name) / "test.dcalbackup"
        create_backup(self.path.read_bytes(), target)
        _manifest, data = read_backup(target)
        payload = json.loads(data)
        self.assertEqual(payload["settings"][CONFIG_KEY], self.store.settings[CONFIG_KEY])
        self.assertEqual(payload["settings"][STATE_KEY], self.store.settings[STATE_KEY])
        expected = deepcopy(self.store.settings[STATE_KEY])
        self.store.settings[CONFIG_KEY] = {"enabled": False}
        self.store.settings[STATE_KEY] = {"version": 1}
        self.store.save()
        restore_store(self.store, target, apply=self.engine.invalidate)
        self.assertEqual(self.store.settings[STATE_KEY], expected)
        count = self.backend.send.call_count
        self.engine.tick(self.now)
        self.assertEqual(self.backend.send.call_count, count, "restored accepted reminder was sent again")

    def test_legacy_matching_notification_not_resent(self):
        event = self.event(has_time=True, due="2026-10-23T09:00", reminder=0)
        self.store.notified.add(f"{event.id}:reminder:2026-10-23T09:00")
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()

    def test_legacy_future_snooze_imported_without_event_change(self):
        event = self.event(snooze_until="2026-10-23T09:30")
        original = deepcopy(event.__dict__)
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()
        self.engine.tick(self.now + timedelta(minutes=30))
        self.backend.send.assert_called_once()
        self.assertEqual(event.__dict__, original)

    def test_canceled_legacy_snooze_not_imported_again(self):
        event = self.event(snooze_until="2026-10-23T09:30")
        self.engine.tick(self.now)
        self.engine.cancel_snooze(event.id)
        self.engine.tick(self.now + timedelta(minutes=30))
        self.backend.send.assert_not_called()

    def test_corrupt_snooze_does_not_crash_or_send(self):
        event = self.event()
        self.store.settings[STATE_KEY] = {"version": 1, "snoozes": {
            event.id: {"base": self.plan().key, "key": None, "at": "2026-10-23T09:00"}}}
        self.engine.tick(self.now)
        self.backend.send.assert_not_called()

    def test_invalid_legacy_reminder_does_not_stop_other_events(self):
        for value in ("bad", True, -999, 10**100, None):
            with self.subTest(value=value):
                self.store.events.clear()
                self.event(id="bad", has_time=True, reminder=value)
                valid = self.event(id="valid")
                self.assertEqual([plan.event_id for plan in plans_for_store(self.store, self.config)], [valid.id])

    def test_aware_dispatch_metadata_is_normalized_to_local_wall_time(self):
        self.event()
        plan = self.plan()
        self.store.settings[STATE_KEY] = {"version": 1, "deliveries": {
            plan.key: {"status": "failed", "at": self.now.astimezone().isoformat(), "attempts": 1}},
            "burst": {"at": self.now.astimezone().isoformat(), "count": 1}}
        self.engine.tick(self.now + timedelta(minutes=3))
        self.backend.send.assert_called_once()

    def test_unknown_state_version_refuses_without_rewriting(self):
        self.event()
        self.store.settings[STATE_KEY] = {"version": 999, "future": "keep"}
        self.store.save()
        before = self.path.read_bytes()
        with self.assertRaises(ReminderError):
            self.engine.tick(self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.backend.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
