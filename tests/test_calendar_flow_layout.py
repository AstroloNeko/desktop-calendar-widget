import tempfile
import unittest
from datetime import date
from pathlib import Path

from calendar_core import Event, Store
from calendar_flow_layout import (
    DEFAULT_GLOBAL_DISPLAY_MODE,
    build_calendar_flow_layout,
    flow_card_detail_level,
    flow_day_at,
    flow_date_range_text,
    normalize_flow_drag_range,
    normalize_global_display_mode,
)
from timeline_model import build_month_timeline


class CalendarFlowLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "calendar.json")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _layout(self, *, lanes: int = 3):
        return build_calendar_flow_layout(build_month_timeline(self.store, 2026, 8), max_visible_lanes=lanes)

    def test_default_display_mode_preserves_existing_timeline(self) -> None:
        self.assertEqual(normalize_global_display_mode(None), DEFAULT_GLOBAL_DISPLAY_MODE)
        self.assertEqual(normalize_global_display_mode("unknown"), "timeline")
        self.assertEqual(normalize_global_display_mode("flow"), "flow")

    def test_month_is_projected_as_monday_first_weeks(self) -> None:
        layout = self._layout()
        self.assertEqual(layout.weeks[0].dates[0], date(2026, 7, 27))
        self.assertEqual(layout.weeks[0].dates[-1], date(2026, 8, 2))

    def test_continuous_multi_day_task_is_one_weekly_block(self) -> None:
        self.store.upsert(Event("multi", "连续事项", "2026-08-03T23:59", duration_days=3))
        blocks = self._layout().block_by_item_id("multi")
        self.assertEqual(len(blocks), 1)
        self.assertEqual((blocks[0].start_column, blocks[0].end_column), (0, 2))

    def test_cross_week_task_is_split_but_keeps_same_item(self) -> None:
        self.store.upsert(Event("cross", "跨周事项", "2026-08-06T23:59", duration_days=5))
        blocks = self._layout().block_by_item_id("cross")
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all(block.item.id == "cross" for block in blocks))
        self.assertTrue(blocks[0].continues_after)
        self.assertTrue(blocks[1].continues_before)

    def test_skip_weekend_uses_disconnected_timeline_segments(self) -> None:
        self.store.upsert(
            Event(
                "workdays",
                "工作日事项",
                "2026-08-07T23:59",
                duration_days=3,
                skip_non_working_days=True,
            )
        )
        blocks = self._layout().block_by_item_id("workdays")
        self.assertEqual([(block.start_date, block.end_date) for block in blocks], [
            (date(2026, 8, 7), date(2026, 8, 7)),
            (date(2026, 8, 10), date(2026, 8, 11)),
        ])

    def test_end_as_ddl_marks_only_block_containing_actual_deadline(self) -> None:
        self.store.upsert(
            Event(
                "deadline",
                "末日 DDL",
                "2026-08-07T23:59",
                duration_days=3,
                skip_non_working_days=True,
                end_as_ddl=True,
            )
        )
        blocks = self._layout().block_by_item_id("deadline")
        self.assertEqual([block.ddl_date for block in blocks], [None, date(2026, 8, 11)])

    def test_native_ddl_uses_same_marker_projection(self) -> None:
        self.store.upsert(Event("ddl", "原生 DDL", "2026-08-20T23:59", event_type="ddl"))
        block = self._layout().block_by_item_id("ddl")[0]
        self.assertEqual(block.ddl_date, date(2026, 8, 20))

    def test_overflow_is_counted_per_day_without_overlapping_cards(self) -> None:
        for index in range(5):
            self.store.upsert(Event(f"same-{index}", f"事项 {index}", "2026-08-20T23:59"))
        layout = self._layout(lanes=3)
        week = next(week for week in layout.weeks if date(2026, 8, 20) in week.dates)
        column = week.dates.index(date(2026, 8, 20))
        self.assertEqual(sum(block.visible for block in week.blocks), 3)
        self.assertEqual(week.hidden_on(column), 2)

    def test_non_overlapping_cards_reuse_lanes_without_false_more_count(self) -> None:
        self.store.upsert(Event("week", "整周事项", "2026-08-17T23:59", duration_days=7))
        self.store.upsert(Event("early", "周二事项", "2026-08-18T23:59", done=True))
        self.store.upsert(Event("late", "周四开始", "2026-08-20T23:59", duration_days=2))
        layout = self._layout(lanes=2)
        week = next(week for week in layout.weeks if date(2026, 8, 18) in week.dates)
        self.assertTrue(all(count == 0 for count in week.hidden_counts))
        lanes = {block.item.id: block.lane for block in week.blocks}
        self.assertEqual(lanes["early"], lanes["late"])

    def test_same_title_tasks_remain_independent(self) -> None:
        self.store.upsert(Event("first", "同名事项", "2026-08-04T23:59"))
        self.store.upsert(Event("second", "同名事项", "2026-08-05T23:59"))
        layout = self._layout()
        self.assertEqual(len(layout.block_by_item_id("first")), 1)
        self.assertEqual(len(layout.block_by_item_id("second")), 1)

    def test_completed_state_is_reused_from_timeline_item(self) -> None:
        self.store.upsert(Event("done", "完成事项", "2026-08-12T23:59", done=True))
        self.assertTrue(self._layout().block_by_item_id("done")[0].item.completed)

    def test_range_text_uses_canonical_item_dates(self) -> None:
        self.store.upsert(Event("range", "日期范围", "2026-08-03T23:59", duration_days=3))
        item = self._layout().block_by_item_id("range")[0].item
        self.assertEqual(flow_date_range_text(item), "8.3-8.5")

    def test_canvas_coordinates_map_to_week_and_date(self) -> None:
        layout = self._layout()
        self.assertEqual(
            flow_day_at(2 * 100 + 5, 2 * 140 + 5, layout=layout, column_width=100, week_height=140),
            date(2026, 8, 12),
        )
        self.assertIsNone(flow_day_at(-1, 5, layout=layout, column_width=100, week_height=140))

    def test_card_text_density_responds_to_available_width(self) -> None:
        self.assertEqual(flow_card_detail_level(90), "compact")
        self.assertEqual(flow_card_detail_level(160, span_columns=2), "medium")
        self.assertEqual(flow_card_detail_level(280, span_columns=3), "large")

    def test_single_day_card_keeps_only_title_even_when_column_is_wide(self) -> None:
        self.assertEqual(flow_card_detail_level(320, span_columns=1), "compact")

    def test_drag_range_is_inclusive_and_direction_independent(self) -> None:
        self.assertEqual(
            normalize_flow_drag_range(date(2026, 8, 7), date(2026, 8, 3)),
            (date(2026, 8, 3), date(2026, 8, 7), 5),
        )

    def test_drag_range_crosses_week_boundary_without_special_storage(self) -> None:
        self.assertEqual(
            normalize_flow_drag_range(date(2026, 8, 7), date(2026, 8, 11)),
            (date(2026, 8, 7), date(2026, 8, 11), 5),
        )


if __name__ == "__main__":
    unittest.main()
