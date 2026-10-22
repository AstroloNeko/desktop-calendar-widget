import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import CalendarApp
from global_drag_ui import GlobalDragMixin
from global_search_ui import SearchDialog
from event_search import SearchResult
from interaction_visuals import CanvasLayer, CanvasOverlay, InteractionFeedbackMixin, elide, match_spans, palette_rows, preview_bounds, visible_rows


class _Canvas:
    def __init__(self):
        self.items, self.binds, self.unbound, self.creates = {}, {}, [], 0

    def create_rectangle(self, *coords, **options):
        self.creates += 1
        self.items[self.creates] = ["rectangle", coords, dict(options)]
        return self.creates

    create_polygon = create_rectangle

    def type(self, identifier):
        return self.items.get(identifier, [""])[0]

    def coords(self, identifier, *coordinates):
        self.items[identifier][1] = coordinates

    def itemconfigure(self, identifier, **options):
        self.items[identifier][2].update(options)

    def tag_raise(self, tag):
        pass

    def tag_bind(self, tag, sequence, callback):
        self.binds[tag, sequence] = callback
        return "callback"

    def tag_unbind(self, tag, sequence, identifier):
        self.unbound.append((tag, sequence, identifier))

    def find_withtag(self, tag):
        return tuple(identifier for identifier, item in self.items.items() if tag in item[2].get("tags", ()))


