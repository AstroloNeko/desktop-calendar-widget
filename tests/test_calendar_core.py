import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from calendar_core import COLORS, Event, RoutineItem, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_file = Path(self.temp_dir.name) / "calendar.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bad_event_does_not_hide_good_events(self):
        self.data_file.write_text(
            json.dumps(
                {
                    "events": [
                        {"id": "good", "title": "有效日程", "due": "2026-08-04T10:00"},
                        {"id": "bad", "title": "损坏日程", "due": "not-a-date"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = Store(self.data_file)
        self.assertEqual([event.id for event in store.events], ["good"])
        self.assertEqual(store.event_by_id("good").title, "有效日程")
        self.assertIsNone(store.event_by_id("missing"))

    def test_old_topmost_setting_migrates_to_desktop_mode(self):
        self.data_file.write_text(json.dumps({"events": [], "settings": {"topmost": True}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["window_mode"], "desktop")

    def test_new_install_defaults_to_crisp_opacity(self):
        store = Store(self.data_file)
        self.assertEqual(store.settings["opacity"], 1.0)

    def test_new_install_defaults_to_modern_theme(self):
        store = Store(self.data_file)
        self.assertEqual(store.settings["theme"], "modern")

    def test_new_and_legacy_settings_default_to_compact_view_without_geometry(self):
        store = Store(self.data_file)
        self.assertEqual(store.settings["view_mode"], "compact")
        self.assertIsNone(store.settings["compact_geometry"])
        self.assertIsNone(store.settings["global_geometry"])
        self.data_file.write_text(json.dumps({"events": [], "settings": {"x": 120, "y": 80}}), encoding="utf-8")
        legacy = Store(self.data_file)
        self.assertEqual(legacy.settings["view_mode"], "compact")
        self.assertEqual((legacy.settings["x"], legacy.settings["y"]), (120, 80))

    def test_legacy_settings_without_theme_use_modern(self):
        self.data_file.write_text(json.dumps({"settings": {"agenda_open": False}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["theme"], "modern")
        self.assertFalse(store.settings["agenda_open"])

    def test_theme_round_trips(self):
        store = Store(self.data_file)
        for theme_name in ("modern", "aero", "paper", "frutiger"):
            store.settings["theme"] = theme_name
            store.save()
            loaded = Store(self.data_file)
            self.assertEqual(loaded.settings["theme"], theme_name)

    def test_legacy_aero_theme_name_is_preserved(self):
        self.data_file.write_text(json.dumps({"settings": {"theme": "aero"}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["theme"], "aero")

    def test_v06_win7_aero_theme_name_is_migrated(self):
        self.data_file.write_text(json.dumps({"settings": {"theme": "win7_aero"}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["theme"], "aero")

    def test_frutiger_theme_persistence_does_not_change_events(self):
        store = Store(self.data_file)
        event = Event("theme-safe", "主题切换不改事项", "2026-08-10T09:30", event_type="ddl")
        store.upsert(event)
        store.settings["theme"] = "frutiger"
        store.save()

        loaded = Store(self.data_file)
        self.assertEqual(loaded.settings["theme"], "frutiger")
        self.assertEqual([item.id for item in loaded.events], ["theme-safe"])
        self.assertEqual(loaded.events[0].event_type, "ddl")

    def test_invalid_theme_falls_back_to_modern(self):
        self.data_file.write_text(json.dumps({"settings": {"theme": "neon_hud"}}), encoding="utf-8")
        store = Store(self.data_file)
        self.assertEqual(store.settings["theme"], "modern")

    def test_legacy_hex_color_is_preserved(self):
        event = Event("legacy", "旧版颜色", "2026-08-04T10:00", color="#F05252")
        self.assertEqual(event.color, "#F05252")

    def test_non_schedule_edit_keeps_notification_history(self):
        store = Store(self.data_file)
        original = Event("one", "旧标题", "2026-08-04T10:00")
        store.upsert(original)
        store.notified.add("one:reminder:2026-08-04T09:00")
        edited = Event("one", "新标题", "2026-08-04T10:00", color=COLORS["薄荷绿"])
        store.upsert(edited)
        self.assertIn("one:reminder:2026-08-04T09:00", store.notified)

    def test_schedule_edit_clears_notification_history(self):
        store = Store(self.data_file)
        store.upsert(Event("one", "事项", "2026-08-04T10:00"))
        store.notified.add("one:reminder:2026-08-04T09:00")
        store.upsert(Event("one", "事项", "2026-08-04T11:00"))
        self.assertFalse(any(key.startswith("one:") for key in store.notified))

    def test_quick_event_uses_optional_time(self):
        store = Store(self.data_file)
        event = store.create_quick("快速事项", date.today())
        self.assertEqual(event.due_date, date.today())
        self.assertFalse(event.has_time)
        self.assertIsNone(event.reminder)
        self.assertEqual(event.event_type, "general")
        loaded = Store(self.data_file)
        self.assertFalse(loaded.events[0].has_time)

    def test_quick_event_saves_selected_color_and_type(self):
        store = Store(self.data_file)
        event = store.create_quick(
            "紧急快速事项",
            date(2026, 8, 5),
            color=COLORS["珊瑚红"],
            event_type="urgent",
        )
        self.assertEqual(event.color, COLORS["珊瑚红"])
        self.assertEqual(event.event_type, "urgent")
        loaded = Store(self.data_file)
        self.assertEqual(loaded.events[0].event_type, "urgent")

    def test_legacy_event_defaults_to_having_a_time(self):
        self.data_file.write_text(
            json.dumps({"events": [{"id": "legacy", "title": "旧日程", "due": "2026-08-05T18:00"}]}),
            encoding="utf-8",
        )
        store = Store(self.data_file)
        self.assertTrue(store.events[0].has_time)
        self.assertEqual(store.events[0].duration_days, 1)
        self.assertFalse(store.events[0].skip_non_working_days)
        self.assertEqual(store.events[0].event_type, "general")

    def test_untimed_event_cannot_keep_a_relative_reminder(self):
        event = Event("untimed", "无具体时间", "2026-08-05T23:59", has_time=False, reminder=60)
        self.assertIsNone(event.reminder)

    def test_multiday_event_appears_on_every_covered_day(self):
        store = Store(self.data_file)
        event = Event("span", "连续任务", "2026-08-05T23:59", has_time=False, duration_days=3)
        store.upsert(event)
        self.assertEqual([item.id for item in store.events_on(date(2026, 8, 5))], ["span"])
        self.assertEqual([item.id for item in store.events_on(date(2026, 8, 6))], ["span"])
        self.assertEqual([item.id for item in store.events_on(date(2026, 8, 7))], ["span"])
        self.assertEqual(store.events_on(date(2026, 8, 8)), [])
        self.assertEqual(event.end_date, date(2026, 8, 7))
        self.assertEqual(event.day_number(date(2026, 8, 6)), 2)

    def test_multiday_event_is_overdue_only_after_last_day(self):
        now = datetime.now()
        start = (now - timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        event = Event("active", "进行中的任务", start.isoformat(timespec="minutes"), has_time=False, duration_days=3)
        self.assertFalse(event.is_overdue)

    def test_multiday_duration_round_trips(self):
        store = Store(self.data_file)
        store.upsert(Event("span", "三天事项", "2026-08-05T10:00", duration_days=3))
        loaded = Store(self.data_file)
        self.assertEqual(loaded.events[0].duration_days, 3)

    def test_workday_duration_skips_weekend(self):
        store = Store(self.data_file)
        event = Event(
            "workdays",
            "两个工作日",
            "2026-08-07T23:59",
            has_time=False,
            duration_days=2,
            skip_non_working_days=True,
        )
        store.events = [event]
        self.assertEqual(store.event_dates(event), (date(2026, 8, 7), date(2026, 8, 10)))
        self.assertEqual(store.events_on(date(2026, 8, 8)), [])

    def test_workday_duration_skips_official_holiday(self):
        store = Store(self.data_file)
        event = Event(
            "holiday",
            "跨中秋",
            "2026-09-24T18:00",
            duration_days=2,
            skip_non_working_days=True,
        )
        self.assertEqual(store.event_dates(event), (date(2026, 9, 24), date(2026, 9, 28)))

    def test_adjusted_weekend_workday_counts(self):
        store = Store(self.data_file)
        event = Event(
            "makeup",
            "调休工作日",
            "2026-09-20T18:00",
            duration_days=2,
            skip_non_working_days=True,
        )
        self.assertEqual(store.event_dates(event), (date(2026, 9, 20), date(2026, 9, 21)))

    def test_non_working_start_moves_to_next_workday(self):
        store = Store(self.data_file)
        event = Event(
            "weekend-start",
            "周末开始",
            "2026-08-08T18:00",
            duration_days=1,
            skip_non_working_days=True,
        )
        self.assertEqual(store.event_dates(event), (date(2026, 8, 10),))

    def test_manual_leave_and_holiday_are_skipped(self):
        store = Store(self.data_file)
        store.set_date_status(date(2026, 8, 10), "leave")
        store.set_date_status(date(2026, 8, 11), "holiday")
        event = Event(
            "manual-days-off",
            "自定义休息日",
            "2026-08-10T18:00",
            duration_days=2,
            skip_non_working_days=True,
        )
        self.assertEqual(store.event_dates(event), (date(2026, 8, 12), date(2026, 8, 13)))

    def test_restoring_normal_uses_official_calendar_again(self):
        store = Store(self.data_file)
        adjusted_sunday = date(2026, 9, 20)
        store.set_date_status(adjusted_sunday, "leave")
        self.assertFalse(store.is_workday(adjusted_sunday))
        store.set_date_status(adjusted_sunday, "normal")
        self.assertTrue(store.is_workday(adjusted_sunday))
        self.assertEqual(store.date_status(adjusted_sunday), "normal")
        official_holiday = date(2026, 9, 25)
        store.set_date_status(official_holiday, "leave")
        store.set_date_status(official_holiday, "normal")
        self.assertFalse(store.is_workday(official_holiday))

    def test_date_status_round_trips_and_invalid_values_are_ignored(self):
        self.data_file.write_text(
            json.dumps(
                {
                    "date_states": {
                        "2026-08-10": "leave",
                        "2026-08-11": "holiday",
                        "2026-08-12": "vacation",
                        "not-a-date": "leave",
                    }
                }
            ),
            encoding="utf-8",
        )
        store = Store(self.data_file)
        self.assertEqual(store.date_status(date(2026, 8, 10)), "leave")
        self.assertEqual(store.date_status(date(2026, 8, 11)), "holiday")
        self.assertEqual(store.date_status(date(2026, 8, 12)), "normal")
        store.save()
        loaded = Store(self.data_file)
        self.assertEqual(loaded.date_states, {"2026-08-10": "leave", "2026-08-11": "holiday"})

    def test_legacy_priorities_are_mapped_to_event_types(self):
        self.data_file.write_text(
            json.dumps(
                {
                    "events": [
                        {"id": "low-key", "title": "旧 low", "due": "2026-08-05T10:00", "priority": "low"},
                        {"id": "normal-key", "title": "旧 normal", "due": "2026-08-05T10:00", "priority": "normal"},
                        {"id": "urgent-key", "title": "旧 urgent", "due": "2026-08-05T10:00", "priority": "urgent"},
                        {"id": "low", "title": "低", "due": "2026-08-05T10:00", "priority": "低"},
                        {"id": "normal", "title": "普通", "due": "2026-08-05T10:00", "priority": "普通"},
                        {"id": "high", "title": "高", "due": "2026-08-05T10:00", "priority": "高"},
                        {"id": "urgent", "title": "紧急", "due": "2026-08-05T10:00", "priority": "紧急"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = Store(self.data_file)
        self.assertEqual(
            [item.event_type for item in store.events],
            ["general", "general", "urgent", "general", "general", "urgent", "urgent"],
        )
        store.save()
        saved_events = json.loads(self.data_file.read_text(encoding="utf-8"))["events"]
        self.assertTrue(all("event_type" in item and "priority" not in item for item in saved_events))

    def test_invalid_event_type_falls_back_to_general(self):
        self.assertEqual(Event("invalid", "未知", "2026-08-05T10:00", event_type="critical").event_type, "general")
        self.assertEqual(Event("missing", "缺省", "2026-08-05T10:00", event_type=None).event_type, "general")

    def test_legacy_ddl_flags_converge_to_ddl_type(self):
        event = Event.from_dict(
            {"id": "legacy-ddl", "title": "旧 DDL", "due": "2026-08-05T10:00", "is_ddl": True, "priority": "normal"}
        )
        self.assertEqual(event.event_type, "ddl")

    def test_invalid_skip_non_working_days_falls_back_to_false(self):
        self.data_file.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "invalid-skip",
                            "title": "错误跳过值",
                            "due": "2026-08-05T10:00",
                            "skip_non_working_days": "yes",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(Store(self.data_file).events[0].skip_non_working_days)

    def test_events_on_places_unfinished_before_finished(self):
        store = Store(self.data_file)
        day = date(2026, 8, 4)
        finished = Event("done", "已完成", "2026-08-04T08:00", done=True)
        open_event = Event("open", "未完成", "2026-08-04T18:00")
        store.events = [finished, open_event]
        self.assertEqual([event.id for event in store.events_on(day)], ["open", "done"])

    def test_events_on_sorts_ddl_urgent_general_stably_and_done_last(self):
        store = Store(self.data_file)
        day = date(2026, 8, 5)
        store.events = [
            Event("general", "一般", "2026-08-05T10:00", event_type="general"),
            Event("urgent-one", "紧急一", "2026-08-05T12:00", event_type="urgent"),
            Event("ddl-one", "DDL 一", "2026-08-05T08:00", event_type="ddl"),
            Event("urgent-two", "紧急二", "2026-08-05T09:00", event_type="urgent"),
            Event("ddl-two", "DDL 二", "2026-08-05T11:00", event_type="ddl"),
            Event("done", "完成", "2026-08-05T07:00", event_type="ddl", done=True),
        ]
        self.assertEqual(
            [event.id for event in store.events_on(day)],
            ["ddl-one", "ddl-two", "urgent-one", "urgent-two", "general", "done"],
        )

    def test_ddl_date_ignores_completed_events(self):
        store = Store(self.data_file)
        day = date(2026, 8, 5)
        ddl = Event("ddl", "DDL", "2026-08-05T10:00", event_type="ddl")
        store.events = [ddl]
        self.assertTrue(store.has_ddl_on(day))
        ddl.done = True
        self.assertFalse(store.has_ddl_on(day))

    def test_ddl_events_put_overdue_first_and_exclude_completed_and_urgent(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 5, 12, 0)
        store.events = [
            Event("soon", "即将", "2026-08-06T10:00", event_type="ddl"),
            Event("late", "逾期", "2026-08-04T10:00", event_type="ddl"),
            Event("later", "稍后", "2026-08-08T10:00", event_type="ddl"),
            Event("done", "完成", "2026-08-03T10:00", event_type="ddl", done=True),
            Event("urgent", "紧急但非 DDL", "2026-08-03T10:00", event_type="urgent"),
            Event("general", "一般", "2026-08-03T10:00", event_type="general"),
        ]
        self.assertEqual([item.id for item in store.ddl_events(now)], ["late", "soon", "later"])

    def test_ddl_events_split_into_pinned_24_hours_and_regular_without_duplicates(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 5, 12, 0)
        store.events = [
            Event("regular", "超过窗口", "2026-08-06T12:01", event_type="ddl"),
            Event("boundary", "正好二十四小时", "2026-08-06T12:00", event_type="ddl"),
            Event("late", "已经逾期", "2026-08-04T10:00", event_type="ddl"),
            Event("soon", "即将截止", "2026-08-06T11:59", event_type="ddl"),
            Event("done", "已经完成", "2026-08-04T09:00", event_type="ddl", done=True),
            Event("urgent", "紧急事项", "2026-08-04T08:00", event_type="urgent"),
        ]
        pinned, regular = store.grouped_ddl_events(now)
        self.assertEqual([item.id for item in pinned], ["late", "soon", "boundary"])
        self.assertEqual([item.id for item in regular], ["regular"])
        self.assertFalse({item.id for item in pinned} & {item.id for item in regular})

    def test_complete_ddl_groups_collect_native_and_endpoint_deadlines_once(self):
        store = Store(self.data_file)
        native = Event("native", "原生 DDL", "2026-08-08T10:00", event_type="ddl")
        general_end = Event(
            "general-end",
            "一般末日 DDL",
            "2026-08-08T10:00",
            event_type="general",
            duration_days=2,
            end_as_ddl=True,
        )
        urgent_end = Event(
            "urgent-end",
            "紧急末日 DDL",
            "2026-08-08T11:00",
            event_type="urgent",
            duration_days=2,
            end_as_ddl=True,
        )
        store.events = [
            Event("general", "一般事项", "2026-08-08T08:00", event_type="general"),
            Event("urgent", "紧急事项", "2026-08-08T09:00", event_type="urgent"),
            native,
            general_end,
            urgent_end,
            Event("native", "重复 ID", "2026-08-09T10:00", event_type="ddl"),
        ]
        groups = store.complete_ddl_groups(datetime(2026, 8, 7, 0, 0))
        all_ids = [item.id for item in groups.overdue + groups.due_soon + groups.future + groups.completed]
        self.assertEqual(all_ids, ["native", "general-end", "urgent-end"])
        self.assertEqual(all_ids.count("native"), 1)
        self.assertEqual(general_end.event_type, "general")
        self.assertEqual(urgent_end.event_type, "urgent")

    def test_complete_ddl_groups_use_actual_last_effective_date(self):
        store = Store(self.data_file)
        endpoint = Event(
            "endpoint",
            "跨周末末日",
            "2026-08-07T10:00",
            duration_days=2,
            skip_non_working_days=True,
            end_as_ddl=True,
        )
        store.events = [endpoint]
        groups = store.complete_ddl_groups(datetime(2026, 8, 8, 9, 0))
        self.assertEqual(store.event_end_date(endpoint), date(2026, 8, 10))
        self.assertEqual([item.id for item in groups.future], ["endpoint"])
        self.assertFalse(store.has_ddl_on(date(2026, 8, 7)))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 10)))

    def test_complete_ddl_groups_classify_overdue_due_soon_future_and_completed(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 7, 12, 0)
        store.events = [
            Event("overdue", "逾期", "2026-08-07T11:59", event_type="ddl"),
            Event("soon", "24 小时内", "2026-08-08T12:00", event_type="ddl"),
            Event("future", "未来", "2026-08-08T12:01", event_type="ddl"),
            Event("done", "已完成", "2026-08-06T10:00", event_type="ddl", done=True),
        ]
        groups = store.complete_ddl_groups(now)
        self.assertEqual([item.id for item in groups.overdue], ["overdue"])
        self.assertEqual([item.id for item in groups.due_soon], ["soon"])
        self.assertEqual([item.id for item in groups.future], ["future"])
        self.assertEqual([item.id for item in groups.completed], ["done"])
        self.assertEqual(groups.total, 4)

    def test_complete_ddl_groups_sort_each_section_stably(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 7, 12, 0)
        store.events = [
            Event("future-later", "未来稍后", "2026-08-10T10:00", event_type="ddl"),
            Event("future-first", "未来较近", "2026-08-09T10:00", event_type="ddl"),
            Event("same-one", "同时间一", "2026-08-08T10:00", event_type="ddl"),
            Event("same-two", "同时间二", "2026-08-08T10:00", event_type="ddl"),
            Event("done-old", "早期完成", "2026-08-01T10:00", event_type="ddl", done=True),
            Event("done-recent", "近期完成", "2026-08-06T10:00", event_type="ddl", done=True),
        ]
        groups = store.complete_ddl_groups(now)
        self.assertEqual([item.id for item in groups.due_soon], ["same-one", "same-two"])
        self.assertEqual([item.id for item in groups.future], ["future-first", "future-later"])
        self.assertEqual([item.id for item in groups.completed], ["done-recent", "done-old"])

    def test_complete_ddl_groups_refresh_after_completion_retype_and_delete(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 7, 12, 0)
        item = Event("changing", "变化事项", "2026-08-08T10:00", event_type="ddl")
        store.events = [item]
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).due_soon], ["changing"])

        item.done = True
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).completed], ["changing"])
        item.done = False
        item.event_type = "general"
        self.assertEqual(store.complete_ddl_groups(now).total, 0)
        item.event_type = "ddl"
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).due_soon], ["changing"])
        store.delete(item.id)
        self.assertEqual(store.complete_ddl_groups(now).total, 0)

    def test_complete_ddl_groups_refresh_after_date_change_or_endpoint_cancel(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 7, 12, 0)
        store.upsert(Event("move", "移动末日", "2026-08-07T10:00", duration_days=2, end_as_ddl=True))
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).due_soon], ["move"])
        store.upsert(Event("anchor", "排序锚点", "2026-08-11T10:00", event_type="ddl"))
        store.upsert(Event("move", "移动末日", "2026-08-11T10:00", duration_days=2, end_as_ddl=True))
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).future], ["anchor", "move"])
        store.upsert(Event("move", "移动末日", "2026-08-08T10:00", duration_days=2, end_as_ddl=True))
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).future], ["move", "anchor"])
        store.upsert(Event("move", "取消末日", "2026-08-08T10:00", duration_days=2, end_as_ddl=False))
        self.assertEqual([entry.id for entry in store.complete_ddl_groups(now).future], ["anchor"])

    def test_untimed_ddl_deadline_uses_end_of_day_boundary(self):
        store = Store(self.data_file)
        item = Event("untimed", "无具体时间", "2026-08-06T23:59", event_type="ddl", has_time=False)
        store.events = [item]
        pinned, regular = store.grouped_ddl_events(datetime(2026, 8, 5, 23, 59))
        self.assertEqual([event.id for event in pinned], ["untimed"])
        self.assertEqual(regular, [])

    def test_ddl_leaves_pinned_area_when_completed_or_retyped(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 5, 12, 0)
        item = Event("ddl", "即将截止", "2026-08-05T13:00", event_type="ddl")
        store.events = [item]
        self.assertEqual([event.id for event in store.grouped_ddl_events(now)[0]], ["ddl"])
        item.event_type = "urgent"
        self.assertEqual(store.grouped_ddl_events(now), ([], []))
        item.event_type = "ddl"
        item.done = True
        self.assertEqual(store.grouped_ddl_events(now), ([], []))

    def test_multiday_event_does_not_create_deadline_by_default(self):
        store = Store(self.data_file)
        item = Event("span", "普通多日事项", "2026-08-05T10:00", duration_days=3)
        store.events = [item]
        self.assertFalse(store.has_ddl_on(date(2026, 8, 7)))
        self.assertEqual(store.ddl_events(datetime(2026, 8, 5, 9, 0)), [])

    def test_end_as_ddl_round_trips_and_legacy_events_default_to_false(self):
        store = Store(self.data_file)
        store.upsert(Event("endpoint", "末日截止", "2026-08-05T10:00", duration_days=3, end_as_ddl=True))
        self.assertTrue(Store(self.data_file).events[0].end_as_ddl)

        self.data_file.write_text(
            json.dumps({"events": [{"id": "legacy", "title": "旧事项", "due": "2026-08-05T10:00", "duration_days": 3}]}),
            encoding="utf-8",
        )
        self.assertFalse(Store(self.data_file).events[0].end_as_ddl)

    def test_end_as_ddl_marks_only_the_actual_last_day(self):
        store = Store(self.data_file)
        item = Event("span", "末日截止", "2026-08-05T10:00", duration_days=3, end_as_ddl=True)
        store.events = [item]
        self.assertFalse(store.has_ddl_on(date(2026, 8, 5)))
        self.assertFalse(store.has_ddl_on(date(2026, 8, 6)))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 7)))
        self.assertEqual([entry.id for entry in store.ddl_events(datetime(2026, 8, 5, 9, 0))], ["span"])

    def test_end_as_ddl_enters_regular_deadline_list(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 5, 9, 0)
        item = Event("regular", "普通 DDL", "2026-08-05T10:00", duration_days=3, end_as_ddl=True)
        store.events = [item]
        pinned, regular = store.grouped_ddl_events(now)
        self.assertEqual(pinned, [])
        self.assertEqual([entry.id for entry in regular], ["regular"])

    def test_end_as_ddl_enters_pinned_list_within_24_hours(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 5, 12, 0)
        item = Event("soon", "即将截止", "2026-08-05T10:00", duration_days=2, end_as_ddl=True)
        store.events = [item]
        pinned, regular = store.grouped_ddl_events(now)
        self.assertEqual([entry.id for entry in pinned], ["soon"])
        self.assertEqual(regular, [])

    def test_overdue_end_as_ddl_is_pinned_first(self):
        store = Store(self.data_file)
        now = datetime(2026, 8, 8, 12, 0)
        overdue = Event("late", "已经逾期", "2026-08-05T10:00", duration_days=2, end_as_ddl=True)
        later = Event("later", "以后截止", "2026-08-08T13:00", duration_days=2, end_as_ddl=True)
        store.events = [later, overdue]
        pinned, regular = store.grouped_ddl_events(now)
        self.assertEqual([entry.id for entry in pinned], ["late"])
        self.assertEqual([entry.id for entry in regular], ["later"])

    def test_deadline_groups_never_duplicate_end_as_ddl_event(self):
        store = Store(self.data_file)
        item = Event("once", "只出现一次", "2026-08-05T10:00", duration_days=2, end_as_ddl=True)
        store.events = [item]
        pinned, regular = store.grouped_ddl_events(datetime(2026, 8, 5, 12, 0))
        self.assertEqual([entry.id for entry in pinned + regular].count("once"), 1)
        self.assertFalse({entry.id for entry in pinned} & {entry.id for entry in regular})

    def test_general_and_urgent_keep_their_type_with_end_deadline(self):
        store = Store(self.data_file)
        general = Event("general-end", "一般末日", "2026-08-05T10:00", duration_days=2, event_type="general", end_as_ddl=True)
        urgent = Event("urgent-end", "紧急末日", "2026-08-05T11:00", duration_days=2, event_type="urgent", end_as_ddl=True)
        store.events = [general, urgent]
        self.assertEqual(general.event_type, "general")
        self.assertEqual(urgent.event_type, "urgent")
        self.assertEqual({entry.id for entry in store.ddl_events(datetime(2026, 8, 5, 9, 0))}, {"general-end", "urgent-end"})

    def test_ddl_type_uses_one_end_deadline_without_extra_flag(self):
        store = Store(self.data_file)
        item = Event("ddl", "原生 DDL", "2026-08-05T10:00", duration_days=3, event_type="ddl", end_as_ddl=True)
        store.events = [item]
        self.assertFalse(item.end_as_ddl)
        self.assertFalse(store.has_ddl_on(date(2026, 8, 5)))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 7)))
        self.assertEqual([entry.id for entry in store.ddl_events()], ["ddl"])

    def test_workday_end_deadline_uses_last_generated_weekday(self):
        store = Store(self.data_file)
        item = Event(
            "weekend",
            "跨周末",
            "2026-08-07T10:00",
            duration_days=2,
            skip_non_working_days=True,
            end_as_ddl=True,
        )
        store.events = [item]
        self.assertFalse(store.has_ddl_on(date(2026, 8, 8)))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 10)))

    def test_workday_end_deadline_uses_last_generated_day_after_holiday(self):
        store = Store(self.data_file)
        item = Event(
            "holiday",
            "跨节假日",
            "2026-09-24T10:00",
            duration_days=2,
            skip_non_working_days=True,
            end_as_ddl=True,
        )
        store.events = [item]
        self.assertTrue(store.has_ddl_on(date(2026, 9, 28)))
        self.assertFalse(store.has_ddl_on(date(2026, 9, 25)))

    def test_changing_duration_moves_end_deadline(self):
        store = Store(self.data_file)
        store.upsert(Event("move", "修改持续时间", "2026-08-05T10:00", duration_days=2, end_as_ddl=True))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 6)))
        store.upsert(Event("move", "修改持续时间", "2026-08-05T10:00", duration_days=4, end_as_ddl=True))
        self.assertFalse(store.has_ddl_on(date(2026, 8, 6)))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 8)))

    def test_deleting_event_removes_end_deadline(self):
        store = Store(self.data_file)
        store.upsert(Event("delete", "删除末日", "2026-08-05T10:00", duration_days=2, end_as_ddl=True))
        self.assertTrue(store.has_ddl_on(date(2026, 8, 6)))
        store.delete("delete")
        self.assertFalse(store.has_ddl_on(date(2026, 8, 6)))
        self.assertEqual(store.ddl_events(), [])

    def test_upcoming_includes_overdue_and_next_week(self):
        store = Store(self.data_file)
        now = datetime.now().replace(second=0, microsecond=0)
        store.events = [
            Event("late", "逾期", (now - timedelta(hours=1)).isoformat(timespec="minutes")),
            Event("soon", "即将到期", (now + timedelta(days=2)).isoformat(timespec="minutes")),
            Event("later", "以后", (now + timedelta(days=10)).isoformat(timespec="minutes")),
        ]
        self.assertEqual([event.id for event in store.upcoming(7)], ["late", "soon"])

    def test_habit_completion_is_scoped_to_one_day(self):
        store = Store(self.data_file)
        item = RoutineItem("habit", "读书", kind="habit", created_on="2026-08-03")
        store.upsert_routine(item)
        store.toggle_routine(item, date(2026, 8, 4))
        self.assertTrue(item.is_done_on(date(2026, 8, 4)))
        self.assertFalse(item.is_done_on(date(2026, 8, 5)))
        self.assertEqual([entry.id for entry in store.routines_on(date(2026, 8, 5))], ["habit"])

    def test_agenda_items_move_completed_routine_to_bottom_and_restore(self):
        store = Store(self.data_file)
        day = date(2026, 8, 5)
        habit = RoutineItem("habit", "读书", kind="habit", created_on="2026-08-03")
        second_habit = RoutineItem("second", "拉伸", kind="habit", created_on="2026-08-03")
        general = Event("general", "一般", "2026-08-05T10:00", event_type="general")
        store.events = [general]
        store.routines = [habit, second_habit]
        self.assertEqual([item.id for item in store.agenda_items_on(day)], ["general", "second", "habit"])
        store.toggle_routine(second_habit, day)
        self.assertEqual([item.id for item in store.agenda_items_on(day)], ["general", "habit", "second"])
        store.toggle_routine(second_habit, day)
        self.assertEqual([item.id for item in store.agenda_items_on(day)], ["general", "second", "habit"])

    def test_todo_completion_hides_it_after_completion_day(self):
        store = Store(self.data_file)
        item = RoutineItem("todo", "交材料", kind="todo", created_on="2026-08-03")
        store.upsert_routine(item)
        store.toggle_routine(item, date(2026, 8, 4))
        self.assertTrue(item.is_done_on(date(2026, 8, 4)))
        self.assertEqual(store.routines_on(date(2026, 8, 5)), [])

    def test_routines_round_trip(self):
        store = Store(self.data_file)
        item = RoutineItem("habit", "拉伸", kind="habit", color=COLORS["薄荷绿"], created_on="2026-08-03")
        store.upsert_routine(item)
        store.toggle_routine(item, date(2026, 8, 4))
        loaded = Store(self.data_file)
        self.assertEqual(len(loaded.routines), 1)
        self.assertTrue(loaded.routines[0].is_done_on(date(2026, 8, 4)))

    def test_legacy_routine_defaults_to_no_reminder(self):
        self.data_file.write_text(
            json.dumps({"routines": [{"id": "legacy", "title": "旧习惯", "created_on": "2026-08-03"}]}),
            encoding="utf-8",
        )
        item = Store(self.data_file).routines[0]
        self.assertFalse(item.reminder_enabled)
        self.assertIsNone(item.reminder_time)

    def test_invalid_routine_reminder_time_is_safely_disabled(self):
        item = RoutineItem.from_dict(
            {
                "id": "invalid-time",
                "title": "无效提醒",
                "created_on": "2026-08-03",
                "reminder_enabled": True,
                "reminder_time": "25:99",
            }
        )
        self.assertFalse(item.reminder_enabled)
        self.assertIsNone(item.reminder_time)

    def test_disabled_routine_reminder_is_not_due(self):
        store = Store(self.data_file)
        store.routines = [RoutineItem("off", "不提醒", created_on="2026-08-03", reminder_time="09:00")]
        self.assertEqual(store.due_routine_reminders(datetime(2026, 8, 5, 10, 0)), [])

    def test_enabled_routine_reminder_is_due_once(self):
        store = Store(self.data_file)
        item = RoutineItem("on", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="09:00")
        store.routines = [item]
        now = datetime(2026, 8, 5, 9, 0)
        self.assertEqual([entry.id for entry in store.due_routine_reminders(now)], ["on"])
        store.notified.add(store.routine_notification_key(item, now.date()))
        self.assertEqual(store.due_routine_reminders(now), [])

    def test_changing_routine_reminder_time_replaces_schedule(self):
        store = Store(self.data_file)
        original = RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="09:00")
        store.upsert_routine(original)
        old_key = store.routine_notification_key(original, date(2026, 8, 5))
        store.notified.add(old_key)
        edited = RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="10:00")
        store.upsert_routine(edited)
        self.assertNotIn(old_key, store.notified)
        self.assertEqual(store.due_routine_reminders(datetime(2026, 8, 5, 9, 30)), [])
        self.assertEqual([entry.id for entry in store.due_routine_reminders(datetime(2026, 8, 5, 10, 0))], ["habit"])

    def test_disabling_routine_reminder_clears_notification_state(self):
        store = Store(self.data_file)
        original = RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="09:00")
        store.upsert_routine(original)
        key = store.routine_notification_key(original, date(2026, 8, 5))
        store.notified.add(key)
        store.upsert_routine(RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=False, reminder_time="09:00"))
        self.assertNotIn(key, store.notified)
        self.assertEqual(store.due_routine_reminders(datetime(2026, 8, 5, 10, 0)), [])

    def test_deleting_routine_clears_notification_state(self):
        store = Store(self.data_file)
        item = RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="09:00")
        store.upsert_routine(item)
        key = store.routine_notification_key(item, date(2026, 8, 5))
        store.notified.add(key)
        store.delete_routine(item.id)
        self.assertNotIn(key, store.notified)
        self.assertEqual(store.routines, [])

    def test_routine_reminder_skips_non_workday(self):
        store = Store(self.data_file)
        store.routines = [RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="09:00")]
        self.assertEqual(store.due_routine_reminders(datetime(2026, 8, 8, 10, 0)), [])

    def test_completed_routine_does_not_remind_later_that_day(self):
        store = Store(self.data_file)
        item = RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="18:00")
        store.routines = [item]
        store.toggle_routine(item, date(2026, 8, 5))
        self.assertEqual(store.due_routine_reminders(datetime(2026, 8, 5, 18, 0)), [])

    def test_routine_reminder_fields_round_trip(self):
        store = Store(self.data_file)
        store.upsert_routine(
            RoutineItem("habit", "提醒", created_on="2026-08-03", reminder_enabled=True, reminder_time="07:30")
        )
        loaded = Store(self.data_file).routines[0]
        self.assertTrue(loaded.reminder_enabled)
        self.assertEqual(loaded.reminder_time, "07:30")


if __name__ == "__main__":
    unittest.main()
