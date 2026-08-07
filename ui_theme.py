from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemeMetrics:
    corner_radius: int = 8
    control_radius: int = 4
    date_radius: int = 7
    outer_border_width: int = 1
    inner_border_width: int = 1
    control_height: int = 26
    shadow_depth: int = 2
    highlight_alpha: float = 0.45
    animation_ms: int = 90


@dataclass(frozen=True)
class Theme:
    name: str
    display_name: str
    style: str

    # Window hierarchy
    window_background: str
    window_border_outer: str
    window_border_inner: str
    window_shadow: str
    panel_background: str
    panel_secondary: str
    divider: str

    # Header
    header_background: str
    header_gradient_start: str
    header_gradient_mid: str
    header_gradient_end: str
    header_highlight: str
    header_border: str
    header_shadow: str
    header_text: str
    header_subtext: str

    # Text
    text_primary: str
    text_secondary: str
    text_muted: str
    text_disabled: str
    text_on_accent: str

    # Controls
    control_background: str
    control_hover: str
    control_pressed: str
    control_border: str
    control_highlight: str
    control_disabled: str
    control_text: str

    # Calendar
    calendar_background: str
    weekday_text: str
    date_text: str
    date_other_month: str
    date_hover_background: str
    date_hover_border: str
    date_selected_background: str
    date_selected_border: str
    date_selected_gradient_start: str
    date_selected_gradient_end: str
    date_selected_inner_border: str
    date_today_background: str
    date_today_border: str
    date_event_indicator: str
    date_weekend_text: str
    date_selected_today: str

    # Schedule
    schedule_background: str
    schedule_card_background: str
    schedule_card_hover: str
    schedule_card_border: str
    schedule_time_text: str
    input_background: str
    input_border: str
    input_hover_border: str
    input_focus: str

    # Shared semantic accents used by the existing dialogs and event content.
    accent: str
    accent_hover: str
    accent_soft: str
    danger: str
    danger_soft: str
    ddl_indicator: str
    ddl_indicator_highlight: str
    event_type_general: str
    event_type_urgent: str
    event_type_ddl: str
    event_type_urgent_background: str
    event_type_urgent_border: str
    event_type_ddl_background: str
    event_type_ddl_border: str
    ddl_pinned_background: str
    ddl_pinned_border: str
    ddl_regular_background: str
    ddl_regular_border: str
    ddl_overdue_background: str
    ddl_due_background: str
    checkbox_border: str
    checkbox_checked: str
    date_leave_indicator: str
    date_holiday_indicator: str
    quick_success: str
    weekend: str
    holiday_workday: str
    holiday_festival: str
    tooltip_background: str
    tooltip_text: str
    card_done_background: str
    event_done: str
    text_done: str
    todo_tag_background: str
    todo_tag_text: str
    metrics: ThemeMetrics


MODERN = Theme(
    name="modern",
    display_name="Modern",
    style="flat",
    window_background="#F7F6F2",
    window_border_outer="#D8D7D2",
    window_border_inner="#FDFCFA",
    window_shadow="#B8B7B3",
    panel_background="#F7F6F2",
    panel_secondary="#EEEDE9",
    divider="#D8D7D2",
    header_background="#FAF9F6",
    header_gradient_start="#FAF9F6",
    header_gradient_mid="#FAF9F6",
    header_gradient_end="#FAF9F6",
    header_highlight="#FFFFFF",
    header_border="#D8D7D2",
    header_shadow="#D8D7D2",
    header_text="#25262B",
    header_subtext="#777A83",
    text_primary="#25262B",
    text_secondary="#777A83",
    text_muted="#A9ABB2",
    text_disabled="#C2C3C7",
    text_on_accent="#FFFFFF",
    control_background="#F2F1ED",
    control_hover="#ECECF3",
    control_pressed="#E0E1EA",
    control_border="#D2D0C9",
    control_highlight="#FFFFFF",
    control_disabled="#EFEFEB",
    control_text="#777A83",
    calendar_background="#FAF9F6",
    weekday_text="#A9ABB2",
    date_text="#25262B",
    date_other_month="#C2C3C7",
    date_hover_background="#F1F0F8",
    date_hover_border="#E0DFF0",
    date_selected_background="#6673D8",
    date_selected_border="#5B67C8",
    date_selected_gradient_start="#6273D9",
    date_selected_gradient_end="#6273D9",
    date_selected_inner_border="#6273D9",
    date_today_background="#FFF8E8",
    date_today_border="#E1A24B",
    date_event_indicator="#6273D9",
    date_weekend_text="#BC6B6B",
    date_selected_today="#F0B457",
    schedule_background="#F7F6F2",
    schedule_card_background="#FFFFFF",
    schedule_card_hover="#F7F7F5",
    schedule_card_border="#E4E3DF",
    schedule_time_text="#777A83",
    input_background="#FFFDFC",
    input_border="#D1CEC6",
    input_hover_border="#B8B4AC",
    input_focus="#6273D9",
    accent="#6273D9",
    accent_hover="#5263C6",
    accent_soft="#E8EAF8",
    danger="#D9515D",
    danger_soft="#FCEDEF",
    ddl_indicator="#D9515D",
    ddl_indicator_highlight="#F2B8BC",
    event_type_general="#8C8F99",
    event_type_urgent="#C47A32",
    event_type_ddl="#D9515D",
    event_type_urgent_background="#FBF1E4",
    event_type_urgent_border="#E8C99F",
    event_type_ddl_background="#FCEDEF",
    event_type_ddl_border="#E9BDC1",
    ddl_pinned_background="#FBF3F2",
    ddl_pinned_border="#EBC9C7",
    ddl_regular_background="#F7F5F2",
    ddl_regular_border="#E3DED7",
    ddl_overdue_background="#FCE9E9",
    ddl_due_background="#FBF2E7",
    checkbox_border="#B9BBC2",
    checkbox_checked="#9A9DA5",
    date_leave_indicator="#C78363",
    date_holiday_indicator="#B97863",
    quick_success="#4F927C",
    weekend="#BC6B6B",
    holiday_workday="#C47B28",
    holiday_festival="#8B70A8",
    tooltip_background="#303136",
    tooltip_text="#FFFFFF",
    card_done_background="#F0F0ED",
    event_done="#C5C6CA",
    text_done="#96989E",
    todo_tag_background="#F8EED7",
    todo_tag_text="#A56E14",
    metrics=ThemeMetrics(
        corner_radius=6,
        control_radius=4,
        date_radius=6,
        outer_border_width=0,
        inner_border_width=0,
        shadow_depth=1,
    ),
)


