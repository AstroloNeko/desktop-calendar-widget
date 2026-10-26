from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from calendar_core import Event, Store
from reminder_engine import CONFIG_KEY, STATE_KEY, ReminderEngine


class ReminderOverviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "calendar_data.json")
        self.backend = Mock()
        self.engine = ReminderEngine(self.store, self.backend)
        self.now = datetime(2031, 10, 24, 9)
        self.engine.configure({"enabled": True, "ddl_days": 0})

    def event(self, identifier="event-1", **kwargs):
        fields = dict(id=identifier, title="隔离测试 " + identifier, due="2031-10-24T23:59", has_time=False)
        fields.update(kwargs)
        event = Event(**fields)
        self.store.events.append(event)
        self.engine.invalidate()
        return event

    def row(self):
        return self.engine.overview(self.now).rows[0]

    def plan(self):
        self.engine.tick(self.now - timedelta(minutes=1))
        return next(iter(self.engine.plans.values()))

    def set_delivery(self, status, attempts=1, at=None):
        plan = self.plan()
        self.store.settings[STATE_KEY] = {"deliveries": {plan.key: {
            "status": status, "attempts": attempts, "at": (at or self.now).isoformat()}}}
        return plan

    def test_default_off_does_not_plan_or_write(self):
        self.event()
        self.store.settings.pop(CONFIG_KEY)
        before = deepcopy(self.store.settings)
        overview = self.engine.overview(self.now)
        self.assertFalse(overview.enabled)
        self.assertEqual(overview.rows, ())
        self.assertEqual(before, self.store.settings)
        self.backend.send.assert_not_called()

    def test_upcoming_uses_existing_plan(self):
        self.event(due="2031-10-25T23:59")
        row = self.row()
        self.assertEqual(row.when, datetime(2031, 10, 25, 9))
        self.assertEqual(row.group, "upcoming")
        self.assertEqual(row.status, "等待提醒")

    def test_read_does_not_rebuild_write_or_touch_cache(self):
        self.event()
        self.store.save()
        data = self.store.data_file.read_bytes()
        settings, queue, plans = deepcopy(self.store.settings), list(self.engine.queue), dict(self.engine.plans)
        revision = self.engine.revision
        for _ in range(3):
            self.engine.overview(self.now)
        self.assertEqual(data, self.store.data_file.read_bytes())
        self.assertEqual(settings, self.store.settings)
        self.assertEqual(queue, self.engine.queue)
        self.assertEqual(plans, self.engine.plans)
        self.assertEqual(revision, self.engine.revision)
        self.assertEqual(self.engine.rebuild_count, 0)
        self.backend.send.assert_not_called()
        self.backend.remove.assert_not_called()

    def test_general_multiday_uses_first_effective_day(self):
        self.event(duration_days=5, skip_non_working_days=True)
        self.store.is_workday = lambda day: day.weekday() < 5
        self.assertEqual(len(self.engine.overview(self.now).rows), 1)
        self.assertEqual(self.row().anchor, "2031-10-24")
        self.assertFalse(self.row().ddl)

    def test_end_ddl_uses_last_actual_date(self):
        self.event(duration_days=3, end_as_ddl=True, skip_non_working_days=True, event_type="urgent")
        self.store.is_workday = lambda day: day.weekday() < 5 and day != date(2031, 10, 27)
        row = self.row()
        self.assertEqual(row.anchor, "2031-10-29")
        self.assertEqual(row.when, datetime(2031, 10, 29, 9))
        self.assertTrue(row.ddl)

    def test_timed_reminder_not_replaced_by_global_clock(self):
        self.event(has_time=True, due="2031-10-24T13:00", reminder=30)
        self.assertEqual(self.row().when, datetime(2031, 10, 24, 12, 30))

    def test_disabled_event_and_completed_event_excluded(self):
        self.event("off")
        self.event("done", done=True)
        self.engine.configure({"enabled": True, "events": {"off": {"mode": "off"}}})
        self.assertFalse(self.engine.overview(self.now).rows)

    def test_submitted_not_shown_as_upcoming_or_seen(self):
        self.event()
        self.set_delivery("accepted")
        overview = self.engine.overview(self.now)
        self.assertEqual(overview.submitted, 1)
        self.assertFalse(overview.rows)

    def test_failure_in_attention_and_bounded_retry(self):
        self.event()
        self.set_delivery("failed", attempts=1)
        row = self.row()
        self.assertEqual(row.group, "attention")
        self.assertEqual(row.status, "派发失败")
        self.assertEqual(row.next_attempt, self.now + timedelta(minutes=2))

    def test_exhausted_has_no_retry(self):
        self.event()
        self.set_delivery("exhausted", attempts=3)
        self.assertEqual(self.row().group, "attention")
        self.assertIsNone(self.row().next_attempt)

    def test_last_failed_attempt_does_not_promise_fourth_send(self):
        self.event()
        self.set_delivery("failed", attempts=3)
        self.assertIsNone(self.row().next_attempt)

    def test_dispatching_is_not_reported_as_delivered(self):
        self.event()
        self.set_delivery("dispatching")
        self.assertEqual(self.row().status, "正在提交 / 等待结果")
        self.assertEqual(self.engine.overview(self.now).submitted, 0)

    def test_missed_catchup_does_not_promise_dispatch(self):
        self.event(due="2031-10-21T23:59")
        self.assertEqual(self.row().status, "已错过提醒")
        self.assertIsNone(self.row().next_attempt)

    def test_current_snooze_only_once_and_cancel_no_base_resend(self):
        event = self.event()
        plan = self.plan()
        self.engine.snooze(event.id, plan.key, 30, self.now)
        row = self.row()
        self.assertEqual(row.group, "snoozed")
        self.assertEqual(row.when, self.now + timedelta(minutes=30))
        self.assertEqual(len(self.engine.overview(self.now).rows), 1)
        self.engine.cancel_snooze(event.id)
        self.assertFalse(self.engine.overview(self.now).rows)
        self.engine.tick(self.now + timedelta(minutes=31))
        self.backend.send.assert_not_called()
        self.assertEqual(event.due, "2031-10-24T23:59")
        self.assertFalse(event.done)

    def test_failed_snooze_is_actionable_and_cancelable(self):
        event = self.event()
        self.engine.snooze(event.id, self.plan().key, 10, self.now)
        snooze = self.engine._state()["snoozes"][event.id]
        state = self.engine._state()
        state["deliveries"][snooze["key"]] = {"status": "failed", "attempts": 1, "at": self.now.isoformat()}
        self.store.settings[STATE_KEY] = state
        self.assertEqual(self.row().group, "attention")
        self.assertTrue(self.row().snoozed)

    def test_stale_snooze_ignored_after_date_edit(self):
        event = self.event()
        self.engine.snooze(event.id, self.plan().key, 10, self.now)
        event.due = "2031-10-28T23:59"
        row = self.row()
        self.assertFalse(row.snoozed)
        self.assertEqual(row.when, datetime(2031, 10, 28, 9))

    def test_corrupt_snooze_not_falsely_scheduled(self):
        event = self.event()
        plan = self.plan()
        self.store.settings[STATE_KEY] = {"snoozes": {event.id: {"base": plan.key, "key": "invalid", "at": "bad"}}}
        self.assertEqual(self.row().group, "attention")
        self.assertTrue(self.row().snoozed)
        self.assertIsNone(self.row().next_attempt)

    def test_deleted_and_completed_snoozes_not_in_view(self):
        event = self.event()
        self.engine.snooze(event.id, self.plan().key, 10, self.now)
        event.done = True
        self.assertFalse(self.engine.overview(self.now).rows)
        self.store.events.clear()
        self.assertFalse(self.engine.overview(self.now).rows)

    def test_legacy_accepted_read_without_import_write(self):
        self.event()
        plan = self.plan()
        self.store.notified.add(f"{plan.event_id}:reminder:{plan.when.isoformat(timespec='minutes')}")
        before = deepcopy(self.store.settings)
        self.assertEqual(self.engine.overview(self.now).submitted, 1)
        self.assertEqual(before, self.store.settings)

    def test_legacy_snooze_matches_dispatch_without_import_write(self):
        self.event(snooze_until="2031-10-24T11:00")
        before = deepcopy(self.store.settings)
        row = self.row()
        self.assertEqual(row.group, "snoozed")
        self.assertEqual(row.when.hour, 11)
        self.assertEqual(before, self.store.settings)
        self.backend.remove.assert_not_called()

    def test_burst_gate_shown_as_earliest_attempt(self):
        self.event()
        self.store.settings[STATE_KEY] = {"burst": {"at": self.now.isoformat(), "count": 3}}
        self.assertEqual(self.row().next_attempt, self.now + timedelta(minutes=5))

    def test_legacy_snooze_can_be_cancelled_before_first_tick(self):
        event = self.event(snooze_until="2031-10-24T11:00")
        self.assertTrue(self.row().snoozed)
        self.engine.cancel_snooze(event.id)
        self.assertFalse(self.engine.overview(self.now).rows)
        self.engine.tick(self.now + timedelta(hours=3))
        self.backend.send.assert_not_called()
        self.assertEqual(event.snooze_until, "2031-10-24T11:00")

    def test_last_dispatch_attempt_not_advertised_as_retryable(self):
        self.event()
        self.set_delivery("dispatching", attempts=3)
        row = self.row()
        self.assertIsNone(row.next_attempt)
        self.assertIn("最后一次", row.detail)

    def test_sort_is_stable_within_groups(self):
        self.event("b", due="2031-10-26T23:59")
        self.event("a", due="2031-10-25T23:59")
        self.event("c", due="2031-10-25T23:59")
        rows = self.engine.overview(self.now).rows
        self.assertEqual([row.event_id for row in rows], ["a", "c", "b"])

    def test_revision_changes_only_after_successful_write(self):
        before = self.engine.revision
        self.engine.overview(self.now)
        self.engine.invalidate()
        self.assertEqual(self.engine.revision, before)
        self.engine.configure({"enabled": False})
        self.assertEqual(self.engine.revision, before + 1)
        self.store.save = Mock(side_effect=OSError("isolated failure"))
        with self.assertRaises(OSError):
            self.engine.configure({"enabled": True})
        self.assertEqual(self.engine.revision, before + 1)

    def test_future_config_or_state_is_rejected_without_writes(self):
        for key in (CONFIG_KEY, STATE_KEY):
            with self.subTest(key=key):
                old = deepcopy(self.store.settings)
                self.store.settings[key] = {"version": 999}
                before = deepcopy(self.store.settings)
                with self.assertRaises(ValueError):
                    self.engine.overview(self.now)
                self.assertEqual(before, self.store.settings)
                self.store.settings = old


if __name__ == "__main__":
    unittest.main()
