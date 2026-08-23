import tempfile
import unittest
from datetime import date
from pathlib import Path

from calendar_core import Event, RoutineItem, Store
from timeline_model import TimelineSelection, build_month_timeline


class TimelineModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "calendar.json")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def build(self, year: int = 2026, month: int = 8, today: date = date(2026, 8, 20)):
        return build_month_timeline(self.store, year, month, today=today)

    def test_empty_month_has_complete_date_domain(self) -> None:
        model = self.build()
        self.assertEqual(len(model.days), 31)
        self.assertEqual(model.items, ())
        self.assertEqual((model.days[0].date, model.days[-1].date), (date(2026, 8, 1), date(2026, 8, 31)))

    def test_timeline_uses_effective_category_color(self) -> None:
        category = self.store.create_category("绘画", "#8B70D6")
        self.store.events = [
            Event("drawing", "画稿", "2026-08-03T10:00", category_id=category.id, color_mode="inherit")
        ]
        item = self.build().items[0]
        self.assertEqual(item.color, "#8B70D6")
        self.assertEqual(item.category_id, category.id)
        self.assertEqual(item.category_name, "绘画")

    def test_global_category_filter_supports_multiple_and_uncategorized(self) -> None:
        drawing = self.store.create_category("绘画", "#8B70D6")
        video = self.store.create_category("视频", "#52B788")
        self.store.events = [
            Event("drawing", "画稿", "2026-08-03T10:00", category_id=drawing.id, color_mode="inherit"),
            Event("video", "剪辑", "2026-08-04T10:00", category_id=video.id, color_mode="inherit"),
            Event("none", "杂事", "2026-08-05T10:00"),
        ]
        selected = build_month_timeline(
            self.store,
            2026,
            8,
            category_ids={drawing.id, video.id},
            include_uncategorized=False,
        )
        self.assertEqual({item.id for item in selected.items}, {"drawing", "video"})
        with_uncategorized = build_month_timeline(
            self.store,
            2026,
            8,
            category_ids={drawing.id},
            include_uncategorized=True,
        )
        self.assertEqual({item.id for item in with_uncategorized.items}, {"drawing", "none"})

    def test_active_ddl_dates_are_unique_and_ignore_completed_items(self) -> None:
        self.store.events = [
            Event("ddl-one", "DDL 一", "2026-08-06T10:00", event_type="ddl"),
            Event("ddl-two", "DDL 二", "2026-08-06T12:00", event_type="ddl"),
            Event("done", "已完成", "2026-08-07T10:00", event_type="ddl", done=True),
            Event("span", "持续事项", "2026-08-08T10:00", duration_days=3),
        ]
        self.assertEqual(self.build().active_ddl_dates, frozenset({date(2026, 8, 6)}))

    def test_single_general_urgent_and_native_ddl_keep_type_and_color(self) -> None:
        self.store.events = [
            Event("general", "一般", "2026-08-03T10:00", color="#52B788", created_at="1"),
            Event("urgent", "紧急", "2026-08-04T10:00", event_type="urgent", created_at="2"),
            Event("ddl", "截止", "2026-08-05T10:00", event_type="ddl", created_at="3"),
        ]
        items = {item.id: item for item in self.build().items}
        self.assertEqual(items["general"].color, "#52B788")
        self.assertTrue(items["urgent"].is_urgent)
        self.assertIsNone(items["urgent"].ddl_date)
        self.assertTrue(items["ddl"].native_ddl)
        self.assertEqual(items["ddl"].ddl_date, date(2026, 8, 5))

    def test_multi_day_is_one_item_with_effective_duration(self) -> None:
        self.store.events = [Event("span", "连续工作", "2026-08-03T10:00", duration_days=4)]
        item = self.build().items[0]
        self.assertEqual(item.source_task_ids, ("span",))
        self.assertEqual((item.start_date, item.end_date), (date(2026, 8, 3), date(2026, 8, 6)))
        self.assertEqual(item.calendar_span_days, 4)
        self.assertEqual(item.effective_days_count, 4)
        self.assertEqual(len(item.segments), 1)

    def test_end_as_ddl_marks_only_actual_end(self) -> None:
        self.store.events = [Event("span", "末日截止", "2026-08-03T10:00", duration_days=3, end_as_ddl=True)]
        item = self.build().items[0]
        self.assertFalse(item.native_ddl)
        self.assertTrue(item.end_as_ddl)
        self.assertEqual(item.ddl_date, date(2026, 8, 5))

    def test_skip_weekend_creates_separate_segments_and_skipped_dates(self) -> None:
        self.store.events = [Event("work", "工作日", "2026-08-07T10:00", duration_days=3, skip_non_working_days=True)]
        item = self.build().items[0]
        self.assertEqual(item.effective_dates, (date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 11)))
        self.assertEqual(item.skipped_dates, (date(2026, 8, 8), date(2026, 8, 9)))
        self.assertEqual([(s.start_date, s.end_date) for s in item.segments], [(date(2026, 8, 7), date(2026, 8, 7)), (date(2026, 8, 10), date(2026, 8, 11))])
        self.assertEqual((item.calendar_span_days, item.effective_days_count), (5, 3))

    def test_non_workday_start_and_endpoint_ddl_use_last_generated_workday(self) -> None:
        self.store.events = [
            Event(
                "work",
                "周末开始",
                "2026-08-08T10:00",
                duration_days=2,
                skip_non_working_days=True,
                end_as_ddl=True,
            )
        ]
        item = self.build().items[0]
        self.assertEqual(item.effective_dates, (date(2026, 8, 10), date(2026, 8, 11)))
        self.assertEqual(item.ddl_date, date(2026, 8, 11))

    def test_skip_legal_holiday_leave_and_respect_adjusted_workday(self) -> None:
        self.store.set_date_status(date(2026, 10, 9), "leave")
        self.store.events = [Event("work", "节假日", "2026-10-08T10:00", duration_days=3, skip_non_working_days=True)]
        item = build_month_timeline(self.store, 2026, 10, today=date(2026, 8, 20)).items[0]
        self.assertEqual(item.effective_dates, (date(2026, 10, 8), date(2026, 10, 10), date(2026, 10, 12)))
        by_day = {meta.date: meta for meta in build_month_timeline(self.store, 2026, 10).days}
        self.assertTrue(by_day[date(2026, 10, 10)].is_weekend)
        self.assertTrue(by_day[date(2026, 10, 10)].is_adjusted_workday)
        self.assertTrue(by_day[date(2026, 10, 10)].is_workday)
        self.assertTrue(by_day[date(2026, 10, 9)].is_user_leave)

    def test_official_holiday_metadata_and_non_current_month_today(self) -> None:
        model = build_month_timeline(self.store, 2026, 10, today=date(2026, 8, 20))
        by_day = {meta.date: meta for meta in model.days}
        self.assertTrue(by_day[date(2026, 10, 2)].is_legal_holiday)
        self.assertEqual(by_day[date(2026, 10, 2)].holiday_name, "国庆节假期")
        self.assertFalse(any(meta.is_today for meta in model.days))

    def test_user_holiday_metadata_is_independent_from_system_holiday(self) -> None:
        self.store.set_date_status(date(2026, 8, 13), "holiday")
        meta = {item.date: item for item in self.build().days}[date(2026, 8, 13)]
        self.assertTrue(meta.is_user_holiday)
        self.assertFalse(meta.is_workday)

    def test_today_marker_appears_only_inside_period(self) -> None:
        model = self.build(today=date(2026, 8, 20))
        self.assertEqual([meta.date for meta in model.days if meta.is_today], [date(2026, 8, 20)])

    def test_same_title_events_never_merge_and_legacy_ids_stay_separate(self) -> None:
        self.store.events = [
            Event("first", "版权监修", "2026-08-03T10:00", duration_days=2),
            Event("second", "版权监修", "2026-08-10T10:00", duration_days=2),
        ]
        self.assertEqual([item.id for item in self.build().items], ["first", "second"])

    def test_month_clipping_and_continuation_flags(self) -> None:
        self.store.events = [
            Event("before", "上月开始", "2026-07-28T10:00", duration_days=9),
            Event("after", "下月结束", "2026-08-28T10:00", duration_days=8),
            Event("whole", "跨整月", "2026-07-20T10:00", duration_days=53),
        ]
        items = {item.id: item for item in self.build().items}
        self.assertEqual((items["before"].visible_start, items["before"].visible_end), (date(2026, 8, 1), date(2026, 8, 5)))
        self.assertTrue(items["before"].continues_from_previous_period)
        self.assertTrue(items["after"].continues_to_next_period)
        self.assertTrue(items["whole"].continues_from_previous_period)
        self.assertTrue(items["whole"].continues_to_next_period)

    def test_month_lengths_include_leap_year(self) -> None:
        self.assertEqual(len(build_month_timeline(self.store, 2027, 2).days), 28)
        self.assertEqual(len(build_month_timeline(self.store, 2028, 2).days), 29)
        self.assertEqual(len(build_month_timeline(self.store, 2026, 4).days), 30)
        self.assertEqual(len(self.build().days), 31)

    def test_ddl_outside_visible_period_is_retained_as_metadata(self) -> None:
        self.store.events = [Event("deadline", "跨月截止", "2026-08-30T10:00", duration_days=4, end_as_ddl=True)]
        item = self.build().items[0]
        self.assertEqual(item.ddl_date, date(2026, 9, 2))
        self.assertTrue(item.continues_to_next_period)

    def test_completed_sort_after_unfinished_and_sort_is_deterministic(self) -> None:
        self.store.events = [
            Event("done", "完成", "2026-08-01T10:00", done=True, created_at="1"),
            Event("general", "一般", "2026-08-02T10:00", created_at="4"),
            Event("urgent", "紧急", "2026-08-02T10:00", event_type="urgent", created_at="3"),
            Event("ddl", "DDL", "2026-08-02T10:00", event_type="ddl", created_at="2"),
        ]
        self.assertEqual([item.id for item in self.build().items], ["ddl", "urgent", "general", "done"])

    def test_rebuild_reflects_edit_delete_and_completion(self) -> None:
        event = Event("changing", "变更", "2026-08-03T10:00", duration_days=2)
        self.store.events = [event]
        self.assertEqual(self.build().items[0].end_date, date(2026, 8, 4))
        event.duration_days = 4
        self.assertEqual(self.build().items[0].end_date, date(2026, 8, 6))
        event.done = True
        self.assertTrue(self.build().items[0].completed)
        self.store.events.clear()
        self.assertEqual(self.build().items, ())

    def test_empty_effective_dates_are_ignored_safely(self) -> None:
        self.store.events = [Event("invalid", "异常", "2026-08-03T10:00")]
        self.store.event_dates = lambda _event: ()
        self.assertEqual(self.build().items, ())

    def test_quick_add_and_ddl_list_share_the_same_event_facts(self) -> None:
        quick = self.store.create_quick("快速事项", date(2026, 8, 6), event_type="general")
        ddl = Event("ddl", "截止事项", "2026-08-08T10:00", event_type="ddl")
        self.store.upsert(ddl)
        model_ids = {item.id for item in self.build().items}
        self.assertIn(quick.id, model_ids)
        self.assertIn(ddl.id, model_ids)
        self.assertEqual([event.id for event in self.store.ddl_events()], [ddl.id])

    def test_save_load_preserves_canonical_event_id_and_timeline_shape(self) -> None:
        event = Event("stable-id", "稳定系列", "2026-08-07T10:00", duration_days=3, skip_non_working_days=True)
        self.store.upsert(event)
        loaded = Store(self.store.data_file)
        item = build_month_timeline(loaded, 2026, 8).items[0]
        self.assertEqual(item.id, "stable-id")
        self.assertEqual(item.source_task_ids, ("stable-id",))
        self.assertEqual(item.effective_dates, (date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 11)))

    def test_habits_do_not_enter_timeline(self) -> None:
        self.store.routines = [RoutineItem("habit", "喝水")]
        self.assertEqual(self.build().items, ())

    def test_selection_uses_stable_item_id(self) -> None:
        self.store.events = [Event("task", "选择", "2026-08-03T10:00")]
        model = self.build()
        selection = TimelineSelection()
        self.assertEqual(selection.select("task", model).id, "task")
        self.assertEqual(selection.get(model).id, "task")
        self.assertIsNone(selection.select("missing", model))
        selection.clear()
        self.assertIsNone(selection.get(model))


if __name__ == "__main__":
    unittest.main()