WIN7_AERO = Theme(
    name="win7_aero",
    display_name="Win7 Aero",
    style="aero",
    window_background="#C3CDD1",
    window_border_outer="#75838A",
    window_border_inner="#F8FCFC",
    window_shadow="#B0B7BA",
    panel_background="#FAFAF7",
    panel_secondary="#F1F2EF",
    divider="#E1E3E1",
    header_background="#D2E0E4",
    header_gradient_start="#F5FDFE",
    header_gradient_mid="#C8D8DD",
    header_gradient_end="#8A9FA8",
    header_highlight="#FFFFFF",
    header_border="#9EADB3",
    header_shadow="#82949B",
    header_text="#273B44",
    header_subtext="#68787E",
    text_primary="#26373D",
    text_secondary="#687277",
    text_muted="#929799",
    text_disabled="#B3B9BA",
    text_on_accent="#FFFFFF",
    control_background="#E9EEF0",
    control_hover="#F5FBFC",
    control_pressed="#CBD6DA",
    control_border="#AAB6BA",
    control_highlight="#FFFFFF",
    control_disabled="#E0E4E5",
    control_text="#334850",
    calendar_background="#FAFAF7",
    weekday_text="#788286",
    date_text="#26373D",
    date_other_month="#ACB2B3",
    date_hover_background="#F7FBFB",
    date_hover_border="#D6E5E6",
    date_selected_background="#C3CFD1",
    date_selected_border="#AAC4C7",
    date_selected_gradient_start="#EAF0F0",
    date_selected_gradient_end="#B9C7C9",
    date_selected_inner_border="#D9E2E3",
    date_today_background="#FFF8EA",
    date_today_border="#C99A55",
    date_event_indicator="#5A9DAC",
    date_weekend_text="#986D70",
    date_selected_today="#D8A357",
    schedule_background="#F7F7F4",
    schedule_card_background="#FDFDFC",
    schedule_card_hover="#F7F9F8",
    schedule_card_border="#E2E4E2",
    schedule_time_text="#727C80",
    input_background="#FAFAF8",
    input_border="#CDD2D2",
    input_hover_border="#AEBEC1",
    input_focus="#79ABB6",
    accent="#4C8998",
    accent_hover="#6FAEBB",
    accent_soft="#E4F0F2",
    danger="#B84D58",
    danger_soft="#F6E3E5",
    ddl_indicator="#B84D58",
    ddl_indicator_highlight="#E8C7C8",
    event_type_general="#849397",
    event_type_urgent="#9C7445",
    event_type_ddl="#B84D58",
    event_type_urgent_background="#F4EBDD",
    event_type_urgent_border="#D8C29E",
    event_type_ddl_background="#F5E6E6",
    event_type_ddl_border="#D9B9BB",
    ddl_pinned_background="#F8F1EE",
    ddl_pinned_border="#D8C0BA",
    ddl_regular_background="#F3F4F1",
    ddl_regular_border="#D7DDDA",
    ddl_overdue_background="#F5E5E3",
    ddl_due_background="#F5EEE2",
    checkbox_border="#AAB6B8",
    checkbox_checked="#8F9CA0",
    date_leave_indicator="#AA765F",
    date_holiday_indicator="#9F6B58",
    quick_success="#5D8F7E",
    weekend="#986D70",
    holiday_workday="#AD6B20",
    holiday_festival="#79618D",
    tooltip_background="#3E4B50",
    tooltip_text="#FFFFFF",
    card_done_background="#F0F1EF",
    event_done="#ABB2B3",
    text_done="#899194",
    todo_tag_background="#F3E7C9",
    todo_tag_text="#8A6218",
    metrics=ThemeMetrics(
        corner_radius=9,
        control_radius=5,
        date_radius=7,
        outer_border_width=1,
        inner_border_width=1,
        control_height=26,
        shadow_depth=1,
        highlight_alpha=0.62,
        animation_ms=80,
    ),
)


THEMES = {theme.name: theme for theme in (MODERN, WIN7_AERO)}
VALID_THEME_NAMES = tuple(THEMES)
DEFAULT_THEME_NAME = MODERN.name


def normalize_theme_name(value: object) -> str:
    return value if isinstance(value, str) and value in THEMES else DEFAULT_THEME_NAME


def get_theme(value: object) -> Theme:
    return THEMES[normalize_theme_name(value)]
