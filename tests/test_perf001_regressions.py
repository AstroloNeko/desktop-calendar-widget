"""Regression tests for PERF-001 rendering, reminder decoupling, and partial persistence."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import CalendarApp, DayCell
from backup_ui import BackupActionsMixin
from calendar_core import Event, Store
from desktop_calendar.global_view.global_header import GlobalHeader
from desktop_calendar.global_view.global_sidebar import GlobalSidebar
from desktop_calendar.reminders.reminder_engine import CONFIG_KEY, STATE_KEY, ReminderEngine, normalize_config
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from event_commands import BatchChange, EventChange, EventState
from holiday_data import HolidayInfo


class _DummyVar:
    def configure(self, **_kwargs):
        pass

    def itemconfigure(self, *_args, **_kwargs):
        pass

    def delete(self, *_args, **_kwargs):
        pass

    def tag_lower(self, *_args, **_kwargs):
        pass


class _DummyWindow:
    def winfo_exists(self):
        return False


class Perf001P0RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _sqlite_store(self, name: str = "cal.sqlite3") -> Store:
        store = Store(Path(self.temp.name) / name, repository_factory=SqliteCalendarRepository)
        self.addCleanup(store.repository.close)
        return store

    def test_render_does_not_invalidate_reminders_while_mutations_and_backup_do(self):
        store = Store(Path(self.temp.name) / "json_cal.json")
        today = date(2026, 11, 27)
        store.events.append(Event(id="ev-1", title="测试", due=f"{today.isoformat()}T10:00", has_time=True))
        cells = []
        for idx in range(42):
            c = SimpleNamespace(update_day=lambda **_kw: None, day=today, event_colors=[], ddl=False, selected=False, draw=lambda: None)
            cells.append(c)

        fake_app = SimpleNamespace(
            store=store,
            theme=SimpleNamespace(event_done="#888888"),
            view_mode="compact",
            shown_year=today.year,
            shown_month=today.month,
            selected=today,
            show_holidays=False,
            day_cells=cells,
            header=_DummyVar(),
            month_label="m",
            month_hint="h",
            day_detail_window=None,
            ddl_list_window=None,
            routine_manager=None,
            _schedule_drag=None,
            invalidate_reminders=Mock(),
            _update_mode_badge=Mock(),
            render_agenda=Mock(),
            render=Mock(),
        )
        fake_app.render = lambda: CalendarApp.render(fake_app)
        fake_app._refresh_routine_mutation = lambda: CalendarApp._refresh_routine_mutation(fake_app)

        CalendarApp.render(fake_app)
        self.assertEqual(fake_app.invalidate_reminders.call_count, 0)

        empty_state = EventState(None, None, frozenset())
        change = EventChange(
            event_id="ev-1",
            label="更新",
            before=empty_state,
            after=empty_state,
            before_dates=(),
            after_dates=(today,),
        )
        CalendarApp._refresh_event_change(fake_app, change)
        self.assertEqual(fake_app.invalidate_reminders.call_count, 1)

        CalendarApp._refresh_routine_mutation(fake_app)
        self.assertEqual(fake_app.invalidate_reminders.call_count, 2)

        CalendarApp._refresh_date_mutation(fake_app)
        self.assertEqual(fake_app.invalidate_reminders.call_count, 3)

        fake_backup = SimpleNamespace(
            store=store,
            view_mode="compact",
            _cancel_schedule_drag=Mock(),
            _reset_global_category_filter=Mock(),
            _invalidate_search=Mock(),
            set_theme=Mock(),
            _apply_theme_opacity=Mock(),
            _set_initial_geometry=Mock(),
            _apply_saved_geometry=Mock(),
            invalidate_reminders=Mock(),
            render=Mock(),
        )
        BackupActionsMixin._refresh_restored_data(fake_backup)
        self.assertEqual(fake_backup.invalidate_reminders.call_count, 1)
        fake_backup.render.assert_called_once()

    def test_idle_reminder_tick_uses_zero_deepcopy_and_zero_writes_but_clones_on_due(self):
        store = self._sqlite_store("reminders.sqlite3")
        now = datetime(2026, 11, 27, 9, 0)
        later_event = Event(id="ev-future", title="明日提醒", due="2026-11-28T15:00", has_time=True, reminder=0)
        store.repository.apply_changes({"events": [later_event]})

        backend = Mock()
        engine = ReminderEngine(store, backend)
        engine.configure(normalize_config({"enabled": True, "windows": True, "ddl_days": 0}))
        engine.tick(now)  # Warm up initial plan rebuild & cutover if needed.

        with (
            patch("desktop_calendar.reminders.reminder_engine.deepcopy", side_effect=AssertionError("unexpected deepcopy")),
            patch.object(store.repository, "_commit", wraps=store.repository._commit) as commit_spy,
        ):
            for _ in range(10):
                engine.tick(now)
            self.assertEqual(commit_spy.call_count, 0)

        # Now add an event that is due immediately and verify save failure rolls back/does not mutate store.settings.
        due_event = Event(id="ev-due", title="现在提醒", due="2026-11-27T09:00", has_time=True, reminder=0)
        store.repository.apply_changes({"events": [later_event, due_event]})
        engine.invalidate()
        before_state = repr(store.settings.get(STATE_KEY))

        with patch.object(store.repository, "_commit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                engine.tick(now)
        self.assertEqual(repr(store.settings.get(STATE_KEY)), before_state)

        engine.tick(now)
        self.assertIn(STATE_KEY, store.settings)
        self.assertTrue(store.settings[STATE_KEY]["batches"])

    def test_sqlite_write_preferences_skips_document_and_noop_commits(self):
        store = self._sqlite_store("prefs.sqlite3")
        store.repository.apply_changes({"events": [Event(id="e1", title="事项", due="2026-11-27T10:00", has_time=True)]})
        rev_before = store.repository.revision()

        with (
            patch.object(store.repository, "_document", side_effect=AssertionError("full _document read not allowed")),
            patch.object(store.repository, "_commit", wraps=store.repository._commit) as commit_spy,
        ):
            changed = store.save_preferences({"global_geometry": {"width": 1120, "height": 760, "x": 40, "y": 40}})
            self.assertTrue(changed)
            self.assertEqual(commit_spy.call_count, 1)
            self.assertEqual(store.repository.revision().generation, rev_before.generation + 1)

            changed_again = store.save_preferences({"global_geometry": {"width": 1120, "height": 760, "x": 40, "y": 40}})
            self.assertFalse(changed_again)
            self.assertEqual(commit_spy.call_count, 1)
            self.assertEqual(store.repository.revision().generation, rev_before.generation + 1)

        reopened = Store(store.data_file, repository_factory=SqliteCalendarRepository)
        self.addCleanup(reopened.repository.close)
        self.assertEqual(reopened.settings["global_geometry"], {"width": 1120, "height": 760, "x": 40, "y": 40})
        with closing(reopened.repository._connect()) as db:
            sync_mode = db.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(sync_mode, 2)  # FULL

    def test_json_write_preferences_noop_and_rollback(self):
        store = Store(Path(self.temp.name) / "json_prefs.json")
        self.assertTrue(store.save_preferences({"theme": "slate"}))
        self.assertEqual(store.settings["theme"], "slate")

        with patch.object(store, "save", wraps=store.save) as save_spy:
            self.assertFalse(store.save_preferences({"theme": "slate"}))
            self.assertEqual(save_spy.call_count, 0)

        with patch.object(store, "save", side_effect=OSError("fail")):
            with self.assertRaises(OSError):
                store.save_preferences({"theme": "modern"})
        self.assertEqual(store.settings["theme"], "slate")

    def test_complete_and_undo_via_desktop_commands_change_the_queue(self):
        from event_commands import EventCommandHistory
        store = self._sqlite_store()
        history = EventCommandHistory(store)
        item = Event("scheduled", "Meeting", "2026-12-03T10:00", has_time=True, reminder=0)
        history.upsert(item)
        engine = ReminderEngine(store, Mock())
        engine.configure({"enabled": True})
        owner = SimpleNamespace(store=store, event_commands=history, view_mode="compact", day_cells=[],
                                day_detail_window=None, ddl_list_window=None, selected=date(2026, 12, 3),
                                shown_year=2026, shown_month=12, render_agenda=Mock(),
                                invalidate_reminders=engine.invalidate)
        owner._refresh_event_change = lambda change, **kw: CalendarApp._refresh_event_change(owner, change, **kw)
        owner._run_event_command = lambda action: CalendarApp._run_event_command(owner, action)
        def queued():
            engine.tick(datetime(2026, 12, 1))
            return [plan.event_id for _, _, plan in engine.queue]
        self.assertEqual(queued(), [item.id])
        CalendarApp.toggle_done(owner, item)
        self.assertEqual(queued(), [])
        CalendarApp.undo_event(owner)
        self.assertEqual(queued(), [item.id])

    def test_root_configures_coalesce_and_detect_dpi_change_without_moving(self):
        owner = SimpleNamespace(_dpi_rebuilding=False, _dpi_check_job=None, _global_window_move=None,
                                winfo_exists=lambda: True, dpi=SimpleNamespace(dpi=96, current_window_dpi=lambda: 144),
                                _apply_dpi_change=Mock(), after=Mock(return_value="job"))
        owner._check_dpi_change = lambda: CalendarApp._check_dpi_change(owner)
        CalendarApp._schedule_dpi_check(owner, SimpleNamespace(widget=object()))
        owner.after.assert_not_called()
        for _ in range(3):
            CalendarApp._schedule_dpi_check(owner, SimpleNamespace(widget=owner))
        owner.after.assert_called_once()
        owner.after.call_args.args[1]()
        owner._apply_dpi_change.assert_called_once_with(144)
        self.assertIsNone(owner._dpi_check_job)

    def test_final_drag_position_is_committed_and_survives_immediate_close(self):
        from test_tray_lifecycle import FakeApp
        app = FakeApp()
        app.store = self._sqlite_store()
        app._geometry_save_job = None
        app.window_mode, app.theme_name, app.global_display_mode, app.agenda_open = "pinned", "modern", "flow", True
        app.dpi = SimpleNamespace(dpi=96, current_window_dpi=lambda: 96, logical=int)
        pos = [10, 20]
        app.winfo_width, app.winfo_height = lambda: 372, lambda: 548
        app.winfo_x, app.winfo_y = lambda: pos[0], lambda: pos[1]
        pending = []
        app.geometry = lambda spec: pending.append(spec)
        def settle():
            if pending:
                self.assertEqual(pending.pop(), "+100+200")
                pos[:] = [100, 200]
        app.update_idletasks = settle
        app._capture_window_geometry = lambda: CalendarApp._capture_window_geometry(app)
        app._save_preferences = app.store.save_preferences
        app._save_window_settings = lambda: CalendarApp._save_window_settings(app)
        with patch("app.clamp_to_work_area", return_value=(100, 200)):
            CalendarApp._end_drag(app)
        self.assertTrue(app.on_close())
        reopened = Store(app.store.data_file, repository_factory=SqliteCalendarRepository)
        self.addCleanup(reopened.repository.close)
        self.assertEqual((reopened.settings["x"], reopened.settings["y"]), (100, 200))

    def test_global_header_layout_and_sidebar_arrange_short_circuit(self):
        fake_header = SimpleNamespace(
            _last_layout_size=None,
            app=SimpleNamespace(
                dpi=SimpleNamespace(px=lambda v: v, logical=lambda v: v),
                theme=SimpleNamespace(style="modern", header_background="#fff", header_highlight="#fff", window_border_inner="#ccc"),
            ),
            sheen=Mock(),
            month=Mock(),
            subtitle=Mock(),
            controls={},
        )
        evt = SimpleNamespace(width=1000, height=58)
        GlobalHeader._layout(fake_header, evt)
        self.assertEqual(fake_header.sheen.delete.call_count, 1)
        GlobalHeader._layout(fake_header, evt)
        self.assertEqual(fake_header.sheen.delete.call_count, 1)

        fake_btn = SimpleNamespace(text="收起右栏", set_text=Mock())
        fake_sidebar = SimpleNamespace(
            workspace_width=0,
            app=SimpleNamespace(dpi=SimpleNamespace(px=lambda v: v, logical=lambda v: v), theme=SimpleNamespace(metrics=SimpleNamespace(workspace_gap=8))),
            DOCK_BELOW=1060,
            MAX_WIDTH=520,
            MIN_WIDTH=240,
            _drawer=False,
            drawer_open=False,
            dock_open=True,
            preferred_width=320,
            _last_arrange_key=None,
            drawer_button=fake_btn,
            configure=Mock(),
            cget=lambda _k: "320",
            winfo_manager=lambda: "grid",
            grid=Mock(),
            grid_forget=Mock(),
            place=Mock(),
            place_forget=Mock(),
            lift=Mock(),
            focus_get=lambda: None,
            grip=Mock(),
            _grip_color=lambda: "#ddd",
        )
        GlobalSidebar.arrange(fake_sidebar, 1200)
        GlobalSidebar.arrange(fake_sidebar, 1200)
        self.assertEqual(fake_sidebar.configure.call_count, 0)
        self.assertEqual(fake_sidebar.grid.call_count, 0)


if __name__ == "__main__":
    unittest.main()