class InteractionPolishTests(unittest.TestCase):
    def test_virtual_window_is_bounded_for_large_models(self):
        window = visible_rows(1000, 3800, 380, 38)
        self.assertLessEqual(len(window), 17)
        self.assertIn(100, window)

    def test_virtual_window_reaches_last_item_and_handles_empty(self):
        self.assertIn(999, visible_rows(1000, 37900, 380, 38))
        self.assertEqual(list(visible_rows(0, 0, 200, 38)), [])
        self.assertEqual(list(visible_rows(1000, 0, 200, 0)), [])

    def test_virtual_rows_and_preview_bounds_scale_consistently(self):
        for dpi in (96, 120, 144, 168, 192):
            scale = dpi / 96
            self.assertEqual(list(visible_rows(1000, 3800 * scale, 380 * scale, 38 * scale)),
                             list(visible_rows(1000, 3800, 380, 38)))
            bounds = preview_bounds(590 * scale, 390 * scale, 260 * scale, 54 * scale,
                                    (0, 0, 600 * scale, 400 * scale), 6 * scale)
            self.assertLessEqual(bounds[2], 594 * scale)
            self.assertLessEqual(bounds[3], 394 * scale)

    def test_preview_clamps_in_scrolled_and_narrow_viewports(self):
        bounds = preview_bounds(800, 900, 260, 54, (700, 800, 900, 950), 6)
        self.assertGreaterEqual(bounds[0], 706)
        self.assertLessEqual(bounds[2], 894)
        self.assertEqual(preview_bounds(0, 0, 260, 54, (0, 0, 100, 100), 6)[2], 94)

    def test_highlight_spans_chinese_case_and_multiple_terms(self):
        self.assertEqual(match_spans("绘画 ART 视频", "art 绘画"), ((0, 2), (3, 6)))
        self.assertEqual(match_spans("任务任务", "任务"), ((0, 4),))
        self.assertEqual(match_spans("abc", ""), ())

    def test_highlight_unicode_casefold_keeps_original_indices(self):
        self.assertEqual(match_spans("Straße", "STRASSE"), ((0, 6),))

    def test_elision_never_exceeds_width(self):
        font = SimpleNamespace(measure=lambda text: len(text) * 10)
        self.assertEqual(elide("abcdef", font, 40), "abc…")
        self.assertEqual(elide("abcdef", font, 5), "")
        self.assertEqual(elide("abc", font, 40), "abc")

    def test_palette_groups_do_not_change_result_ranking(self):
        results = (SearchResult("event", "b", "", ""), SearchResult("event", "a", "", ""), SearchResult("command", "new", "", ""))
        rows, height = palette_rows(results, lambda value: value)
        self.assertEqual([row.index for row in rows if row.index is not None], [0, 1, 2])
        self.assertEqual([row.label for row in rows if row.index is None], ["匹配事项", "快捷操作"])
        self.assertEqual(height, 216)

    def test_empty_palette_layout(self):
        self.assertEqual(palette_rows((), lambda x: x), ((), 0))

    def test_overlay_reuses_slots_and_hides_unused_without_deleting(self):
        canvas = _Canvas()
        overlay = CanvasOverlay(canvas, "preview")
        for frame in range(100):
            overlay.begin()
            for slot in range(frame % 5 + 1):
                overlay.item(slot, "rectangle", (frame, 0, frame + 4, 10), fill="color")
            overlay.end()
        self.assertEqual(canvas.creates, 5)
        overlay.clear()
        self.assertTrue(all(item[2]["state"] == "hidden" for item in canvas.items.values()))

    def test_overlay_recreates_items_after_full_renderer_redraw(self):
        canvas = _Canvas()
        pool = CanvasOverlay(canvas, "preview")
        pool.begin()
        first = pool.item(0, "rectangle", (0, 0, 1, 1))
        canvas.items.clear()
        second = pool.item(0, "rectangle", (0, 0, 1, 1))
        self.assertNotEqual(first, second)

    def test_canvas_layer_tags_static_and_dynamic_content_separately(self):
        canvas = _Canvas()
        layer = CanvasLayer(canvas, "rows")
        identifier = layer.create_rectangle(0, 0, 10, 10, tags=("task",))
        self.assertEqual(canvas.items[identifier][2]["tags"], ("task", "rows"))

    def test_callbacks_are_replaced_without_registering_duplicates(self):
        canvas = _Canvas()
        layer = CanvasLayer(canvas)
        callbacks = []
        for index in range(100):
            layer.tag_bind("task", "<Enter>", lambda event, index=index: callbacks.append(index))
        self.assertEqual(len(canvas.binds), 1)
        canvas.binds["task", "<Enter>"](None)
        self.assertEqual(callbacks, [99])
        layer.prune_bindings()
        self.assertEqual(len(canvas.unbound), 1)
        self.assertEqual(canvas._interaction_bindings, {})

    def test_live_canvas_tag_callbacks_are_retained(self):
        canvas = _Canvas()
        layer = CanvasLayer(canvas)
        canvas.create_rectangle(0, 0, 1, 1, tags=("task",))
        layer.tag_bind("task", "<Enter>", lambda _event: None)
        layer.prune_bindings()
        self.assertEqual(len(canvas._interaction_bindings), 1)

    def test_pending_enter_defers_until_current_results_arrive(self):
        fake = SimpleNamespace(_pending=True, _closed=False)
        SearchDialog.activate(fake)
        self.assertTrue(fake._activate_when_ready)

    def test_pending_mouse_click_cannot_open_an_outdated_result(self):
        self.assertEqual(SearchDialog._click(SimpleNamespace(_pending=True), Mock()), "break")

    def test_blank_date_drag_does_not_rebuild_calendar_on_motion(self):
        method = inspect.getsource(CalendarApp._update_flow_drag)
        self.assertIn("_draw_flow_range_preview", method)
        self.assertNotIn("_draw_calendar_flow()", method)

    def test_ghost_does_not_delete_full_canvas_or_recompute_dates(self):
        source = inspect.getsource(GlobalDragMixin._draw_schedule_preview)
        self.assertNotIn('delete("all")', source)
        self.assertNotIn("occurrence_dates", source)
        self.assertIn("preview.effective_dates", source)

    def test_row_scroll_redraw_does_not_rebuild_model(self):
        source = inspect.getsource(CalendarApp._refresh_timeline_rows)
        self.assertIn("rows_only=True", source)
        self.assertNotIn("build_month_timeline", source)

    def test_palette_close_cancels_jobs_trace_and_releases_query_on_ui_thread(self):
        query = Mock()
        app = Mock()
        fake = SimpleNamespace(_closed=False, _debounce="input", _poll_job="poll", _activation_job="activate",
                               query=query, _query_trace="trace", after_cancel=Mock(),
                               grab_current=lambda: None, destroy=Mock(), app=app)
        SearchDialog.close(fake, restore_focus=False)
        self.assertIsNone(fake.query)
        query.trace_remove.assert_called_once_with("write", "trace")
        self.assertEqual(fake.after_cancel.call_count, 3)
        app._leave_modal.assert_called_once_with(fake)

    def test_feedback_replaces_existing_notice_and_timer(self):
        label = Mock()
        fake = SimpleNamespace(_feedback_job="timer", _feedback_label=label, after_cancel=Mock())
        InteractionFeedbackMixin._clear_feedback(fake)
        fake.after_cancel.assert_called_once_with("timer")
        label.destroy.assert_called_once()
        self.assertIsNone(fake._feedback_label)

    def test_feedback_has_no_modal_or_topmost_window(self):
        source = inspect.getsource(InteractionFeedbackMixin)
        self.assertNotIn("Toplevel", source)
        self.assertNotIn("grab_set", source)
        self.assertNotIn("topmost", source)
