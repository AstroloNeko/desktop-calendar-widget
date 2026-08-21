import unittest
from datetime import date

from global_timeline_ui import (
    GLOBAL_TIMELINE_LAYOUT,
    canvas_day_at,
    detail_state_text,
    primary_action_for_view,
    timeline_tooltip_text,
    timeline_bar_label,
    timeline_type_label,
    wheel_units,
)
from timeline_model import TimelineItem, TimelineSegment


def _item(**overrides) -> TimelineItem:
    values = {
        "id": "item-1",
        "title": "跨周末交付",
        "color": "#6F7EE8",
        "task_type": "urgent",
        "completed": False,
        "start_date": date(2026, 8, 7),
        "end_date": date(2026, 8, 10),
        "visible_start": date(2026, 8, 7),
        "visible_end": date(2026, 8, 10),
        "effective_dates": (date(2026, 8, 7), date(2026, 8, 10)),
        "visible_effective_dates": (date(2026, 8, 7), date(2026, 8, 10)),
        "skipped_dates": (date(2026, 8, 8), date(2026, 8, 9)),
        "segments": (
            TimelineSegment(date(2026, 8, 7), date(2026, 8, 7), (date(2026, 8, 7),)),
            TimelineSegment(date(2026, 8, 10), date(2026, 8, 10), (date(2026, 8, 10),)),
        ),
        "ddl_date": date(2026, 8, 10),
        "is_urgent": True,
        "native_ddl": False,
        "end_as_ddl": True,
        "continues_from_previous_period": False,
        "continues_to_next_period": False,
        "source_task_ids": ("item-1",),
        "calendar_span_days": 4,
        "effective_days_count": 2,
        "created_at": "2026-08-01T10:00",
        "notes": "交付前复核",
    }
    values.update(overrides)
    return TimelineItem(**values)


class GlobalTimelineUiTests(unittest.TestCase):
    def test_wide_workspace_shows_detail_panel(self) -> None:
        self.assertTrue(GLOBAL_TIMELINE_LAYOUT.show_detail_panel(1280))

    def test_narrow_workspace_hides_detail_panel(self) -> None:
        self.assertFalse(GLOBAL_TIMELINE_LAYOUT.show_detail_panel(900))

    def test_detail_panel_grows_smoothly_at_medium_width(self) -> None:
        self.assertEqual(GLOBAL_TIMELINE_LAYOUT.detail_panel_width(980), 220)
        self.assertLess(GLOBAL_TIMELINE_LAYOUT.detail_panel_width(1080), GLOBAL_TIMELINE_LAYOUT.detail_width)
        self.assertEqual(GLOBAL_TIMELINE_LAYOUT.detail_panel_width(1280), GLOBAL_TIMELINE_LAYOUT.detail_width)

    def test_dates_keep_minimum_readable_width(self) -> None:
        self.assertEqual(GLOBAL_TIMELINE_LAYOUT.day_width(620, 31), GLOBAL_TIMELINE_LAYOUT.day_min_width)

    def test_dates_use_available_width_when_wide(self) -> None:
        self.assertEqual(GLOBAL_TIMELINE_LAYOUT.day_width(1240, 31), 40)

    def test_200_percent_layout_keeps_logical_row_density(self) -> None:
        self.assertLess(GLOBAL_TIMELINE_LAYOUT.row_height, 50)
        self.assertEqual(GLOBAL_TIMELINE_LAYOUT.row_height * 2, 76)

    def test_canvas_coordinate_maps_to_month_date(self) -> None:
        self.assertEqual(
            canvas_day_at(34 * 9 + 2, period_start=date(2026, 8, 1), day_width=34, day_count=31),
            date(2026, 8, 10),
        )

    def test_canvas_coordinate_outside_month_is_ignored(self) -> None:
        self.assertIsNone(canvas_day_at(34 * 31, period_start=date(2026, 8, 1), day_width=34, day_count=31))

    def test_wheel_direction_is_stable(self) -> None:
        self.assertEqual(wheel_units(120), -1)
        self.assertEqual(wheel_units(-120), 1)
        self.assertEqual(wheel_units(0), 0)

    def test_short_timeline_segment_omits_inner_label(self) -> None:
        self.assertEqual(timeline_bar_label("跨周末交付", 42), "")

    def test_medium_timeline_segment_uses_ellipsis(self) -> None:
        self.assertEqual(timeline_bar_label("这是一个很长的事项标题", 74), "这是一个很长的…")

    def test_wide_timeline_segment_keeps_full_title(self) -> None:
        self.assertEqual(timeline_bar_label("完成报告", 120), "完成报告")

    def test_global_primary_action_creates_while_compact_opens_detail(self) -> None:
        self.assertEqual(primary_action_for_view("global"), "create")
        self.assertEqual(primary_action_for_view("compact"), "day_detail")

    def test_tooltip_keeps_effective_days_type_and_ddl(self) -> None:
        text = timeline_tooltip_text(_item())
        self.assertIn("跨周末交付", text)
        self.assertIn("2 个有效工作日", text)
        self.assertIn("类型：紧急", text)
        self.assertIn("DDL：2026-08-10", text)

    def test_type_labels_cover_all_task_semantics(self) -> None:
        self.assertEqual(timeline_type_label(_item(task_type="general")), "一般")
        self.assertEqual(timeline_type_label(_item(task_type="urgent")), "紧急")
        self.assertEqual(timeline_type_label(_item(task_type="ddl")), "DDL")

    def test_completed_detail_state(self) -> None:
        self.assertEqual(detail_state_text(_item(completed=True)), "已完成")

    def test_segments_remain_disconnected_for_skipped_days(self) -> None:
        item = _item()
        self.assertEqual(len(item.segments), 2)
        self.assertEqual(item.segments[0].end_date, date(2026, 8, 7))
        self.assertEqual(item.segments[1].start_date, date(2026, 8, 10))


if __name__ == "__main__":
    unittest.main()
