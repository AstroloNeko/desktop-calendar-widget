import inspect
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from app import CalendarApp
from global_drag_ui import GlobalDragMixin


class ScheduleDragUITests(unittest.TestCase):
    def test_motion_only_updates_preview_and_schedules_one_overlay_frame(self):
        canvas = Mock()
        canvas.winfo_width.return_value = 1000
        canvas.winfo_height.return_value = 600
        fake = SimpleNamespace(
            _schedule_drag=Mock(active=True), _schedule_canvas=canvas,
            _schedule_render_job=None, _schedule_month_direction=0,
            _schedule_pointer_day=Mock(return_value=date(2026, 8, 10)),
            _draw_schedule_preview=Mock(), after=Mock(return_value="frame"),
            dpi=SimpleNamespace(px=lambda x: x),
        )
        event = SimpleNamespace(x=300, y=300, x_root=300, y_root=300)
        GlobalDragMixin._schedule_motion(fake, event)
        GlobalDragMixin._schedule_motion(fake, event)
        self.assertEqual(fake.after.call_count, 1)
        self.assertEqual(fake._schedule_drag.update.call_count, 2)
        canvas.delete.assert_not_called()

    def test_release_is_one_command_and_cleans_grab_before_commit(self):
        preview = object()
        fake = SimpleNamespace(
            _schedule_drag=SimpleNamespace(active=True, source=SimpleNamespace(event_id="task"), finish=lambda: preview),
            _schedule_motion=Mock(), _cancel_schedule_drag=Mock(),
            commit_event_preview=Mock(), select_timeline_item=Mock(),
        )
        self.assertEqual(GlobalDragMixin._schedule_release(fake, Mock()), "break")
        fake._cancel_schedule_drag.assert_called_once()
        fake.commit_event_preview.assert_called_once_with(preview, focus_result=True)
        fake.select_timeline_item.assert_not_called()

    def test_click_without_drag_only_selects(self):
        fake = SimpleNamespace(
            _schedule_drag=SimpleNamespace(active=False, source=SimpleNamespace(event_id="task"), finish=lambda: None),
            _schedule_motion=Mock(), _cancel_schedule_drag=Mock(),
            commit_event_preview=Mock(), select_timeline_item=Mock(),
        )
        GlobalDragMixin._schedule_release(fake, Mock())
        fake.commit_event_preview.assert_not_called()
        fake.select_timeline_item.assert_called_once_with("task")

    def test_escape_cancels_event_before_blank_selection_or_desktop_action(self):
        fake = SimpleNamespace(_schedule_drag=object(), _cancel_schedule_drag=Mock())
        self.assertEqual(CalendarApp._handle_escape(fake), "break")
        fake._cancel_schedule_drag.assert_called_once()

    def test_both_renderers_share_gesture_not_date_algorithms(self):
        for method in (CalendarApp._update_flow_drag, CalendarApp._finish_flow_drag):
            self.assertIn("_schedule_", inspect.getsource(method))
        self.assertIn("_schedule_press", inspect.getsource(CalendarApp._select_global_day))
        self.assertIn("_schedule_press", inspect.getsource(CalendarApp._start_flow_drag))
        self.assertNotIn("is_workday", inspect.getsource(GlobalDragMixin))

    def test_cancel_releases_grab_and_all_delayed_jobs(self):
        canvas = Mock()
        canvas.grab_current.return_value = canvas
        fake = SimpleNamespace(_schedule_drag=Mock(), _schedule_canvas=canvas,
                               _schedule_render_job="frame", _schedule_month_job="month",
                               after_cancel=Mock(), global_status_label=Mock())
        self.assertTrue(GlobalDragMixin._cancel_schedule_drag(fake))
        canvas.grab_release.assert_called_once()
        self.assertIsNone(fake._schedule_drag)
        self.assertEqual(fake.after_cancel.call_count, 2)
        self.assertFalse(GlobalDragMixin._cancel_schedule_drag(fake))

    def test_focus_loss_cancels_without_stealing_focus(self):
        fake = SimpleNamespace(_schedule_drag=object(), focus_displayof=lambda: None,
                               _cancel_schedule_drag=Mock())
        GlobalDragMixin._schedule_check_focus(fake)
        fake._cancel_schedule_drag.assert_called_once()

    def test_modal_editor_blocks_drag_start(self):
        fake = SimpleNamespace(_active_modal_window=lambda: object())
        self.assertIsNone(GlobalDragMixin._schedule_press(fake, Mock()))
