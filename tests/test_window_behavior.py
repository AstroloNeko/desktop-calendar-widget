import inspect
import unittest
from datetime import date, datetime
from types import SimpleNamespace

from app import (
    CalendarApp,
    DDL_LIST_ENTRY_LABEL,
    DDLListDialog,
    DayCell,
    DayDetailDialog,
    EVENT_STRIPE_WIDTH,
    EventEditor,
    ROUTINE_ENTRY_LABEL,
    ddl_display_datetime,
    ddl_list_logical_height,
    ddl_relative_label,
    event_stripe_color,
    main_region_visibility,
    parse_event_due,
)
from calendar_core import Event, RoutineItem


class _AvailableTray:
    error = None
    is_available = True


class _FakeCalendar:
    def __init__(self) -> None:
        self.tray_icon = _AvailableTray()
        self._lower_job = None
        self.desktop_session_active = True
        self.saved = False
        self.hidden = False

    def _start_tray_icon(self) -> None:
        raise AssertionError("available tray icon should be reused")

    def _save_window_settings(self) -> None:
        self.saved = True

    def withdraw(self) -> None:
        self.hidden = True


class WindowBehaviorTests(unittest.TestCase):
    def test_event_editor_presents_as_modal_child(self) -> None:
        actions: list[object] = []

        class FakeMaster:
            def present_overlay(self, window) -> None:
                actions.append(("present", window))

        class FakeEntry:
            def focus_set(self) -> None:
                actions.append("entry-focus")

        editor = type(
            "FakeEditor",
            (),
            {
                "master_app": FakeMaster(),
                "title_entry": FakeEntry(),
                "winfo_exists": lambda self: True,
                "transient": lambda self, parent: actions.append(("transient", parent)),
                "grab_set": lambda self: actions.append("grab"),
                "focus_set": lambda self: actions.append("focus"),
            },
        )()

        EventEditor._present(editor)

        self.assertEqual(actions[0], ("transient", editor.master_app))
        self.assertEqual(actions[1], ("present", editor))
        self.assertEqual(actions[2:], ["grab", "focus", "entry-focus"])

    def test_event_editor_close_releases_modal_grab_before_destroy(self) -> None:
        actions: list[str] = []

        class FakeMaster:
            editor_window = None
            day_detail_window = None

            def after(self, _delay: int, callback) -> None:
                callback()

            def apply_window_mode(self) -> None:
                actions.append("main")

        master = FakeMaster()
        editor = type(
            "FakeEditor",
            (),
            {
                "master_app": master,
                "grab_current": lambda self: self,
                "grab_release": lambda self: actions.append("release"),
                "destroy": lambda self: actions.append("destroy"),
            },
        )()
        master.editor_window = editor

        EventEditor.close(editor)

        self.assertEqual(actions, ["release", "destroy", "main"])

    def test_calendar_flow_drag_selects_range_without_opening_editor(self) -> None:
        actions: list[str] = []

        class FakeCanvas:
            def grab_current(self):
                return self

            def grab_release(self) -> None:
                actions.append("release")

        canvas = FakeCanvas()
        event = SimpleNamespace(widget=canvas)
        fake = type(
            "FakeCalendar",
            (),
            {
                "_global_flow_drag_anchor": date(2026, 8, 28),
                "_global_flow_drag_start": date(2026, 8, 28),
                "_global_flow_drag_end": date(2026, 8, 28),
                "_global_flow_drag_moved": True,
                "selected": date(2026, 8, 28),
                "_global_flow_day_from_event": lambda self, _event: date(2026, 8, 24),
                "_set_quick_placeholder": lambda self: actions.append("placeholder"),
                "_draw_calendar_flow": lambda self: actions.append("draw"),
                "open_new_event": lambda self, *_args, **_kwargs: actions.append("unexpected-editor"),
            },
        )()

        result = CalendarApp._finish_flow_drag(fake, event)

        self.assertEqual(result, "break")
        self.assertEqual(fake._global_flow_drag_start, date(2026, 8, 24))
        self.assertEqual(fake._global_flow_drag_end, date(2026, 8, 28))
        self.assertEqual(fake.selected, date(2026, 8, 24))
        self.assertNotIn("unexpected-editor", actions)
        self.assertEqual(actions, ["release", "placeholder", "draw"])

    def test_calendar_flow_double_click_uses_selected_range(self) -> None:
        captured: list[tuple[date, date, int]] = []
        selected_range = (date(2026, 8, 24), date(2026, 8, 30), 7)
        fake = type(
            "FakeCalendar",
            (),
            {
                "_canvas_has_timeline_item": lambda self, _widget: False,
                "_global_flow_day_from_event": lambda self, _event: date(2026, 8, 27),
                "_flow_selected_range": lambda self: selected_range,
                "_open_flow_range_editor": lambda self, *values: captured.append(values),
            },
        )()

        result = CalendarApp._create_flow_day(fake, SimpleNamespace(widget=object()))

        self.assertEqual(result, "break")
        self.assertEqual(captured, [selected_range])

    def test_calendar_flow_context_menus_use_existing_crud_with_confirmed_delete(self) -> None:
        range_menu = inspect.getsource(CalendarApp._show_flow_range_menu)
        for label in ("新增事项", "新增习惯", "设置日期状态", "取消选择"):
            self.assertIn(label, range_menu)
        item_menu = inspect.getsource(CalendarApp._show_flow_item_menu)
        for label in ("编辑", "取消完成", "完成", "删除"):
            self.assertIn(label, item_menu)
        self.assertIn("self._confirm_delete", item_menu)

    def test_global_workspace_wires_dual_view_switch(self) -> None:
        source = inspect.getsource(CalendarApp._build_global_ui)
        self.assertIn("▦ 月度排期", source)
        self.assertIn("▤ 任务时间轴", source)
        self.assertIn('set_global_display_mode("flow")', source)
        self.assertIn('set_global_display_mode("timeline")', source)

    def test_global_quick_add_uses_enter_without_ambiguous_plus_button(self) -> None:
        source = inspect.getsource(CalendarApp._build_global_ui)
        self.assertIn('self.quick_entry.bind("<Return>", self.quick_add)', source)
        self.assertNotIn('ThemeButton(quick_frame, self, "+", self.quick_add', source)

    def test_global_toolbar_names_compact_return_explicitly(self) -> None:
        source = inspect.getsource(CalendarApp._build_global_ui)
        self.assertIn('"紧凑视图", self.return_to_compact_view', source)

    def test_global_display_switch_persists_and_redraws_in_place(self) -> None:
        actions: list[str] = []

        class FakeStore:
            settings: dict[str, str] = {}

            def save(self) -> None:
                actions.append("save")

        fake = type(
            "FakeCalendar",
            (),
            {
                "global_display_mode": "timeline",
                "store": FakeStore(),
                "_cancel_flow_drag": lambda self, **_kwargs: None,
                "_update_global_display_mode_widgets": lambda self: actions.append("widgets"),
                "_draw_active_global_view": lambda self: actions.append("draw"),
            },
        )()
        CalendarApp.set_global_display_mode(fake, "flow")
        self.assertEqual(fake.global_display_mode, "flow")
        self.assertEqual(fake.store.settings["global_display_mode"], "flow")
        self.assertEqual(actions, ["save", "widgets", "draw"])

    def test_global_display_mode_is_saved_with_window_settings(self) -> None:
        source = inspect.getsource(CalendarApp._save_window_settings)
        self.assertIn('self.store.settings["global_display_mode"]', source)

    def test_calendar_flow_today_indicator_uses_theme_tokens(self) -> None:
        source = inspect.getsource(CalendarApp._draw_calendar_flow)
        self.assertIn('text="今天"', source)
        self.assertIn("theme.date_today_background", source)
        self.assertIn("theme.date_today_border", source)

    def test_escape_cancels_calendar_flow_drag_without_opening_editor(self) -> None:
        actions: list[str] = []
        fake = type(
            "FakeCalendar",
            (),
            {
                "view_mode": "global",
                "global_display_mode": "flow",
                "_global_flow_drag_start": date(2026, 8, 3),
                "_global_flow_drag_end": date(2026, 8, 7),
                "_global_flow_drag_moved": True,
                "_draw_calendar_flow": lambda self: actions.append("redraw"),
                "_cancel_flow_drag": CalendarApp._cancel_flow_drag,
                "_end_desktop_session": lambda self: actions.append("desktop"),
            },
        )()
        self.assertEqual(CalendarApp._handle_escape(fake), "break")
        self.assertIsNone(fake._global_flow_drag_start)
        self.assertIsNone(fake._global_flow_drag_end)
        self.assertFalse(fake._global_flow_drag_moved)
        self.assertEqual(actions, ["redraw"])

    def test_global_detail_keeps_existing_task_semantics(self) -> None:
        class Capture:
            def __init__(self) -> None:
                self.values: dict[str, object] = {}

            def configure(self, **values) -> None:
                self.values.update(values)

            def set_text(self, text: str) -> None:
                self.values["text"] = text

        fake = SimpleNamespace(
            global_detail_title=Capture(),
            global_detail_meta=Capture(),
            global_detail_notes=Capture(),
            global_detail_toggle_button=Capture(),
        )
        item = SimpleNamespace(
            title="跨周交付",
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 7),
            calendar_span_days=5,
            effective_days_count=5,
            task_type="urgent",
            ddl_date=date(2026, 8, 7),
            completed=False,
            notes="交付前复核",
        )
        CalendarApp._update_global_detail(fake, item)
        self.assertEqual(fake.global_detail_title.values["text"], "跨周交付")
        detail = str(fake.global_detail_meta.values["text"])
        self.assertIn("2026-08-03 → 2026-08-07", detail)
        self.assertIn("5 个自然日 · 5 个有效工作日", detail)
        self.assertIn("类型：紧急", detail)
        self.assertIn("DDL：2026-08-07", detail)
        self.assertIn("状态：已逾期", detail)
        self.assertIn("交付前复核", str(fake.global_detail_notes.values["text"]))

    def test_global_workspace_wires_complete_toolbar_and_quick_add(self) -> None:
        source = inspect.getsource(CalendarApp._build_global_ui)
        for command in (
            "open_new_event",
            "open_day_detail",
            "open_routine_manager",
            "open_ddl_list",
            "toggle_window_mode",
            "show_main_menu",
            "return_to_compact_view",
            "hide_to_tray",
            "quick_add",
            "show_quick_options",
        ):
            self.assertIn(command, source)

    def test_global_workspace_uses_theme_tokens_instead_of_hex_colors(self) -> None:
        source = inspect.getsource(CalendarApp._build_global_ui)
        self.assertNotIn("#FFFFFF", source)
        self.assertNotIn("#000000", source)
        self.assertIn("theme.", source)

    def test_global_primary_action_opens_new_event(self) -> None:
        actions: list[str] = []
        fake = type(
            "FakeCalendar",
            (),
            {
                "view_mode": "global",
                "open_new_event": lambda self: actions.append("create"),
                "open_day_detail": lambda self: actions.append("detail"),
            },
        )()
        CalendarApp._open_primary_action(fake)
        self.assertEqual(actions, ["create"])

    def test_scroll_callbacks_keep_headers_and_labels_synchronized(self) -> None:
        actions: list[tuple[str, float]] = []

        class FakeCanvas:
            def xview_moveto(self, value: float) -> None:
                actions.append(("date", value))

            def yview_moveto(self, value: float) -> None:
                actions.append(("label", value))

        class FakeScrollbar:
            def set(self, first: str, _last: str) -> None:
                actions.append(("scrollbar", float(first)))

        fake = type(
            "FakeCalendar",
            (),
            {
                "global_hscroll": FakeScrollbar(),
                "global_vscroll": FakeScrollbar(),
                "global_date_canvas": FakeCanvas(),
                "global_label_canvas": FakeCanvas(),
            },
        )()
        CalendarApp._sync_global_xscroll(fake, "0.25", "0.75")
        CalendarApp._sync_global_yscroll(fake, "0.40", "0.90")
        self.assertIn(("date", 0.25), actions)
        self.assertIn(("label", 0.4), actions)

    def test_main_ddl_entry_uses_complete_list_label(self) -> None:
        self.assertEqual(DDL_LIST_ENTRY_LABEL, "DDL列表")

    def test_global_toggle_routes_by_explicit_view_mode(self) -> None:
        actions: list[str] = []
        compact = type(
            "FakeCalendar",
            (),
            {
                "view_mode": "compact",
                "enter_global_view": lambda self: actions.append("global"),
                "return_to_compact_view": lambda self: actions.append("compact"),
            },
        )()
        CalendarApp.toggle_global_view(compact)
        compact.view_mode = "global"
        CalendarApp.toggle_global_view(compact)
        self.assertEqual(actions, ["global", "compact"])

    def test_view_mode_is_not_inferred_from_window_width(self) -> None:
        fake = type("FakeCalendar", (), {"view_mode": "compact", "winfo_width": lambda self: 2000})()
        self.assertEqual(CalendarApp.get_view_mode(fake), "compact")

    def test_return_to_compact_activates_foreground_session_in_desktop_mode(self) -> None:
        fake = type("FakeCalendar", (), {"window_mode": "desktop", "desktop_session_active": False})()
        CalendarApp._activate_compact_return_session(fake)
        self.assertTrue(fake.desktop_session_active)

        fake.window_mode = "pinned"
        CalendarApp._activate_compact_return_session(fake)
        self.assertFalse(fake.desktop_session_active)

    def test_ddl_relative_labels_cover_overdue_today_tomorrow_and_future(self) -> None:
        now = datetime(2026, 8, 7, 12, 0)
        self.assertEqual(ddl_relative_label(datetime(2026, 8, 7, 11, 59), now), "已逾期")
        self.assertEqual(ddl_relative_label(datetime(2026, 8, 7, 18, 0), now), "今天")
        self.assertEqual(ddl_relative_label(datetime(2026, 8, 8, 9, 0), now), "明天")
        self.assertEqual(ddl_relative_label(datetime(2026, 8, 10, 9, 0), now), "3天后")

    def test_ddl_display_datetime_is_compact_but_keeps_other_year(self) -> None:
        now = datetime(2026, 8, 7, 12, 0)
        self.assertEqual(ddl_display_datetime(datetime(2026, 8, 10, 18, 30), now), "8月10日 18:30")
        self.assertEqual(ddl_display_datetime(datetime(2027, 1, 2, 9, 5), now), "2027年1月2日 09:05")

    def test_ddl_list_height_adapts_and_caps_long_collections(self) -> None:
        self.assertEqual(ddl_list_logical_height(0, 0, 1, 0, False), 250)
        self.assertEqual(ddl_list_logical_height(0, 0, 0, 8, False), 250)
        self.assertGreater(
            ddl_list_logical_height(0, 0, 0, 8, True),
            ddl_list_logical_height(0, 0, 0, 8, False),
        )
        self.assertEqual(ddl_list_logical_height(4, 4, 12, 8, True), 590)

    def test_ddl_entry_reuses_existing_complete_list_window(self) -> None:
        presented: list[object] = []

        class FakeDDLList:
            def winfo_exists(self) -> bool:
                return True

        ddl_list = FakeDDLList()
        fake = type(
            "FakeCalendar",
            (),
            {
                "ddl_list_window": ddl_list,
                "present_overlay": lambda self, window: presented.append(window),
            },
        )()

        CalendarApp.open_ddl_list(fake)
        self.assertEqual(presented, [ddl_list])

    def test_completed_ddl_section_toggle_refreshes_window(self) -> None:
        refreshed: list[bool] = []
        fake = type(
            "FakeDDLList",
            (),
            {"completed_open": False, "refresh": lambda self: refreshed.append(self.completed_open)},
        )()
        DDLListDialog._toggle_completed(fake)
        self.assertTrue(fake.completed_open)
        self.assertEqual(refreshed, [True])

    def test_main_routine_entry_uses_habit_module_label(self) -> None:
        self.assertEqual(ROUTINE_ENTRY_LABEL, "习惯")

    def test_routine_entry_opens_existing_habit_manager(self) -> None:
        presented: list[object] = []

        class FakeManager:
            def winfo_exists(self) -> bool:
                return True

        manager = FakeManager()
        fake = type(
            "FakeCalendar",
            (),
            {
                "routine_manager": manager,
                "present_overlay": lambda self, window: presented.append(window),
            },
        )()

        CalendarApp.open_routine_manager(fake)
        self.assertEqual(presented, [manager])

    def test_routine_scheduler_marks_each_due_item_and_notifies_once(self) -> None:
        now = datetime(2026, 8, 5, 9, 0)
        first = RoutineItem("first", "第一个习惯", reminder_enabled=True, reminder_time="09:00")
        second = RoutineItem("second", "第二个习惯", reminder_enabled=True, reminder_time="08:30")
        notified: set[str] = set()
        shown: list[list[RoutineItem]] = []

        class FakeStore:
            def due_routine_reminders(self, _now: datetime) -> list[RoutineItem]:
                return [first, second]

            def routine_notification_key(self, item: RoutineItem, day: date) -> str:
                return f"routine:{item.id}:{day.isoformat()}:{item.reminder_time}"

        fake = type(
            "FakeCalendar",
            (),
            {
                "store": FakeStore(),
                "show_routine_notification": lambda self, items: shown.append(items),
            },
        )()
        fake.store.notified = notified

        self.assertTrue(CalendarApp._check_routine_reminder(fake, now))
        self.assertEqual(
            notified,
            {
                "routine:first:2026-08-05:09:00",
                "routine:second:2026-08-05:08:30",
            },
        )
        self.assertEqual(shown, [[first, second]])

    def test_collapsed_layout_keeps_quick_add_and_pinned_ddl_visible(self) -> None:
        visible = main_region_visibility(False, pinned_ddl_count=2, regular_ddl_count=3)
        self.assertTrue(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.agenda_header)
        self.assertTrue(visible.footer)
        self.assertFalse(visible.daily_content)
        self.assertFalse(visible.regular_ddl)

    def test_expanded_layout_shows_daily_and_regular_ddl_content(self) -> None:
        visible = main_region_visibility(True, pinned_ddl_count=0, regular_ddl_count=2)
        self.assertFalse(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.daily_content)
        self.assertTrue(visible.regular_ddl)

    def test_empty_collapsed_layout_keeps_persistent_regions(self) -> None:
        visible = main_region_visibility(False, pinned_ddl_count=0, regular_ddl_count=0)
        self.assertFalse(visible.pinned_ddl)
        self.assertTrue(visible.quick_add)
        self.assertTrue(visible.agenda_header)
        self.assertTrue(visible.footer)
        self.assertFalse(visible.daily_content)
        self.assertFalse(visible.regular_ddl)

    def test_regular_ddl_region_adds_height_without_reducing_daily_viewport(self) -> None:
        fake = type(
            "FakeCalendar",
            (),
            {
                "agenda_open": True,
                "winfo_screenheight": lambda self: 2000,
                "_ddl_canvas_height": CalendarApp._ddl_canvas_height,
                "_ddl_region_height": CalendarApp._ddl_region_height,
            },
        )()
        without_regular = CalendarApp._desired_window_height(fake, 0, 0)
        with_regular = CalendarApp._desired_window_height(fake, 0, 1)
        self.assertGreater(with_regular, without_regular)
        self.assertEqual(with_regular - without_regular, fake._ddl_region_height(1))

        fake.agenda_open = False
        self.assertEqual(
            CalendarApp._desired_window_height(fake, 0, 1),
            CalendarApp._desired_window_height(fake, 0, 0),
        )

    def test_event_time_can_be_left_blank(self) -> None:
        due, has_time = parse_event_due("2026-08-05", "")
        self.assertFalse(has_time)
        self.assertEqual(due.isoformat(timespec="minutes"), "2026-08-05T23:59")

    def test_event_time_is_kept_when_supplied(self) -> None:
        due, has_time = parse_event_due("2026-08-05", "09:30")
        self.assertTrue(has_time)
        self.assertEqual(due.isoformat(timespec="minutes"), "2026-08-05T09:30")

    def test_hide_to_tray_keeps_app_alive(self) -> None:
        fake = _FakeCalendar()
        CalendarApp.hide_to_tray(fake)
        self.assertTrue(fake.saved)
        self.assertTrue(fake.hidden)
        self.assertFalse(fake.desktop_session_active)
        self.assertIsNotNone(fake.tray_icon)

    def test_double_click_opens_day_detail_instead_of_editor(self) -> None:
        selected: list[date] = []
        details: list[date] = []

        class FakeApp:
            def select_day(self, day: date) -> None:
                selected.append(day)

            def open_day_detail(self, day: date) -> None:
                details.append(day)

            def open_editor(self, **_kwargs) -> None:
                raise AssertionError("double click must not open the editor directly")

        fake_cell = type("FakeCell", (), {"app": FakeApp(), "day": date(2026, 8, 5)})()
        result = DayCell._double_click(fake_cell)
        self.assertEqual(result, "break")
        self.assertEqual(selected, [date(2026, 8, 5)])
        self.assertEqual(details, [date(2026, 8, 5)])

    def test_main_add_entry_opens_editor_for_selected_date(self) -> None:
        opened: list[date] = []
        selected_day = date(2026, 8, 7)

        fake = type(
            "FakeCalendar",
            (),
            {
                "selected": selected_day,
                "open_editor": lambda self, **kwargs: opened.append(kwargs["selected"]),
            },
        )()

        CalendarApp.open_new_event(fake)
        self.assertEqual(opened, [selected_day])

    def test_drag_created_event_prefills_existing_duration_field(self) -> None:
        captured: dict[str, object] = {}
        selected_day = date(2026, 8, 3)
        fake = type(
            "FakeCalendar",
            (),
            {"selected": selected_day, "open_editor": lambda self, **kwargs: captured.update(kwargs)},
        )()
        CalendarApp.open_new_event(fake, selected_day, duration_days=5)
        self.assertEqual(captured["selected"], selected_day)
        self.assertEqual(captured["initial_duration_days"], 5)

    def test_day_detail_add_entry_opens_editor_for_detail_date(self) -> None:
        opened: list[date] = []
        detail_day = date(2026, 8, 8)

        master = type("FakeCalendar", (), {"open_new_event": lambda self, day: opened.append(day)})()
        detail = type("FakeDetail", (), {"master_app": master, "day": detail_day})()

        DayDetailDialog._add_event(detail)
        self.assertEqual(opened, [detail_day])

    def test_editor_close_refreshes_existing_day_detail(self) -> None:
        actions: list[str] = []

        class FakeDetail:
            def winfo_exists(self) -> bool:
                return True

            def refresh(self) -> None:
                actions.append("refresh")

        class FakeMaster:
            editor_window = None
            day_detail_window = FakeDetail()

            def after(self, _delay: int, callback) -> None:
                callback()

            def present_overlay(self, _window) -> None:
                actions.append("present")

        master = FakeMaster()
        editor = type("FakeEditor", (), {"master_app": master, "destroy": lambda self: actions.append("destroy")})()
        master.editor_window = editor

        EventEditor.close(editor)

        self.assertIsNone(master.editor_window)
        self.assertEqual(actions, ["destroy", "refresh", "present"])

    def test_editor_cancel_without_detail_only_returns_to_main_window(self) -> None:
        actions: list[str] = []

        class FakeMaster:
            editor_window = None
            day_detail_window = None

            def after(self, _delay: int, callback) -> None:
                callback()

            def apply_window_mode(self) -> None:
                actions.append("main")

        master = FakeMaster()
        editor = type("FakeEditor", (), {"master_app": master, "destroy": lambda self: actions.append("destroy")})()
        master.editor_window = editor

        EventEditor.close(editor)

        self.assertIsNone(master.editor_window)
        self.assertEqual(actions, ["destroy", "main"])

    def test_event_stripe_uses_item_color_independently_from_type(self) -> None:
        theme = type("FakeTheme", (), {"event_done": "#A0A0A0"})()
        event = Event("ddl", "DDL", "2026-08-05T10:00", color="#E65D67", event_type="ddl")
        self.assertEqual(event_stripe_color(theme, event), "#E65D67")
        self.assertEqual(EVENT_STRIPE_WIDTH, 4)
        event.done = True
        self.assertEqual(event_stripe_color(theme, event), "#A0A0A0")

    def test_quick_add_passes_selected_color_and_event_type(self) -> None:
        captured: dict[str, object] = {}

        class FakeVar:
            value = "快速事项"

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class FakeStore:
            def create_quick(self, title: str, day: date, **options) -> None:
                captured.update(title=title, day=day, **options)

        class FakeEntry:
            def focus_set(self) -> None:
                captured["focused"] = True

        fake = type(
            "FakeCalendar",
            (),
            {
                "quick_var": FakeVar(),
                "quick_placeholder_active": False,
                "quick_color": "#E65D67",
                "quick_event_type": "ddl",
                "selected": date(2026, 8, 5),
                "store": FakeStore(),
                "quick_entry": FakeEntry(),
                "render": lambda self: captured.update(rendered=True),
            },
        )()
        self.assertEqual(CalendarApp.quick_add(fake), "break")
        self.assertEqual(captured["event_type"], "ddl")
        self.assertEqual(captured["color"], "#E65D67")
        self.assertTrue(captured["rendered"])
        self.assertTrue(captured["focused"])


if __name__ == "__main__":
    unittest.main()
