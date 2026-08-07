from __future__ import annotations

import calendar
import queue
import shutil
import sys
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from dpi_utils import DpiManager, LogicalCanvas, enable_dpi_awareness, scale_px, scaled_geometry, unscale_px


DPI_AWARENESS_MODE = enable_dpi_awareness()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from calendar_core import (
    APP_DIR,
    APP_NAME,
    COLORS,
    DATA_DIR,
    DATE_STATUS_LABELS,
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OPTIONS,
    REMINDERS,
    WEEKDAYS,
    Event,
    RoutineItem,
    Store,
    normalize_reminder_time,
)
from holiday_data import HolidayInfo, holiday_for
from tray_icon import TrayIcon
from win_integration import (
    SingleInstance,
    bring_to_front,
    clamp_to_work_area,
    is_autostart_enabled,
    is_foreground_process,
    make_tool_window,
    raise_for_interaction,
    send_to_desktop,
    set_autostart,
)
from update_manager import (
    UpdateError,
    UpdateInfo,
    check_for_update,
    download_update,
    is_newer_version,
    launch_updater,
    running_as_packaged_app,
)
from version import __version__
from ui_draw import (
    bevel_control,
    blend,
    draw_calendar_date_ring,
    draw_calendar_date_state,
    draw_calendar_today_accent,
    rounded_rectangle,
    vertical_gradient,
    vertical_multi_gradient,
)
from ui_theme import Theme, get_theme, normalize_theme_name


WINDOW_WIDTH = 372
OPEN_HEIGHT = 548
CLOSED_HEIGHT = 405
DDL_VISIBLE_ROWS = 2
DDL_ROW_HEIGHT = 30
DDL_REGION_CHROME_HEIGHT = 58
EVENT_STRIPE_WIDTH = 4
ROUTINE_ENTRY_LABEL = "习惯"
DATE_STATE_HALF_WIDTH = 14
DATE_STATE_TOP = 1
DATE_STATE_BOTTOM = 23
DATE_RING_HALF_WIDTH = 17
DATE_RING_TOP = 0
DATE_RING_BOTTOM = 26

FONT = "Microsoft YaHei UI"


_ACTIVE_THEME = get_theme("modern")


@dataclass(frozen=True)
class MainRegionVisibility:
    pinned_ddl: bool
    quick_add: bool
    agenda_header: bool
    daily_content: bool
    regular_ddl: bool
    footer: bool


def main_region_visibility(
    agenda_open: bool,
    pinned_ddl_count: int,
    regular_ddl_count: int,
) -> MainRegionVisibility:
    """Describe which semantic areas belong in the current main-window state."""
    return MainRegionVisibility(
        pinned_ddl=pinned_ddl_count > 0,
        quick_add=True,
        agenda_header=True,
        daily_content=agenda_open,
        regular_ddl=agenda_open and regular_ddl_count > 0,
        footer=True,
    )


def current_theme() -> Theme:
    return _ACTIVE_THEME


def activate_theme(theme: Theme) -> None:
    """Update compatibility aliases used by dialogs not yet fully redrawn."""
    global _ACTIVE_THEME, SURFACE, CARD, INK, SUBTLE, FAINT, BORDER, HOVER
    global ACCENT, ACCENT_SOFT, WEEKEND, DANGER, FIELD_BACKGROUND, CONTROL_BACKGROUND
    global CARD_MUTED, CARD_BORDER, DANGER_SOFT, ACCENT_HOVER, EVENT_DONE, TEXT_DONE
    _ACTIVE_THEME = theme
    SURFACE = theme.panel_background
    CARD = theme.schedule_card_background
    INK = theme.text_primary
    SUBTLE = theme.text_secondary
    FAINT = theme.text_muted
    BORDER = theme.divider
    HOVER = theme.control_hover
    ACCENT = theme.accent
    ACCENT_SOFT = theme.accent_soft
    WEEKEND = theme.weekend
    DANGER = theme.danger
    FIELD_BACKGROUND = theme.input_background
    CONTROL_BACKGROUND = theme.panel_secondary
    CARD_MUTED = theme.schedule_card_hover
    CARD_BORDER = theme.schedule_card_border
    DANGER_SOFT = theme.danger_soft
    ACCENT_HOVER = theme.accent_hover
    EVENT_DONE = theme.event_done
    TEXT_DONE = theme.text_done


activate_theme(_ACTIVE_THEME)


def event_type_color(theme: Theme, event_type: str, *, done: bool = False) -> str:
    """Return the semantic type accent without replacing an event's own color."""
    if done:
        return theme.event_done
    return {
        "urgent": theme.event_type_urgent,
        "ddl": theme.event_type_ddl,
    }.get(event_type, theme.event_type_general)


def event_stripe_color(theme: Theme, event: Event) -> str:
    """Keep the card color channel independent from the event's type."""
    return theme.event_done if event.done else event.color


def event_type_badge_style(theme: Theme, event_type: str) -> tuple[str, str, str] | None:
    """Return the compact badge palette for exceptional event types."""
    if event_type == "urgent":
        return theme.event_type_urgent, theme.event_type_urgent_background, theme.event_type_urgent_border
    if event_type == "ddl":
        return theme.event_type_ddl, theme.event_type_ddl_background, theme.event_type_ddl_border
    return None


def geometry_at(width: int, height: int, x: int, y: int) -> str:
    return scaled_geometry(width, height, x, y)


def position_at(x: int, y: int) -> str:
    return f"{x:+d}{y:+d}"


def center_toplevel(
    window: tk.Toplevel,
    master: tk.Misc,
    width: int,
    height: int,
    *,
    y_offset: int = 0,
) -> None:
    """Center a logical-size popup using device pixels on the master's monitor."""
    device_width = scale_px(width)
    device_height = scale_px(height)
    x = master.winfo_rootx() + (master.winfo_width() - device_width) // 2
    y = master.winfo_rooty() + scale_px(y_offset)
    x, y = clamp_to_work_area(x, y, device_width, device_height)
    window.geometry(geometry_at(width, height, x, y))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def parse_event_due(date_text: str, time_text: str) -> tuple[datetime, bool]:
    selected_date = datetime.strptime(date_text.strip(), "%Y-%m-%d").date()
    cleaned_time = time_text.strip()
    if cleaned_time:
        return datetime.strptime(f"{selected_date.isoformat()} {cleaned_time}", "%Y-%m-%d %H:%M"), True
    return datetime.combine(selected_date, datetime.min.time()).replace(hour=23, minute=59), False


def log_exception(exc_type, exc_value, exc_traceback) -> None:
    """Keep pythonw failures diagnosable without reopening a console window."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "calendar.log").open("a", encoding="utf-8") as log:
            log.write("\n" + datetime.now().isoformat(timespec="seconds") + "\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=log)
    except OSError:
        pass


def button_label(
    parent: tk.Widget,
    text: str,
    command: Callable[[], None],
    *,
    width: int = 3,
    bg: Optional[str] = None,
    fg: Optional[str] = None,
    hover: Optional[str] = None,
    font_size: int = 10,
) -> tk.Label:
    palette = current_theme()
    bg = bg or palette.control_background
    fg = fg or palette.control_text
    hover = hover or palette.control_hover
    label = tk.Label(
        parent,
        text=text,
        width=width,
        bg=bg,
        fg=fg,
        font=(FONT, font_size),
        cursor="hand2",
        padx=1,
        pady=3,
    )
    label.bind("<Button-1>", lambda _event: command())
    label.bind("<Enter>", lambda _event: label.configure(bg=hover))
    label.bind("<Leave>", lambda _event: label.configure(bg=bg))
    return label


class ThemeButton(LogicalCanvas):
    """Compact flat/Aero control used by the main gadget chrome."""

    def __init__(
        self,
        parent: tk.Widget,
        app: "CalendarApp",
        text: str,
        command: Callable[[], None],
        *,
        width: int = 28,
        height: int = 27,
        font_size: int = 9,
        foreground: Optional[str] = None,
        surface_background: Optional[str] = None,
        accented: bool = False,
        outlined: bool = False,
    ) -> None:
        self.app = app
        self.text = text
        self.command = command
        self.font_size = font_size
        self.foreground = foreground
        self.surface_background = surface_background
        self.accented = accented
        self.outlined = outlined
        self.state = "normal"
        super().__init__(
            parent,
            dpi=app.dpi,
            width=width,
            height=height,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
        )
        self.bind("<Enter>", lambda _event: self._set_state("hover"))
        self.bind("<Leave>", lambda _event: self._set_state("normal"))
        self.bind("<ButtonPress-1>", lambda _event: self._set_state("pressed"))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda _event: self.command())
        self.bind("<Return>", lambda _event: self.command())
        self.draw()

    def _set_state(self, state: str) -> None:
        self.state = state
        self.draw()

    def _release(self, event: tk.Event) -> None:
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._set_state("hover" if inside else "normal")
        if inside:
            self.command()

    def set_text(self, text: str, foreground: Optional[str] = None) -> None:
        self.text = text
        self.foreground = foreground
        self.draw()

    def draw(self) -> None:
        theme = self.app.theme
        width = max(2, self.logical_width())
        height = max(2, self.logical_height())
        self.delete("all")
        if theme.style == "aero":
            background = (
                {
                    "normal": blend(theme.accent, theme.control_background, 0.58),
                    "hover": blend(theme.accent_hover, theme.control_hover, 0.52),
                    "pressed": blend(theme.accent, theme.control_pressed, 0.48),
                }[self.state]
                if self.accented
                else {
                    "normal": theme.control_background,
                    "hover": theme.control_hover,
                    "pressed": theme.control_pressed,
                }[self.state]
            )
            border = theme.accent if self.accented else theme.accent_hover if self.state == "hover" else theme.control_border
            highlight = (
                blend(theme.control_pressed, theme.control_highlight, 0.42)
                if self.state == "pressed"
                else theme.control_highlight
            )
            self.configure(bg=self.surface_background or theme.header_background)
            bevel_control(
                self,
                width,
                height,
                background=background,
                border=border,
                highlight=highlight,
                radius=theme.metrics.control_radius,
                pressed=self.state == "pressed",
            )
        else:
            if self.accented:
                background = theme.accent_hover if self.state == "hover" else blend(theme.accent, theme.control_pressed, 0.18) if self.state == "pressed" else theme.accent
            elif not self.outlined and self.state == "normal":
                background = self.surface_background or theme.header_background
            else:
                background = theme.control_hover if self.state == "hover" else theme.control_pressed if self.state == "pressed" else theme.control_background
            self.configure(bg=self.surface_background or theme.header_background)
            rounded_rectangle(
                self,
                1,
                1,
                width - 2,
                height - 2,
                theme.metrics.control_radius,
                fill=background,
                outline=theme.control_border if self.outlined else "",
                width=1,
            )
        y_offset = 1 if self.state == "pressed" else 0
        text_color = self.foreground or (theme.text_on_accent if self.accented and theme.style == "flat" else theme.control_text)
        self.create_text(
            width / 2,
            height / 2 + y_offset,
            text=self.text,
            fill=text_color,
            font=(FONT, self.font_size),
        )


class TaskCheck(LogicalCanvas):
    """Compact themed checkbox with a generous, fixed click target."""

    def __init__(
        self,
        parent: tk.Widget,
        app: "CalendarApp",
        *,
        done: bool,
        background: str,
        command: Callable[[], None],
        height: int = 42,
    ) -> None:
        super().__init__(
            parent,
            dpi=app.dpi,
            width=34,
            height=height,
            bg=background,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.app = app
        self.done = done
        self.command = command
        self.bind("<Button-1>", lambda _event: self.command())
        self.bind("<Configure>", lambda _event: self.draw())
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        theme = self.app.theme
        center_y = max(12, int(self.logical_height() / 2))
        box_fill = theme.checkbox_checked if self.done else str(self.cget("bg"))
        rounded_rectangle(
            self,
            11,
            center_y - 6,
            23,
            center_y + 6,
            3,
            fill=box_fill,
            outline=theme.event_done if self.done else theme.checkbox_border,
            width=1,
        )
        if self.done:
            self.create_line(14, center_y, 17, center_y + 3, 21, center_y - 3, fill=theme.text_on_accent, width=1)


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: Optional[tk.Toplevel] = None
        self.after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Button-1>", self.hide, add="+")

    def _schedule(self, _event=None) -> None:
        self.after_id = self.widget.after(550, self.show)

    def show(self) -> None:
        if self.window or not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.window.geometry(position_at(x, y))
        palette = current_theme()
        tk.Label(
            self.window,
            text=self.text,
            bg=palette.tooltip_background,
            fg=palette.tooltip_text,
            font=(FONT, 8),
            padx=8,
            pady=4,
        ).pack()

    def hide(self, _event=None) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        if self.window:
            self.window.destroy()
            self.window = None


class DayCell(LogicalCanvas):
    def __init__(self, parent: tk.Widget, app: "CalendarApp", column: int) -> None:
        cell_height = 35 if app.theme.style == "aero" else 36
        super().__init__(
            parent,
            dpi=app.dpi,
            width=46,
            height=cell_height,
            bg=app.theme.calendar_background,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.app = app
        self.column = column
        self.day = date.today()
        self.in_month = True
        self.selected = False
        self.today = False
        self.hovered = False
        self.event_colors: list[str] = []
        self.holiday: Optional[HolidayInfo] = None
        self.date_status = "normal"
        self.ddl = False
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Double-Button-1>", self._double_click)
        self.bind("<Button-3>", self._right_click)

    def update_day(
        self,
        day: date,
        in_month: bool,
        selected: bool,
        today: bool,
        colors: list[str],
        holiday: Optional[HolidayInfo] = None,
        ddl: bool = False,
        date_status: str = "normal",
    ) -> None:
        self.day = day
        self.in_month = in_month
        self.selected = selected
        self.today = today
        self.event_colors = colors[:3]
        self.holiday = holiday
        self.ddl = ddl
        self.date_status = date_status
        self.draw()

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self.draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.draw()

    def _click(self, _event=None) -> None:
        self.focus_set()
        self.app.select_day(self.day)

    def _double_click(self, _event=None) -> str:
        self.app.select_day(self.day)
        self.app.open_day_detail(self.day)
        return "break"

    def _right_click(self, event: tk.Event) -> None:
        self.app.select_day(self.day)
        self.app.show_day_menu(self.day, event.x_root, event.y_root)

    def draw(self) -> None:
        # Date cells are rebuilt from a clean tagged canvas on every state
        # transition, so hover/selected/today layers cannot accumulate.
        self.delete("all")
        theme = self.app.theme
        self.configure(bg=theme.calendar_background)
        width = max(self.logical_width(), 38)
        center_x = width // 2
        # Date states always frame the number only. Holiday text has its own
        # baseline below, so selecting a festival never changes the box size.
        date_y = 11
        if self.hovered and not self.selected:
            draw_calendar_date_state(
                self,
                center_x - DATE_STATE_HALF_WIDTH,
                DATE_STATE_TOP,
                center_x + DATE_STATE_HALF_WIDTH,
                DATE_STATE_BOTTOM,
                fill=theme.date_hover_background,
                border=theme.date_hover_border,
                radius=theme.metrics.date_radius,
                top_highlight=blend(theme.date_hover_background, theme.control_highlight, 0.30) if theme.style == "aero" else None,
            )
        if self.selected:
            selected_glass = theme.style == "aero"
            draw_calendar_date_state(
                self,
                center_x - DATE_STATE_HALF_WIDTH,
                DATE_STATE_TOP,
                center_x + DATE_STATE_HALF_WIDTH,
                DATE_STATE_BOTTOM,
                fill=theme.date_selected_background,
                border=theme.date_selected_border,
                radius=max(4, theme.metrics.date_radius - 1) if selected_glass else theme.metrics.date_radius,
                gradient_start=theme.date_selected_gradient_start if selected_glass else None,
                gradient_end=theme.date_selected_gradient_end if selected_glass else None,
                inner_border=theme.date_selected_inner_border if selected_glass else None,
                top_highlight=blend(
                    theme.date_selected_gradient_start,
                    theme.control_highlight,
                    0.18,
                ) if selected_glass else None,
            )
        elif self.today:
            draw_calendar_date_state(
                self,
                center_x - DATE_STATE_HALF_WIDTH,
                DATE_STATE_TOP,
                center_x + DATE_STATE_HALF_WIDTH,
                DATE_STATE_BOTTOM,
                fill=theme.date_today_background,
                border=theme.date_today_background if self.ddl else theme.date_today_border,
                radius=theme.metrics.date_radius,
                top_highlight=blend(theme.date_today_background, theme.control_highlight, 0.34) if theme.style == "aero" else None,
            )

        if self.ddl:
            # DDL owns an outer ring, leaving selected/today layers and the
            # holiday baseline untouched. Aero gets one restrained inner glint.
            draw_calendar_date_ring(
                self,
                center_x - DATE_RING_HALF_WIDTH,
                DATE_RING_TOP,
                center_x + DATE_RING_HALF_WIDTH,
                DATE_RING_BOTTOM,
                color=theme.ddl_indicator,
                radius=theme.metrics.date_radius + 2,
                inner_highlight=theme.ddl_indicator_highlight if theme.style == "aero" else None,
            )

        if self.today and (self.selected or self.ddl):
            draw_calendar_today_accent(
                self,
                center_x - DATE_STATE_HALF_WIDTH,
                DATE_STATE_TOP,
                center_x + DATE_STATE_HALF_WIDTH,
                DATE_STATE_BOTTOM,
                color=theme.date_selected_today,
                highlight=blend(theme.date_selected_today, theme.control_highlight, 0.42) if theme.style == "aero" else None,
            )

        if self.date_status in ("leave", "holiday"):
            status_color = theme.date_leave_indicator if self.date_status == "leave" else theme.date_holiday_indicator
            self.create_line(
                center_x - DATE_STATE_HALF_WIDTH + 2,
                3,
                center_x - DATE_STATE_HALF_WIDTH + 7,
                3,
                fill=status_color,
                width=2,
                tags="date_marker",
            )

        if self.selected:
            color = theme.header_text if theme.style == "aero" else theme.text_on_accent
        elif not self.in_month:
            color = theme.date_other_month
        elif self.column >= 5:
            color = theme.date_weekend_text
        else:
            color = theme.date_text
        weight = "bold" if self.today or self.selected else "normal"
        self.create_text(center_x, date_y, text=str(self.day.day), fill=color, font=(FONT, 9, weight), tags="date_text")

        if self.holiday and self.in_month:
            holiday_color = {
                "day_off": theme.date_leave_indicator if self.holiday.name == "请假" else theme.date_holiday_indicator,
                "workday": theme.holiday_workday,
            }.get(self.holiday.kind, theme.holiday_festival)
            if self.selected:
                holiday_color = blend(
                    holiday_color,
                    theme.date_selected_border,
                    0.22 if theme.style == "flat" else 0.15,
                )
            self.create_text(
                center_x,
                27,
                text=self.holiday.short_name,
                fill=holiday_color,
                font=(
                    FONT,
                    7 if len(self.holiday.short_name) <= 4 else 6,
                    "normal" if theme.style == "flat" else "bold" if self.holiday.kind != "festival" else "normal",
                ),
                tags="date_text",
            )

        if self.event_colors:
            gap = 7
            start = center_x - (len(self.event_colors) - 1) * gap / 2
            indicator_bottom = self.logical_height() - 1
            for index, event_color in enumerate(self.event_colors):
                x = start + index * gap
                self.create_rectangle(
                    x - 2,
                    indicator_bottom - 1,
                    x + 2,
                    indicator_bottom,
                    fill=event_color,
                    outline="",
                    tags="date_event",
                )


class EventEditor(tk.Toplevel):
    WIDTH = 400
    HEIGHT = 590

    def __init__(self, master: "CalendarApp", selected: date, event: Optional[Event] = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.event = event
        self.title("编辑日程" if event else "新建日程")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")

        due = event.due_at if event else datetime.combine(selected, datetime.min.time()).replace(hour=23, minute=59)
        self.title_var = tk.StringVar(value=event.title if event else "")
        self.date_var = tk.StringVar(value=due.strftime("%Y-%m-%d"))
        self.time_var = tk.StringVar(value=due.strftime("%H:%M") if event and event.has_time else "")
        self.duration_var = tk.StringVar(value=str(event.duration_days if event else 1))
        self.skip_non_working_var = tk.BooleanVar(value=event.skip_non_working_days if event else False)
        self.end_as_ddl_var = tk.BooleanVar(value=event.end_as_ddl if event else False)
        self.event_type_var = tk.StringVar(value=event.event_type if event else "general")
        self.color_var = tk.StringVar(value=event.color if event else COLORS["海盐蓝"])
        reminder_value = event.reminder if event else None
        reminder_label = next((label for label, value in REMINDERS.items() if value == reminder_value), "不提醒")
        self.reminder_var = tk.StringVar(value=reminder_label)
        self._drag_origin: Optional[tuple[int, int, int, int]] = None
        self.color_canvases: list[tuple[tk.Canvas, str]] = []
        self.event_type_radios: list[tuple[tk.Radiobutton, str]] = []

        shell = tk.Frame(self, bg=CARD, padx=20, pady=0)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        title = tk.Label(header, text="编辑日程" if event else "新建日程", bg=CARD, fg=INK, font=(FONT, 12, "bold"))
        title.pack(side="left", pady=12)
        close = button_label(header, "×", self.close, width=2, bg=CARD, fg=SUBTLE, hover=HOVER, font_size=13)
        close.pack(side="right", pady=8)
        for widget in (header, title):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

        tk.Label(shell, text="要做什么？", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        self.title_entry = tk.Entry(
            shell,
            textvariable=self.title_var,
            bg=FIELD_BACKGROUND,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 11),
        )
        self.title_entry.pack(fill="x", pady=(4, 12), ipady=7)

        date_row = tk.Frame(shell, bg=CARD)
        date_row.pack(fill="x")
        date_col = tk.Frame(date_row, bg=CARD)
        date_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
        time_col = tk.Frame(date_row, bg=CARD, width=scale_px(115))
        time_col.pack(side="right", padx=(6, 0))
        self._field_label(date_col, "日期与持续时间")
        self._flat_entry(date_col, self.date_var).pack(fill="x", ipady=6)
        self._field_label(time_col, "时间（可选）")
        self._flat_entry(time_col, self.time_var, width=10).pack(fill="x", ipady=6)

        shortcuts = tk.Frame(shell, bg=CARD)
        shortcuts.pack(fill="x", pady=(7, 10))
        for label, offset in (("今天", 0), ("明天", 1), ("一周后", 7)):
            chip = tk.Label(shortcuts, text=label, bg=CONTROL_BACKGROUND, fg=SUBTLE, font=(FONT, 8), padx=8, pady=3, cursor="hand2")
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-1>", lambda _event, days=offset: self.date_var.set((date.today() + timedelta(days=days)).isoformat()))
        tk.Label(shortcuts, text="持续", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(side="left", padx=(8, 3))
        duration = tk.Spinbox(
            shortcuts,
            from_=1,
            to=365,
            textvariable=self.duration_var,
            width=4,
            justify="center",
            bg=FIELD_BACKGROUND,
            fg=INK,
            buttonbackground=CONTROL_BACKGROUND,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 8),
        )
        duration.pack(side="left", ipady=2)
        tk.Label(shortcuts, text="天", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(side="left", padx=(3, 0))

        span_options = tk.Frame(
            shell,
            bg=CONTROL_BACKGROUND,
            padx=master.dpi.px(8),
            pady=master.dpi.px(5),
            highlightthickness=master.dpi.px(1),
            highlightbackground=master.theme.control_border,
        )
        span_options.pack(fill="x", pady=(0, 10))
        tk.Checkbutton(
            span_options,
            text="跳过节假日和请假日",
            variable=self.skip_non_working_var,
            bg=CONTROL_BACKGROUND,
            activebackground=CONTROL_BACKGROUND,
            fg=SUBTLE,
            activeforeground=INK,
            selectcolor=FIELD_BACKGROUND,
            font=(FONT, 8),
            cursor="hand2",
            highlightthickness=0,
        ).pack(side="left")
        self.end_as_ddl_check = tk.Checkbutton(
            span_options,
            text="最后一天作为 DDL",
            variable=self.end_as_ddl_var,
            bg=CONTROL_BACKGROUND,
            activebackground=CONTROL_BACKGROUND,
            fg=SUBTLE,
            activeforeground=INK,
            disabledforeground=FAINT,
            selectcolor=FIELD_BACKGROUND,
            font=(FONT, 8),
            cursor="hand2",
            highlightthickness=0,
        )
        self.end_as_ddl_check.pack(side="left", padx=(8, 0))
        self.duration_var.trace_add("write", lambda *_args: self._update_end_as_ddl_option())

        self._field_label(shell, "事项类型")
        event_type_row = tk.Frame(shell, bg=CARD)
        event_type_row.pack(fill="x", pady=(4, 10))
        for event_type, event_type_text in EVENT_TYPE_OPTIONS:
            radio = tk.Radiobutton(
                event_type_row,
                text=event_type_text,
                variable=self.event_type_var,
                value=event_type,
                indicatoron=False,
                bg=CONTROL_BACKGROUND,
                fg=SUBTLE,
                selectcolor=ACCENT_SOFT,
                activebackground=ACCENT_SOFT,
                activeforeground=INK,
                relief="flat",
                bd=0,
                font=(FONT, 8),
                padx=11,
                pady=4,
                cursor="hand2",
                command=self._event_type_changed,
            )
            radio.pack(side="left", padx=(0, 6))
            self.event_type_radios.append((radio, event_type))
        self._draw_event_type_controls()
        self._update_end_as_ddl_option()

        reminder_row = tk.Frame(shell, bg=CARD)
        reminder_row.pack(fill="x", pady=(0, 10))
        reminder_col = tk.Frame(reminder_row, bg=CARD)
        reminder_col.pack(side="left", fill="x", expand=True)
        self._field_label(reminder_col, "提醒（需填写时间）")
        reminder_box = ttk.Combobox(
            reminder_col,
            textvariable=self.reminder_var,
            values=tuple(REMINDERS),
            state="readonly",
            font=(FONT, 9),
        )
        reminder_box.pack(fill="x", pady=(4, 0))

        self._field_label(shell, "颜色")
        color_row = tk.Frame(shell, bg=CARD)
        color_row.pack(fill="x", pady=(3, 9))
        color_values = list(COLORS.values())
        if self.color_var.get() not in color_values:
            color_values.append(self.color_var.get())
        for color in color_values:
            swatch = LogicalCanvas(
                color_row,
                dpi=master.dpi,
                width=28,
                height=28,
                bg=CARD,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
            )
            swatch.pack(side="left", padx=(0, 8))
            swatch.bind("<Button-1>", lambda _event, value=color: self._choose_color(value))
            self.color_canvases.append((swatch, color))
        self._draw_colors()

        self._field_label(shell, "备注（可选）")
        self.notes = tk.Text(
            shell,
            width=38,
            height=3,
            bg=FIELD_BACKGROUND,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            wrap="word",
            font=(FONT, 9),
        )
        self.notes.pack(fill="x", pady=(4, 12))
        if event:
            self.notes.insert("1.0", event.notes)

        actions = tk.Frame(shell, bg=CARD)
        actions.pack(fill="x")
        if event:
            delete = tk.Button(actions, text="删除", command=self.delete, bg=DANGER_SOFT, fg=DANGER, relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
            delete.pack(side="left")
        save = tk.Button(actions, text="保存", command=self.save, bg=ACCENT, fg=current_theme().text_on_accent, activebackground=ACCENT_HOVER, activeforeground=current_theme().text_on_accent, relief="flat", bd=0, padx=20, pady=7, font=(FONT, 9, "bold"), cursor="hand2")
        save.pack(side="right")
        cancel = tk.Button(actions, text="取消", command=self.close, bg=CONTROL_BACKGROUND, fg=SUBTLE, relief="flat", bd=0, padx=14, pady=7, cursor="hand2")
        cancel.pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Control-Return>", lambda _event: self.save())
        self.bind("<Control-s>", lambda _event: self.save())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_idletasks()
        self._center_near_master()
        self.after_idle(self._present)
        self.after(80, self._present)
        self.after(260, self._present)

    def _present(self) -> None:
        if not self.winfo_exists():
            return
        self.master_app.present_overlay(self)
        self.title_entry.focus_force()

    @staticmethod
    def _field_label(parent: tk.Widget, text: str) -> None:
        tk.Label(parent, text=text, bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")

    @staticmethod
    def _flat_entry(parent: tk.Widget, variable: tk.StringVar, width: int = 18) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            bg=FIELD_BACKGROUND,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 9),
        )

    def _choose_color(self, color: str) -> None:
        self.color_var.set(color)
        self._draw_colors()

    def _draw_event_type_controls(self) -> None:
        selected = self.event_type_var.get()
        theme = self.master_app.theme
        for radio, event_type in self.event_type_radios:
            accent = event_type_color(theme, event_type)
            is_selected = event_type == selected
            background = blend(accent, theme.panel_secondary, 0.80) if is_selected else theme.control_background
            radio.configure(
                bg=background,
                fg=accent if is_selected else theme.text_secondary,
                selectcolor=background,
                activebackground=blend(accent, theme.panel_secondary, 0.86),
                activeforeground=theme.text_primary,
                highlightthickness=1 if is_selected else 0,
                highlightbackground=blend(accent, theme.panel_secondary, 0.55),
            )

    def _event_type_changed(self) -> None:
        self._draw_event_type_controls()
        self._update_end_as_ddl_option()

    def _update_end_as_ddl_option(self) -> None:
        try:
            duration_days = int(self.duration_var.get())
        except ValueError:
            self.end_as_ddl_check.configure(state="disabled", cursor="arrow")
            return
        eligible = duration_days > 1 and self.event_type_var.get() != "ddl"
        self.end_as_ddl_check.configure(
            state="normal" if eligible else "disabled",
            cursor="hand2" if eligible else "arrow",
        )
        if not eligible:
            self.end_as_ddl_var.set(False)

    def _draw_colors(self) -> None:
        selected = self.color_var.get()
        for canvas, color in self.color_canvases:
            canvas.delete("all")
            if color == selected:
                canvas.create_oval(2, 2, 26, 26, outline=INK, width=1.5)
                canvas.create_oval(6, 6, 22, 22, fill=color, outline="")
            else:
                canvas.create_oval(5, 5, 23, 23, fill=color, outline="")

    def _center_near_master(self) -> None:
        center_toplevel(self, self.master_app, self.WIDTH, self.HEIGHT, y_offset=10)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())

    def _drag(self, event: tk.Event) -> None:
        if not self._drag_origin:
            return
        start_x, start_y, win_x, win_y = self._drag_origin
        self.geometry(position_at(win_x + event.x_root - start_x, win_y + event.y_root - start_y))

    def save(self) -> None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showinfo(APP_NAME, "请先填写日程标题。", parent=self)
            self.title_entry.focus_set()
            return
        time_text = self.time_var.get().strip()
        try:
            due, has_time = parse_event_due(self.date_var.get(), time_text)
        except ValueError:
            messagebox.showinfo(APP_NAME, "日期或时间格式不正确。\n日期请使用 YYYY-MM-DD；时间可以留空，填写时请使用 HH:MM。", parent=self)
            return
        try:
            duration_days = int(self.duration_var.get().strip())
            if not 1 <= duration_days <= 365:
                raise ValueError
        except ValueError:
            messagebox.showinfo(APP_NAME, "持续天数请输入 1～365 之间的整数。", parent=self)
            return
        item = Event(
            id=self.event.id if self.event else str(uuid.uuid4()),
            title=title,
            due=due.isoformat(timespec="minutes"),
            has_time=has_time,
            duration_days=duration_days,
            skip_non_working_days=self.skip_non_working_var.get(),
            end_as_ddl=self.end_as_ddl_var.get(),
            color=self.color_var.get(),
            event_type=self.event_type_var.get(),
            reminder=REMINDERS[self.reminder_var.get()] if has_time else None,
            notes=self.notes.get("1.0", "end").strip(),
            done=self.event.done if self.event else False,
            created_at=self.event.created_at if self.event else "",
        )
        self.master_app.upsert_event(item)
        self.close()

    def delete(self) -> None:
        if self.event and messagebox.askyesno(APP_NAME, f"确定删除“{self.event.title}”？", parent=self):
            self.master_app.delete_event(self.event.id)
            self.close()

    def close(self) -> None:
        if self.master_app.editor_window is self:
            self.master_app.editor_window = None
        self.destroy()
        detail = self.master_app.day_detail_window
        if detail and detail.winfo_exists():
            detail.refresh()
            self.master_app.after(60, lambda: self.master_app.present_overlay(detail))
        else:
            self.master_app.after(120, self.master_app.apply_window_mode)


class RoutineEditor(tk.Toplevel):
    WIDTH = 360
    HEIGHT = 420

    def __init__(self, master: "CalendarApp", item: Optional[RoutineItem] = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.item = item
        self.title("编辑习惯清单项" if item else "新增习惯清单项")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")
        self.title_var = tk.StringVar(value=item.title if item else "")
        self.kind_var = tk.StringVar(value=item.kind if item else "habit")
        self.color_var = tk.StringVar(value=item.color if item else COLORS["薄荷绿"])
        default_reminder_time = normalize_reminder_time(
            item.reminder_time if item else master.store.settings.get("routine_reminder_time", "09:00")
        ) or "09:00"
        self.reminder_enabled_var = tk.BooleanVar(value=item.reminder_enabled if item else False)
        self.reminder_time_var = tk.StringVar(value=default_reminder_time)
        self.color_canvases: list[tuple[tk.Canvas, str]] = []

        shell = tk.Frame(self, bg=CARD, padx=20)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, height=54)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="编辑习惯清单项" if item else "新增习惯清单项", bg=CARD, fg=INK, font=(FONT, 12, "bold")).pack(side="left", pady=13)
        button_label(header, "×", self.close, width=2, bg=CARD, font_size=13).pack(side="right", pady=8)

        tk.Label(shell, text="每天要做什么？", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        self.title_entry = tk.Entry(
            shell,
            textvariable=self.title_var,
            bg=FIELD_BACKGROUND,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 11),
        )
        self.title_entry.pack(fill="x", pady=(5, 14), ipady=7)

        tk.Label(shell, text="类型", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        kind_row = tk.Frame(shell, bg=CARD)
        kind_row.pack(fill="x", pady=(5, 6))
        for text, value in (("习惯 · 每个工作日重置", "habit"), ("待办 · 完成一次即结束", "todo")):
            tk.Radiobutton(
                kind_row,
                text=text,
                variable=self.kind_var,
                value=value,
                indicatoron=False,
                bg=CONTROL_BACKGROUND,
                fg=SUBTLE,
                selectcolor=ACCENT_SOFT,
                activebackground=ACCENT_SOFT,
                activeforeground=INK,
                relief="flat",
                bd=0,
                font=(FONT, 8),
                padx=10,
                pady=5,
                cursor="hand2",
            ).pack(side="left", padx=(0, 7))
        tk.Label(shell, text="习惯只记录当天完成；待办完成后不会在次日出现。", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(anchor="w", pady=(0, 14))

        tk.Label(shell, text="提醒", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        reminder_row = tk.Frame(
            shell,
            bg=CONTROL_BACKGROUND,
            padx=master.dpi.px(9),
            pady=master.dpi.px(6),
            highlightthickness=master.dpi.px(1),
            highlightbackground=master.theme.control_border,
        )
        reminder_row.pack(fill="x", pady=(5, 14))
        tk.Checkbutton(
            reminder_row,
            text="开启提醒",
            variable=self.reminder_enabled_var,
            command=self._update_reminder_controls,
            bg=CONTROL_BACKGROUND,
            activebackground=CONTROL_BACKGROUND,
            fg=INK,
            activeforeground=INK,
            selectcolor=FIELD_BACKGROUND,
            font=(FONT, 9),
            cursor="hand2",
            highlightthickness=0,
        ).pack(side="left")
        self.reminder_time_controls = tk.Frame(reminder_row, bg=CONTROL_BACKGROUND)
        tk.Label(
            self.reminder_time_controls,
            text="提醒时间",
            bg=CONTROL_BACKGROUND,
            fg=SUBTLE,
            font=(FONT, 8),
        ).pack(side="left", padx=(12, 5))
        reminder_time_entry = tk.Entry(
            self.reminder_time_controls,
            textvariable=self.reminder_time_var,
            width=7,
            justify="center",
            bg=FIELD_BACKGROUND,
            fg=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 9),
        )
        reminder_time_entry.pack(side="left", ipady=4)
        self._update_reminder_controls()

        tk.Label(shell, text="颜色", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        color_row = tk.Frame(shell, bg=CARD)
        color_row.pack(fill="x", pady=(5, 20))
        color_values = list(COLORS.values())
        if self.color_var.get() not in color_values:
            color_values.append(self.color_var.get())
        for color in color_values:
            swatch = LogicalCanvas(
                color_row,
                dpi=master.dpi,
                width=30,
                height=26,
                bg=CARD,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
            )
            swatch.pack(side="left", padx=(0, 7))
            swatch.bind("<Button-1>", lambda _event, value=color: self._choose_color(value))
            self.color_canvases.append((swatch, color))
        self._draw_colors()

        actions = tk.Frame(shell, bg=CARD)
        actions.pack(fill="x", side="bottom", pady=(0, 17))
        if item:
            tk.Button(actions, text="删除", command=self.delete, bg=DANGER_SOFT, fg=DANGER, relief="flat", bd=0, padx=14, pady=7, cursor="hand2").pack(side="left")
        tk.Button(actions, text="保存", command=self.save, bg=ACCENT, fg=current_theme().text_on_accent, activebackground=ACCENT_HOVER, activeforeground=current_theme().text_on_accent, relief="flat", bd=0, padx=20, pady=7, font=(FONT, 9, "bold"), cursor="hand2").pack(side="right")
        tk.Button(actions, text="取消", command=self.close, bg=CONTROL_BACKGROUND, fg=SUBTLE, relief="flat", bd=0, padx=14, pady=7, cursor="hand2").pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Control-Return>", lambda _event: self.save())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_idletasks()
        center_toplevel(self, master, self.WIDTH, self.HEIGHT, y_offset=48)
        self.after_idle(self._present)

    def _present(self) -> None:
        if self.winfo_exists():
            self.master_app.present_overlay(self)
            self.title_entry.focus_force()

    def _choose_color(self, color: str) -> None:
        self.color_var.set(color)
        self._draw_colors()

    def _update_reminder_controls(self) -> None:
        if self.reminder_enabled_var.get():
            if not self.reminder_time_controls.winfo_ismapped():
                self.reminder_time_controls.pack(side="right")
        else:
            self.reminder_time_controls.pack_forget()

    def _draw_colors(self) -> None:
        selected = self.color_var.get()
        for canvas, color in self.color_canvases:
            canvas.delete("all")
            canvas.create_rectangle(2, 2, 28, 24, fill=CARD, outline=INK if color == selected else CARD, width=2)
            canvas.create_rectangle(6, 6, 24, 20, fill=color, outline="")

    def save(self) -> None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showinfo(APP_NAME, "请先填写习惯清单内容。", parent=self)
            self.title_entry.focus_set()
            return
        reminder_time = normalize_reminder_time(self.reminder_time_var.get())
        if self.reminder_enabled_var.get() and reminder_time is None:
            messagebox.showinfo(APP_NAME, "提醒时间请使用 HH:MM，例如 09:00。", parent=self)
            return
        item = RoutineItem(
            id=self.item.id if self.item else str(uuid.uuid4()),
            title=title,
            kind=self.kind_var.get(),
            color=self.color_var.get(),
            created_on=self.item.created_on if self.item else date.today().isoformat(),
            completed_on=self.item.completed_on if self.item else None,
            habit_done=list(self.item.habit_done) if self.item else [],
            enabled=self.item.enabled if self.item else True,
            reminder_enabled=self.reminder_enabled_var.get(),
            reminder_time=reminder_time,
        )
        self.master_app.store.upsert_routine(item)
        self.master_app.render()
        if self.master_app.routine_manager and self.master_app.routine_manager.winfo_exists():
            self.master_app.routine_manager.refresh()
        self.close()

    def delete(self) -> None:
        if self.item and messagebox.askyesno(APP_NAME, f"确定删除“{self.item.title}”？", parent=self):
            self.master_app.store.delete_routine(self.item.id)
            self.master_app.render()
            if self.master_app.routine_manager and self.master_app.routine_manager.winfo_exists():
                self.master_app.routine_manager.refresh()
            self.close()

    def close(self) -> None:
        if self.master_app.routine_editor is self:
            self.master_app.routine_editor = None
        self.destroy()
        manager = self.master_app.routine_manager
        if manager and manager.winfo_exists():
            self.master_app.after(60, lambda: self.master_app.present_overlay(manager))
        elif self.master_app.day_detail_window and self.master_app.day_detail_window.winfo_exists():
            detail = self.master_app.day_detail_window
            detail.refresh()
            self.master_app.after(60, lambda: self.master_app.present_overlay(detail))
        else:
            self.master_app.after(120, self.master_app.apply_window_mode)


class RoutineManager(tk.Toplevel):
    WIDTH = 400
    HEIGHT = 500

    def __init__(self, master: "CalendarApp") -> None:
        super().__init__(master)
        self.master_app = master
        self.title("习惯清单")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=18, pady=10)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left")
        tk.Label(title_box, text="习惯清单", bg=CARD, fg=INK, font=(FONT, 13, "bold"), anchor="w").pack(anchor="w")
        tk.Label(title_box, text="习惯每天回来，待办完成一次即结束", bg=CARD, fg=FAINT, font=(FONT, 8), anchor="w").pack(anchor="w")
        button_label(header, "×", self.close, width=2, bg=CARD, font_size=13).pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        list_shell = tk.Frame(shell, bg=CARD, padx=14, pady=10)
        list_shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(list_shell, bg=CARD, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_shell, orient="vertical", command=self.canvas.yview)
        self.list_inner = tk.Frame(self.canvas, bg=CARD)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.list_inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.list_window, width=event.width))
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(int(-event.delta / 120), "units"))

        footer = tk.Frame(shell, bg=CARD, padx=16, pady=12)
        footer.pack(fill="x")
        tk.Button(footer, text="＋ 新增习惯清单项", command=lambda: self.master_app.open_routine_editor(), bg=ACCENT, fg=current_theme().text_on_accent, activebackground=ACCENT_HOVER, activeforeground=current_theme().text_on_accent, relief="flat", bd=0, padx=16, pady=7, font=(FONT, 9, "bold"), cursor="hand2").pack(side="right")
        tk.Label(footer, text="仅在工作日显示", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")

        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.update_idletasks()
        center_toplevel(self, master, self.WIDTH, self.HEIGHT, y_offset=24)
        self.after_idle(lambda: master.present_overlay(self))

    def refresh(self) -> None:
        for child in self.list_inner.winfo_children():
            child.destroy()
        if not self.master_app.store.routines:
            tk.Label(self.list_inner, text="还没有习惯清单\n点击下方按钮添加第一项", bg=CARD, fg=FAINT, font=(FONT, 9), justify="center", pady=48).pack(fill="x")
            return
        for item in self.master_app.store.routines:
            completed = item.kind == "todo" and bool(item.completed_on)
            row_background = current_theme().card_done_background if completed else CARD_MUTED
            row = tk.Frame(self.list_inner, bg=row_background, highlightthickness=1, highlightbackground=CARD_BORDER, cursor="hand2")
            row.pack(fill="x", pady=(0, 7), padx=1)
            tk.Frame(row, bg=current_theme().event_done if completed else item.color, width=3).pack(side="left", fill="y")
            content = tk.Frame(row, bg=row_background, padx=10, pady=8)
            content.pack(side="left", fill="both", expand=True)
            title = tk.Label(
                content,
                text=truncate(item.title, 28),
                bg=row_background,
                fg=current_theme().text_done if completed else INK,
                font=(FONT, 9, "overstrike" if completed else "normal"),
                anchor="w",
            )
            title.pack(fill="x")
            type_text = "习惯 · 每个工作日" if item.kind == "habit" else ("待办 · 已完成" if item.completed_on else "待办 · 等待完成")
            if item.reminder_enabled and item.reminder_time:
                type_text += f" · 提醒 {item.reminder_time}"
            meta = tk.Label(content, text=type_text, bg=row_background, fg=current_theme().text_done if completed else SUBTLE, font=(FONT, 8), anchor="w")
            meta.pack(fill="x", pady=(2, 0))
            more = tk.Label(row, text="›", bg=row_background, fg=FAINT, font=(FONT, 13), width=3, cursor="hand2")
            more.pack(side="right", fill="y")
            for widget in (row, content, title, meta, more):
                widget.bind("<Button-1>", lambda _event, entry=item: self.master_app.open_routine_editor(entry))
                widget.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(int(-event.delta / 120), "units"))

    def close(self) -> None:
        if self.master_app.routine_editor and self.master_app.routine_editor.winfo_exists():
            self.master_app.routine_editor.close()
        if self.master_app.routine_manager is self:
            self.master_app.routine_manager = None
        self.destroy()
        detail = self.master_app.day_detail_window
        if detail and detail.winfo_exists():
            detail.refresh()
            self.master_app.after(60, lambda: self.master_app.present_overlay(detail))
        else:
            self.master_app.after(120, self.master_app.apply_window_mode)


class DayDetailDialog(tk.Toplevel):
    WIDTH = 420
    HEIGHT = 560

    def __init__(self, master: "CalendarApp", day: date) -> None:
        super().__init__(master)
        self.master_app = master
        self.day = day
        self.title("单日事项详情")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")
        self.status_var = tk.StringVar(value=master.store.date_status(day))

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=16, pady=10)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left", fill="x", expand=True)
        self.date_title = tk.Label(title_box, text="", bg=CARD, fg=INK, font=(FONT, 13, "bold"), anchor="w")
        self.date_title.pack(anchor="w")
        self.date_subtitle = tk.Label(title_box, text="", bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w")
        self.date_subtitle.pack(anchor="w")
        button_label(header, "×", self.close, width=2, bg=CARD, font_size=13).pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        status_row = tk.Frame(shell, bg=CONTROL_BACKGROUND, padx=14, pady=8)
        status_row.pack(fill="x")
        tk.Label(status_row, text="日期状态", bg=CONTROL_BACKGROUND, fg=SUBTLE, font=(FONT, 8)).pack(side="left", padx=(0, 8))
        for status, label in DATE_STATUS_LABELS.items():
            status_color = {
                "leave": master.theme.date_leave_indicator,
                "holiday": master.theme.date_holiday_indicator,
            }.get(status, master.theme.accent)
            tk.Radiobutton(
                status_row,
                text=label,
                variable=self.status_var,
                value=status,
                command=self._save_status,
                indicatoron=False,
                bg=CONTROL_BACKGROUND,
                fg=SUBTLE,
                selectcolor=blend(status_color, master.theme.panel_secondary, 0.78),
                activebackground=blend(status_color, master.theme.panel_secondary, 0.84),
                activeforeground=INK,
                relief="flat",
                bd=0,
                padx=9,
                pady=3,
                font=(FONT, 8),
                cursor="hand2",
            ).pack(side="left", padx=(0, 5))

        list_shell = tk.Frame(shell, bg=CARD, padx=12, pady=10)
        list_shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(list_shell, bg=CARD, bd=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(list_shell, orient="vertical", command=self.canvas.yview)
        self.list_inner = tk.Frame(self.canvas, bg=CARD)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.list_window, width=event.width))
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(int(-event.delta / 120), "units"))

        footer = tk.Frame(shell, bg=CARD, padx=14, pady=11)
        footer.pack(fill="x")
        ThemeButton(
            footer,
            self.master_app,
            "＋ 新增事项",
            self._add_event,
            width=102,
            height=32,
            font_size=9,
            surface_background=master.theme.schedule_card_background,
            accented=True,
        ).pack(side="right")
        ThemeButton(
            footer,
            self.master_app,
            "习惯清单",
            self.master_app.open_routine_manager,
            width=73,
            height=30,
            font_size=8,
            surface_background=master.theme.schedule_card_background,
        ).pack(side="left")

        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.update_idletasks()
        center_toplevel(self, master, self.WIDTH, self.HEIGHT, y_offset=12)
        self.after_idle(lambda: master.present_overlay(self))

    def set_day(self, day: date) -> None:
        self.day = day
        self.refresh()

    def _add_event(self) -> None:
        self.master_app.open_new_event(self.day)

    def refresh(self) -> None:
        if not self.winfo_exists():
            return
        self.status_var.set(self.master_app.store.date_status(self.day))
        self.date_title.configure(text=f"{self.day.year}年{self.day.month}月{self.day.day}日 · {WEEKDAYS[self.day.weekday()]}")
        system_holiday = holiday_for(self.day)
        custom_status = self.master_app.store.date_status(self.day)
        status_text = f"日期状态：{DATE_STATUS_LABELS[custom_status]}"
        if system_holiday:
            status_text += f" · 系统日历：{system_holiday.name}"
        self.date_subtitle.configure(text=status_text)
        for child in self.list_inner.winfo_children():
            child.destroy()
        items = self.master_app.store.agenda_items_on(self.day)
        if not items:
            tk.Label(
                self.list_inner,
                text="当天没有事项\n可点击下方按钮新增",
                bg=CARD,
                fg=FAINT,
                font=(FONT, 9),
                justify="center",
                pady=70,
            ).pack(fill="x")
        else:
            last_done: Optional[bool] = None
            for item in items:
                done = item.done if isinstance(item, Event) else item.is_done_on(self.day)
                if done != last_done:
                    tk.Label(
                        self.list_inner,
                        text="已完成" if done else "待处理",
                        bg=CARD,
                        fg=current_theme().text_muted,
                        font=(FONT, 8, "bold"),
                        anchor="w",
                    ).pack(fill="x", padx=2, pady=(5 if last_done is not None else 0, 5))
                    last_done = done
                if isinstance(item, Event):
                    self._build_event_row(item)
                else:
                    self._build_routine_row(item)
        self.after_idle(self._update_scrollbar)

    def _base_row(self, color: str, done: bool, command: Callable[[], None]) -> tuple[tk.Frame, tk.Frame, TaskCheck]:
        background = current_theme().card_done_background if done else current_theme().schedule_card_background
        row = tk.Frame(self.list_inner, bg=background, highlightthickness=1, highlightbackground=current_theme().schedule_card_border)
        row.pack(fill="x", pady=(0, 7), padx=1)
        stripe = tk.Frame(row, bg=current_theme().event_done if done else color, width=EVENT_STRIPE_WIDTH)
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        check = TaskCheck(
            row,
            self.master_app,
            done=done,
            background=background,
            command=command,
            height=54,
        )
        check.pack(side="left", fill="y", padx=(3, 0))
        content = tk.Frame(row, bg=background, padx=2, pady=7)
        content.pack(side="left", fill="both", expand=True)
        return row, content, check

    def _build_event_row(self, item: Event) -> None:
        row, content, check = self._base_row(
            event_stripe_color(current_theme(), item),
            item.done,
            lambda: self.master_app.toggle_done(item),
        )
        background = str(row.cget("bg"))
        title_row = tk.Frame(content, bg=background)
        title_row.pack(fill="x")
        title = tk.Label(
            title_row,
            text=truncate(item.title, 28),
            bg=background,
            fg=current_theme().text_done if item.done else current_theme().text_primary,
            font=(FONT, 9, "overstrike" if item.done else "normal"),
            anchor="w",
        )
        title.pack(side="left", fill="x", expand=True)
        badge_style = None if item.done else event_type_badge_style(current_theme(), item.event_type)
        badge = None
        if badge_style:
            badge_text, badge_background, badge_border = badge_style
            badge = tk.Label(
                title_row,
                text=EVENT_TYPE_LABELS[item.event_type],
                bg=badge_background,
                fg=badge_text,
                font=(FONT, 7),
                padx=4,
                pady=0,
                highlightthickness=1,
                highlightbackground=badge_border,
                cursor="hand2",
            )
            badge.pack(side="right", padx=(4, 0))
        end_date = self.master_app.store.event_end_date(item)
        overdue = self.master_app.store.is_event_overdue(item)
        meta_text = f"截止 {end_date.month}月{end_date.day}日 · {EVENT_TYPE_LABELS[item.event_type]}"
        if overdue:
            meta_text += " · 已逾期"
        tk.Label(
            content,
            text=meta_text,
            bg=background,
            fg=current_theme().danger if overdue else current_theme().schedule_time_text,
            font=(FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))
        actions = tk.Frame(row, bg=background)
        actions.pack(side="right", fill="y", padx=(2, 5))
        edit = tk.Label(actions, text="编辑", bg=background, fg=current_theme().text_secondary, font=(FONT, 8), cursor="hand2")
        edit.pack(side="left", padx=4)
        delete = tk.Label(actions, text="删除", bg=background, fg=current_theme().danger, font=(FONT, 8), cursor="hand2")
        delete.pack(side="left", padx=4)
        interactive_widgets = [row, content, title_row, title, edit]
        if badge:
            interactive_widgets.append(badge)
        for widget in interactive_widgets:
            widget.bind("<Button-1>", lambda _event: self.master_app.open_editor(item))
        delete.bind("<Button-1>", lambda _event: self.master_app._confirm_delete(item, parent=self))

    def _build_routine_row(self, item: RoutineItem) -> None:
        done = item.is_done_on(self.day)
        row, content, check = self._base_row(
            item.color,
            done,
            lambda: self.master_app.toggle_routine(item, self.day),
        )
        background = str(row.cget("bg"))
        title = tk.Label(
            content,
            text=truncate(item.title, 28),
            bg=background,
            fg=current_theme().text_done if done else current_theme().text_primary,
            font=(FONT, 9, "overstrike" if done else "normal"),
            anchor="w",
        )
        title.pack(fill="x")
        meta_text = "习惯 · 每个工作日" if item.kind == "habit" else "待办 · 完成一次即结束"
        if done:
            meta_text += " · 已完成"
        tk.Label(content, text=meta_text, bg=background, fg=current_theme().schedule_time_text, font=(FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))
        edit = tk.Label(row, text="编辑", bg=background, fg=current_theme().text_secondary, font=(FONT, 8), cursor="hand2", padx=8)
        edit.pack(side="right", fill="y")
        for widget in (row, content, title, edit):
            widget.bind("<Button-1>", lambda _event: self.master_app.open_routine_editor(item))

    def _save_status(self) -> None:
        self.master_app.store.set_date_status(self.day, self.status_var.get())
        self.master_app.render()

    def _update_scrollbar(self) -> None:
        self.list_inner.update_idletasks()
        bbox = self.canvas.bbox("all")
        needs_scroll = bool(bbox and bbox[3] > self.canvas.winfo_height())
        if needs_scroll and not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y")
        elif not needs_scroll and self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()

    def close(self) -> None:
        if self.master_app.day_detail_window is self:
            self.master_app.day_detail_window = None
        self.destroy()
        self.master_app.after(120, self.master_app.apply_window_mode)


class UpcomingDialog(tk.Toplevel):
    def __init__(self, master: "CalendarApp") -> None:
        super().__init__(master)
        self.master_app = master
        self.title("未来 7 天")
        self.configure(bg=BORDER)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(360)}x{scale_px(450)}")

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="未来 7 天", bg=CARD, fg=INK, font=(FONT, 13, "bold")).pack(side="left")
        close = button_label(header, "×", self.close, width=2, bg=CARD, font_size=13)
        close.pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        canvas = tk.Canvas(shell, bg=CARD, bd=0, highlightthickness=0, width=scale_px(336))
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD, padx=14, pady=10)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        events = master.store.upcoming(7, include_overdue=True)
        if not events:
            tk.Label(inner, text="未来一周没有未完成的日程", bg=CARD, fg=FAINT, font=(FONT, 9), pady=40).pack()
        last_day: Optional[date] = None
        for item in events:
            start_date = master.store.event_start_date(item)
            if start_date != last_day:
                last_day = start_date
                day_text = "今天" if last_day == date.today() else f"{last_day.month}月{last_day.day}日 · {WEEKDAYS[last_day.weekday()]}"
                tk.Label(inner, text=day_text, bg=CARD, fg=SUBTLE, font=(FONT, 8, "bold"), anchor="w").pack(fill="x", pady=(8, 4))
            row = tk.Frame(inner, bg=CARD_MUTED, cursor="hand2", padx=8, pady=7)
            row.pack(fill="x", pady=2)
            tk.Frame(row, bg=item.color, width=4).pack(side="left", fill="y", padx=(0, 8))
            title = tk.Label(row, text=truncate(item.title, 22), bg=CARD_MUTED, fg=INK, font=(FONT, 9), anchor="w")
            title.pack(side="left", fill="x", expand=True)
            overdue = master.store.is_event_overdue(item)
            if overdue:
                when = "逾期"
            else:
                when = item.due_at.strftime("%H:%M") if item.has_time else "无具体时间"
                if item.duration_days > 1:
                    when = f"{when} · {item.duration_days}天"
            meta = tk.Label(row, text=when, bg=CARD_MUTED, fg=DANGER if overdue else SUBTLE, font=(FONT, 8))
            meta.pack(side="right")
            for widget in (row, title, meta):
                widget.bind("<Button-1>", lambda _event, event=item: self._edit(event))

        self.bind("<Escape>", lambda _event: self.close())
        self.update_idletasks()
        center_toplevel(self, master, 360, 450, y_offset=40)
        self.after_idle(lambda: self.master_app.present_overlay(self))

    def _edit(self, event: Event) -> None:
        self.close()
        self.master_app.open_editor(event)

    def close(self) -> None:
        self.destroy()
        self.master_app.after(120, self.master_app.apply_window_mode)


class UpdateProgressDialog(tk.Toplevel):
    def __init__(self, master: "CalendarApp", status: str) -> None:
        super().__init__(master)
        self.title("检查更新")
        self.configure(bg=BORDER)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{scale_px(340)}x{scale_px(138)}")
        shell = tk.Frame(self, bg=CARD, padx=18, pady=14)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(shell, text="桌面月历更新", bg=CARD, fg=INK, font=(FONT, 11, "bold"), anchor="w").pack(fill="x")
        self.status_label = tk.Label(shell, text=status, bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w")
        self.status_label.pack(fill="x", pady=(7, 8))
        self.progress = ttk.Progressbar(shell, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x")
        self.progress.start(12)
        self.update_idletasks()
        center_toplevel(self, master, 340, 138, y_offset=90)
        self.after_idle(lambda: master.present_overlay(self))

    def set_status(self, status: str) -> None:
        if self.winfo_exists():
            self.status_label.configure(text=status)

    def set_progress(self, value: int) -> None:
        if not self.winfo_exists():
            return
        if str(self.progress.cget("mode")) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress.configure(value=value)


class CalendarApp(tk.Tk):
    def __init__(self, store: Optional[Store] = None, instance: Optional[SingleInstance] = None) -> None:
        super().__init__()
        self.dpi = DpiManager(self)
        self.store = store or Store()
        self.instance_guard = instance
        self.theme_name = normalize_theme_name(self.store.settings.get("theme"))
        self.store.settings["theme"] = self.theme_name
        self.theme = get_theme(self.theme_name)
        activate_theme(self.theme)
        self.selected = date.today()
        self.shown_year = self.selected.year
        self.shown_month = self.selected.month
        self.day_cells: list[DayCell] = []
        self.agenda_open = bool(self.store.settings.get("agenda_open", True))
        self.window_mode = self.store.settings.get("window_mode", "desktop")
        if self.window_mode not in ("desktop", "pinned"):
            self.window_mode = "desktop"
        self.quick_placeholder_active = True
        self._drag_origin: Optional[tuple[int, int, int, int]] = None
        self._lower_job: Optional[str] = None
        self.notification_windows: list[tk.Toplevel] = []
        self.overlay_windows: list[tk.Toplevel] = []
        self.editor_window: Optional[EventEditor] = None
        self.routine_manager: Optional[RoutineManager] = None
        self.routine_editor: Optional[RoutineEditor] = None
        self.day_detail_window: Optional[DayDetailDialog] = None
        self.update_dialog: Optional[UpdateProgressDialog] = None
        self.update_busy = False
        self._dpi_check_job: Optional[str] = None
        self._dpi_rebuilding = False
        self.show_holidays = bool(self.store.settings.get("show_holidays", True))
        self.quick_color = COLORS["海盐蓝"]
        self.quick_event_type = "general"
        self.desktop_session_active = False
        self._window_ready = False
        self.tray_icon: Optional[TrayIcon] = None
        self.tray_actions: queue.Queue[str] = queue.Queue()

        self.title(APP_NAME)
        icon_path = resource_path("assets/calendar.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.day_selected_image = self._load_image("assets/day_selected.png")
        self.day_today_image = self._load_image("assets/day_today.png")
        self.configure(bg=self.theme.window_shadow)
        self.overrideredirect(True)
        if not self.store.settings.get("crisp_text_migrated_v1", False):
            self.store.settings["opacity"] = 1.0
            self.store.settings["crisp_text_migrated_v1"] = True
            try:
                self.store.save()
            except OSError:
                # Keep the crisp in-memory default even if a portable/read-only
                # launch cannot persist the one-time migration yet.
                pass
        try:
            opacity = min(1.0, max(0.90, float(self.store.settings.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        self._apply_theme_opacity(opacity)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._configure_style()
        self._build_ui()
        self._set_initial_geometry()
        self._bind_shortcuts()
        self.bind("<Configure>", self._schedule_dpi_check, add="+")
        self.bind("<FocusIn>", self._schedule_dpi_check, add="+")
        self.render()
        self.after(80, self._finish_window_setup)
        self.after(220, self._start_tray_icon)
        self.after(250, self._poll_tray_actions)
        self.after(1200, self.check_reminders)
        if self.store.load_error:
            self.after(250, lambda: messagebox.showwarning(APP_NAME, f"日历数据读取失败，已先打开空日历。\n\n{self.store.load_error}", parent=self))

    @staticmethod
    def _load_image(relative: str) -> Optional[tk.PhotoImage]:
        path = resource_path(relative)
        if not path.exists():
            return None
        try:
            return tk.PhotoImage(file=str(path))
        except tk.TclError:
            return None

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            padding=self.dpi.px(4),
            font=(FONT, 9),
            fieldbackground=self.theme.input_background,
            background=self.theme.control_background,
            foreground=self.theme.text_primary,
            bordercolor=self.theme.input_border,
            arrowcolor=self.theme.control_text,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.theme.input_background)],
            selectbackground=[("readonly", self.theme.input_background)],
            selectforeground=[("readonly", self.theme.text_primary)],
        )

    def _apply_theme_opacity(self, value: Optional[float] = None) -> None:
        if value is None:
            try:
                value = float(self.store.settings.get("opacity", 1.0))
            except (TypeError, ValueError):
                value = 1.0
        value = min(1.0, max(0.90, value))
        # Tk applies alpha to every child uniformly. Aero therefore uses an
        # opaque composite so desktop content cannot ghost through the body.
        self.attributes("-alpha", 1.0 if self.theme.style == "aero" else value)

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        log_exception(exc_type, exc_value, exc_traceback)
        try:
            messagebox.showerror(APP_NAME, f"操作没有完成，错误已经记录。\n\n{exc_value}", parent=self)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        theme = self.theme
        dp = self.dpi.px
        header_height = 55 if theme.style == "aero" else 56
        weekday_pady = 1 if theme.style == "aero" else 2
        self.day_cells = []
        self.configure(bg=theme.window_shadow)
        self.window_frame = tk.Frame(self, bg=theme.window_border_outer)
        self.window_frame.pack(fill="both", expand=True, padx=dp(theme.metrics.shadow_depth), pady=dp(theme.metrics.shadow_depth))
        self.inner_frame = tk.Frame(self.window_frame, bg=theme.window_border_inner)
        self.inner_frame.pack(fill="both", expand=True, padx=dp(theme.metrics.outer_border_width), pady=dp(theme.metrics.outer_border_width))
        self.shell = tk.Frame(
            self.inner_frame,
            bg=theme.panel_background,
            highlightthickness=dp(1) if theme.style == "aero" else 0,
            highlightbackground=theme.window_background,
        )
        self.shell.pack(fill="both", expand=True, padx=dp(theme.metrics.inner_border_width), pady=dp(theme.metrics.inner_border_width))

        self.header = LogicalCanvas(
            self.shell,
            dpi=self.dpi,
            height=header_height,
            bg=theme.header_background,
            bd=0,
            highlightthickness=0,
        )
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.header.bind("<Configure>", self._draw_header)
        self.month_label = self.header.create_text(
            12,
            18,
            text="",
            fill=theme.header_text,
            font=(FONT, 12, "normal") if theme.style == "aero" else (FONT, 13, "bold"),
            anchor="w",
            tags=("month_text",),
        )
        self.month_hint = self.header.create_text(
            12,
            39,
            text="",
            fill=theme.header_subtext,
            font=(FONT, 8),
            anchor="w",
            tags=("month_hint",),
        )
        self.header.tag_bind("month_text", "<Button-1>", lambda _event: self.go_today())

        compact = theme.style == "aero"
        widths = (25, 25, 25, 37, 29, 25) if compact else (27, 27, 27, 39, 31, 27)
        control_height = 25 if compact else 27
        control_y = 15 if compact else 14
        previous = ThemeButton(self.header, self, "‹", lambda: self.change_month(-1), width=widths[0], height=control_height, font_size=13 if compact else 14)
        today = ThemeButton(self.header, self, "今", self.go_today, width=widths[1], height=control_height, font_size=9)
        following = ThemeButton(self.header, self, "›", lambda: self.change_month(1), width=widths[2], height=control_height, font_size=13 if compact else 14)
        self.mode_button = ThemeButton(self.header, self, "桌面", self.toggle_window_mode, width=widths[3], height=control_height, font_size=8)
        self.menu_button = ThemeButton(self.header, self, "···", self.show_main_menu, width=widths[4], height=control_height, font_size=9 if compact else 10)
        minimize = ThemeButton(self.header, self, "−", self.hide_to_tray, width=widths[5], height=control_height, font_size=10)
        controls = (previous, today, following, self.mode_button, self.menu_button, minimize)
        x = WINDOW_WIDTH - 13 - sum(widths) - 5 * 2
        for control, control_width in zip(controls, widths):
            control.place(x=dp(x), y=dp(control_y), width=dp(control_width), height=dp(control_height))
            x += control_width + 2
        Tooltip(previous, "上个月（滚轮向上 / PgUp）")
        Tooltip(today, "回到今天（Ctrl+T）")
        Tooltip(following, "下个月（滚轮向下 / PgDn）")
        Tooltip(self.mode_button, "桌面模式空闲时不遮挡应用；点击月历会临时前置")
        Tooltip(minimize, "隐藏到通知区域，提醒仍会继续")

        self.header.bind("<ButtonPress-1>", self._start_drag)
        self.header.bind("<B1-Motion>", self._drag_window)
        self.header.bind("<ButtonRelease-1>", self._end_drag)

        weekdays = tk.Frame(self.shell, bg=theme.calendar_background, padx=dp(12))
        weekdays.pack(fill="x")
        for column, name in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            weekdays.grid_columnconfigure(column, weight=1, uniform="weekday")
            tk.Label(
                weekdays,
                text=name,
                bg=theme.calendar_background,
                fg=theme.date_weekend_text if column >= 5 else theme.weekday_text,
                font=(FONT, 8),
                pady=dp(weekday_pady),
            ).grid(row=0, column=column, sticky="ew")

        self.calendar_frame = tk.Frame(self.shell, bg=theme.calendar_background, padx=dp(12), pady=dp(2))
        self.calendar_frame.pack(fill="x")
        for column in range(7):
            self.calendar_frame.grid_columnconfigure(column, weight=1, uniform="day")
        for row in range(6):
            self.calendar_frame.grid_rowconfigure(row, weight=1)
            for column in range(7):
                cell = DayCell(self.calendar_frame, self, column)
                cell.grid(row=row, column=column, sticky="nsew")
                self.day_cells.append(cell)
        self.calendar_frame.bind("<MouseWheel>", self._calendar_wheel)
        for cell in self.day_cells:
            cell.bind("<MouseWheel>", self._calendar_wheel, add="+")

        tk.Frame(self.shell, bg=theme.divider, height=dp(1)).pack(fill="x", padx=dp(12), pady=(dp(3), 0))

        self.schedule_section = tk.Frame(self.shell, bg=theme.schedule_background)
        self.schedule_section.pack(fill="both", expand=True)

        self.agenda_bar = tk.Frame(self.schedule_section, bg=theme.schedule_background, height=dp(39), padx=dp(12), cursor="hand2")
        self.agenda_bar.pack_propagate(False)
        self.agenda_toggle = tk.Label(self.agenda_bar, text="⌃", bg=theme.schedule_background, fg=theme.text_secondary, font=(FONT, 10), cursor="hand2")
        self.agenda_toggle.pack(side="left", padx=(0, dp(6)))
        self.agenda_title = tk.Label(self.agenda_bar, text="", bg=theme.schedule_background, fg=theme.text_primary, font=(FONT, 10, "bold"), cursor="hand2")
        self.agenda_title.pack(side="left")
        self.agenda_count = tk.Label(self.agenda_bar, text="", bg=theme.schedule_background, fg=theme.text_muted, font=(FONT, 8), cursor="hand2")
        self.agenda_count.pack(side="left", padx=(dp(7), 0))
        add = ThemeButton(
            self.agenda_bar,
            self,
            "+",
            self.open_new_event,
            width=28,
            height=25,
            font_size=13,
            foreground=None if theme.style == "flat" else theme.control_text,
            surface_background=theme.schedule_background,
            accented=theme.style == "flat",
        )
        add.pack(side="right", pady=dp(4))
        routines = ThemeButton(
            self.agenda_bar,
            self,
            ROUTINE_ENTRY_LABEL,
            self.open_routine_manager,
            width=43,
            height=25,
            font_size=8,
            foreground=theme.text_secondary if theme.style == "flat" else theme.control_text,
            surface_background=theme.schedule_background,
            outlined=True,
        )
        routines.pack(side="right", pady=dp(4), padx=(0, dp(3)))
        Tooltip(add, "打开当天详情并管理事项")
        Tooltip(routines, "管理习惯清单：工作日习惯与一次性待办")
        for widget in (self.agenda_bar, self.agenda_toggle, self.agenda_title, self.agenda_count):
            widget.bind("<Button-1>", lambda _event: self.toggle_agenda())

        self._build_agenda_body()

    def _draw_header(self, _event=None) -> None:
        width = max(1, self.header.logical_width())
        height = max(1, self.header.logical_height())
        self.header.delete("header_art")
        if self.theme.style == "aero":
            vertical_multi_gradient(
                self.header,
                0,
                0,
                width,
                height,
                (
                    (0.0, self.theme.header_highlight),
                    (0.16, self.theme.header_gradient_start),
                    (0.34, self.theme.header_gradient_mid),
                    (0.72, blend(self.theme.header_gradient_mid, self.theme.header_gradient_end, 0.55)),
                    (1.0, self.theme.header_gradient_end),
                ),
                tags="header_art",
            )
            self.header.create_line(1, 1, width - 2, 1, fill=self.theme.header_highlight, tags="header_art")
            reflection_y = round(height * 0.33)
            self.header.create_line(2, reflection_y, width - 3, reflection_y, fill=blend(self.theme.header_highlight, self.theme.header_gradient_mid, 0.62), tags="header_art")
            self.header.create_line(0, height - 2, width, height - 2, fill=self.theme.header_shadow, tags="header_art")
        else:
            vertical_gradient(
                self.header,
                0,
                0,
                width,
                height,
                self.theme.header_gradient_start,
                self.theme.header_gradient_end,
                steps=42,
                tags="header_art",
            )
        self.header.create_line(0, height - 1, width, height - 1, fill=self.theme.header_border, tags="header_art")
        self.header.tag_lower("header_art")

    def _build_ddl_area(self, parent: tk.Widget, title: str, *, pinned: bool):
        theme = self.theme
        dp = self.dpi.px
        background = theme.ddl_pinned_background if pinned else theme.ddl_regular_background
        border = theme.ddl_pinned_border if pinned else theme.ddl_regular_border
        frame = tk.Frame(
            parent,
            bg=background,
            highlightthickness=dp(1),
            highlightbackground=border,
        )
        ddl_header = tk.Frame(frame, bg=background, padx=dp(8), pady=dp(3))
        ddl_header.pack(fill="x")
        tk.Label(
            ddl_header,
            text=title,
            bg=background,
            fg=theme.event_type_ddl if pinned else theme.text_secondary,
            font=(FONT, 8, "bold"),
        ).pack(side="left")
        count_label = tk.Label(
            ddl_header,
            text="",
            bg=background,
            fg=theme.text_secondary,
            font=(FONT, 8),
        )
        count_label.pack(side="right")
        ddl_body = tk.Frame(frame, bg=background)
        ddl_body.pack(fill="x")
        canvas = tk.Canvas(
            ddl_body,
            bg=background,
            bd=0,
            highlightthickness=0,
            height=dp(DDL_ROW_HEIGHT),
        )
        scrollbar = ttk.Scrollbar(ddl_body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=background)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="x", expand=True)
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        return frame, count_label, canvas, scrollbar, inner

    def _build_agenda_body(self) -> None:
        theme = self.theme
        dp = self.dpi.px
        (
            self.pinned_ddl_frame,
            self.pinned_ddl_count_label,
            self.pinned_ddl_canvas,
            self.pinned_ddl_scrollbar,
            self.pinned_ddl_inner,
        ) = self._build_ddl_area(self.schedule_section, "紧急 DDL", pinned=True)

        self.quick_frame = tk.Frame(self.schedule_section, bg=theme.schedule_background, padx=dp(12), pady=dp(5))
        self.quick_frame.pack(fill="x")
        self._quick_entry_hovered = False
        self.quick_var = tk.StringVar(value="")
        self.quick_entry = tk.Entry(
            self.quick_frame,
            textvariable=self.quick_var,
            bg=theme.input_background,
            fg=theme.text_muted,
            insertbackground=theme.text_primary,
            relief="flat",
            highlightthickness=dp(1),
            highlightbackground=theme.input_border,
            highlightcolor=theme.input_focus,
            font=(FONT, 9),
        )
        self.quick_entry.pack(side="left", fill="x", expand=True, ipady=dp(6))
        self.quick_entry.bind("<FocusIn>", self._quick_focus_in)
        self.quick_entry.bind("<FocusOut>", self._quick_focus_out)
        self.quick_entry.bind("<Enter>", lambda _event: self._quick_entry_hover(True))
        self.quick_entry.bind("<Leave>", lambda _event: self._quick_entry_hover(False))
        self.quick_entry.bind("<Return>", self.quick_add)
        self.quick_options_button = ThemeButton(
            self.quick_frame,
            self,
            "一般 · 选项",
            self.show_quick_options,
            width=66,
            height=25 if theme.style == "aero" else 29,
            font_size=8,
            foreground=theme.text_secondary if theme.style == "flat" else theme.control_text,
            surface_background=theme.schedule_background,
            outlined=True,
        )
        self.quick_options_button.pack(side="right", padx=(dp(6), 0))
        self._refresh_quick_options_button()

        self.agenda_bar.pack(fill="x")

        self.footer_frame = tk.Frame(self.schedule_section, bg=theme.schedule_background, padx=dp(13), height=dp(21))
        self.footer_frame.pack(side="bottom", fill="x")
        self.footer_frame.pack_propagate(False)
        self.upcoming_label = tk.Label(self.footer_frame, text="", bg=theme.schedule_background, fg=theme.text_muted, font=(FONT, 8), cursor="hand2")
        self.upcoming_label.pack(side="left")
        self.upcoming_label.bind("<Button-1>", lambda _event: UpcomingDialog(self))
        Tooltip(self.upcoming_label, "查看未来 7 天和已逾期日程")
        tk.Label(self.footer_frame, text="双击日期查看详情", bg=theme.schedule_background, fg=theme.text_muted, font=(FONT, 8)).pack(side="right")

        self.collapsible_frame = tk.Frame(self.schedule_section, bg=theme.schedule_background)
        (
            self.regular_ddl_frame,
            self.regular_ddl_count_label,
            self.regular_ddl_canvas,
            self.regular_ddl_scrollbar,
            self.regular_ddl_inner,
        ) = self._build_ddl_area(self.collapsible_frame, "DDL 事项 / 临近截止", pinned=False)

        self.daily_list_shell = tk.Frame(self.collapsible_frame, bg=theme.schedule_background, padx=dp(10))
        self.daily_list_shell.pack(fill="both", expand=True)
        self.agenda_canvas = tk.Canvas(self.daily_list_shell, bg=theme.schedule_background, bd=0, highlightthickness=0, width=dp(340), height=dp(126))
        self.agenda_scrollbar = ttk.Scrollbar(self.daily_list_shell, orient="vertical", command=self.agenda_canvas.yview)
        self.agenda_inner = tk.Frame(self.agenda_canvas, bg=theme.schedule_background)
        self.agenda_window = self.agenda_canvas.create_window((0, 0), window=self.agenda_inner, anchor="nw")
        self.agenda_canvas.configure(yscrollcommand=self.agenda_scrollbar.set)
        self.agenda_canvas.pack(side="left", fill="both", expand=True)
        self.agenda_inner.bind("<Configure>", lambda _event: self.agenda_canvas.configure(scrollregion=self.agenda_canvas.bbox("all")))
        self.agenda_canvas.bind("<Configure>", lambda event: self.agenda_canvas.itemconfigure(self.agenda_window, width=event.width))
        self.agenda_canvas.bind("<MouseWheel>", self._agenda_wheel)
        self.agenda_inner.bind("<MouseWheel>", self._agenda_wheel)
        if self.agenda_open:
            self.collapsible_frame.pack(fill="both", expand=True)

    def _set_initial_geometry(self) -> None:
        pinned_items, regular_items = self.store.grouped_ddl_events()
        height = self._desired_window_height(len(pinned_items), len(regular_items))
        self.update_idletasks()
        area = self.dpi.work_area()
        window_width = self.dpi.px(WINDOW_WIDTH)
        window_height = self.dpi.px(height)
        saved_x = self.store.settings.get("x")
        saved_y = self.store.settings.get("y")
        try:
            x = int(saved_x) if saved_x is not None else area.right - window_width - self.dpi.px(26)
            y = int(saved_y) if saved_y is not None else area.top + self.dpi.px(44)
        except (TypeError, ValueError):
            x, y = area.right - window_width - self.dpi.px(26), area.top + self.dpi.px(44)
        x, y = clamp_to_work_area(x, y, window_width, window_height)
        self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))

    def _ddl_canvas_height(self, item_count: int) -> int:
        return DDL_ROW_HEIGHT * min(max(0, item_count), DDL_VISIBLE_ROWS)

    def _ddl_region_height(self, item_count: int) -> int:
        if item_count <= 0:
            return 0
        # Header, borders, and the region's outer pack spacing are fixed;
        # Rows are the only variable part of the bounded DDL viewport.
        return DDL_REGION_CHROME_HEIGHT + self._ddl_canvas_height(item_count)

    def _desired_window_height(self, pinned_ddl_count: int, regular_ddl_count: int) -> int:
        base_height = OPEN_HEIGHT if self.agenda_open else CLOSED_HEIGHT
        base_height += self._ddl_region_height(pinned_ddl_count)
        if self.agenda_open:
            base_height += self._ddl_region_height(regular_ddl_count)
        work_height = self.dpi.logical(self.dpi.work_area().height) if hasattr(self, "dpi") else self.winfo_screenheight()
        return min(base_height, max(CLOSED_HEIGHT, work_height - 48))

    def _apply_window_height(self, pinned_ddl_count: int, regular_ddl_count: int) -> None:
        height = self._desired_window_height(pinned_ddl_count, regular_ddl_count)
        x, y = clamp_to_work_area(
            self.winfo_x(),
            self.winfo_y(),
            self.dpi.px(WINDOW_WIDTH),
            self.dpi.px(height),
        )
        self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))

    def _apply_main_layout(self, pinned_ddl_count: int, regular_ddl_count: int) -> None:
        visible = main_region_visibility(self.agenda_open, pinned_ddl_count, regular_ddl_count)

        if visible.pinned_ddl:
            self.pinned_ddl_canvas.configure(height=self.dpi.px(self._ddl_canvas_height(pinned_ddl_count)))
            if not self.pinned_ddl_frame.winfo_manager():
                self.pinned_ddl_frame.pack(
                    fill="x",
                    padx=self.dpi.px(10),
                    pady=(self.dpi.px(4), self.dpi.px(3)),
                    before=self.quick_frame,
                )
        elif self.pinned_ddl_frame.winfo_manager():
            self.pinned_ddl_frame.pack_forget()

        if visible.daily_content:
            if not self.collapsible_frame.winfo_manager():
                self.collapsible_frame.pack(fill="both", expand=True)
        elif self.collapsible_frame.winfo_manager():
            self.collapsible_frame.pack_forget()

        if visible.regular_ddl:
            self.regular_ddl_canvas.configure(height=self.dpi.px(self._ddl_canvas_height(regular_ddl_count)))
            if not self.regular_ddl_frame.winfo_manager():
                self.regular_ddl_frame.pack(
                    side="bottom",
                    fill="x",
                    padx=self.dpi.px(10),
                    pady=(self.dpi.px(3), self.dpi.px(4)),
                    before=self.daily_list_shell,
                )
        elif self.regular_ddl_frame.winfo_manager():
            self.regular_ddl_frame.pack_forget()

        self._apply_window_height(pinned_ddl_count, regular_ddl_count)

    def _finish_window_setup(self) -> None:
        make_tool_window(self)
        self._window_ready = True
        self.apply_window_mode(force_desktop=True)

    def _schedule_dpi_check(self, _event=None) -> None:
        if self._dpi_rebuilding:
            return
        if self._dpi_check_job:
            try:
                self.after_cancel(self._dpi_check_job)
            except tk.TclError:
                pass
        self._dpi_check_job = self.after(140, self._check_dpi_change)

    def _check_dpi_change(self) -> None:
        self._dpi_check_job = None
        if self._dpi_rebuilding or not self.winfo_exists():
            return
        new_dpi = self.dpi.current_window_dpi()
        if new_dpi == self.dpi.dpi:
            return
        self._apply_dpi_change(new_dpi)

    def _rebuild_main_ui(self, quick_value: str, quick_was_placeholder: bool) -> None:
        self.window_frame.destroy()
        self._configure_style()
        self._build_ui()
        self.render()
        if quick_value and not quick_was_placeholder:
            self.quick_var.set(quick_value)
            self.quick_placeholder_active = False
            self.quick_entry.configure(fg=self.theme.text_primary)
        self.after_idle(self._draw_header)

    def _apply_dpi_change(self, new_dpi: int) -> None:
        quick_value = self.quick_var.get() if hasattr(self, "quick_var") else ""
        quick_was_placeholder = self.quick_placeholder_active
        x, y = self.winfo_x(), self.winfo_y()
        old_dpi = self.dpi.dpi
        popup_geometries: list[tuple[tk.Toplevel, int, int, int, int]] = []
        for child in self.winfo_children():
            if not isinstance(child, tk.Toplevel) or not child.winfo_exists():
                continue
            child.update_idletasks()
            popup_geometries.append(
                (
                    child,
                    unscale_px(child.winfo_width(), old_dpi),
                    unscale_px(child.winfo_height(), old_dpi),
                    child.winfo_x(),
                    child.winfo_y(),
                )
            )
        self._dpi_rebuilding = True
        try:
            self.dpi.apply(new_dpi)
            self._rebuild_main_ui(quick_value, quick_was_placeholder)
            for popup, logical_width, logical_height, popup_x, popup_y in popup_geometries:
                if not popup.winfo_exists():
                    continue
                device_width = scale_px(logical_width, new_dpi)
                device_height = scale_px(logical_height, new_dpi)
                popup_x, popup_y = clamp_to_work_area(
                    popup_x,
                    popup_y,
                    device_width,
                    device_height,
                )
                popup.geometry(
                    scaled_geometry(logical_width, logical_height, popup_x, popup_y, new_dpi)
                )
            pinned, regular = self.store.grouped_ddl_events()
            height = self._desired_window_height(len(pinned), len(regular))
            x, y = clamp_to_work_area(x, y, self.dpi.px(WINDOW_WIDTH), self.dpi.px(height))
            self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))
            make_tool_window(self)
        finally:
            self._dpi_rebuilding = False
        self.after(80, self.apply_window_mode)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-n>", lambda _event: self.open_day_detail())
        self.bind("<Control-t>", lambda _event: self.go_today())
        self.bind("<Home>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-t>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-n>", lambda event: self._keyboard_command(event, self.open_day_detail))
        self.bind("<Return>", lambda event: self._keyboard_command(event, self.open_day_detail))
        self.bind("<Prior>", lambda _event: self.change_month(-1))
        self.bind("<Next>", lambda _event: self.change_month(1))
        self.bind("<Left>", lambda event: self._move_selection(event, -1))
        self.bind("<Right>", lambda event: self._move_selection(event, 1))
        self.bind("<Up>", lambda event: self._move_selection(event, -7))
        self.bind("<Down>", lambda event: self._move_selection(event, 7))
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<ButtonPress>", self._activate_desktop_session, add="+")
        self.bind("<Escape>", lambda _event: self._end_desktop_session())

    def render(self) -> None:
        self.header.itemconfigure(self.month_label, text=f"{self.shown_year}年 {self.shown_month}月")
        today = date.today()
        self.header.itemconfigure(self.month_hint, text=f"今天 {today.month}月{today.day}日 · {WEEKDAYS[today.weekday()]}")
        self._update_mode_badge()

        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self.shown_year, self.shown_month)
        while len(weeks) < 6:
            start = weeks[-1][-1] + timedelta(days=1)
            weeks.append([start + timedelta(days=index) for index in range(7)])
        for index, cell in enumerate(self.day_cells):
            row, column = divmod(index, 7)
            day = weeks[row][column]
            day_events = self.store.events_on(day)
            colors = [event.color if not event.done else self.theme.event_done for event in day_events]
            custom_status = self.store.date_status(day)
            system_holiday = holiday_for(day) if self.show_holidays else None
            status_holiday = (
                HolidayInfo("请假", "请假", "day_off")
                if custom_status == "leave"
                else HolidayInfo("自定义假期", "放假", "day_off")
                if custom_status == "holiday"
                else None
            )
            cell.update_day(
                day=day,
                in_month=day.month == self.shown_month,
                selected=day == self.selected,
                today=day == today,
                colors=colors,
                holiday=system_holiday or status_holiday,
                ddl=self.store.has_ddl_on(day),
                date_status=custom_status,
            )
        self.render_agenda()
        if self.day_detail_window and self.day_detail_window.winfo_exists():
            self.day_detail_window.refresh()

    def _display_holiday(self, day: date) -> Optional[HolidayInfo]:
        custom_status = self.store.date_status(day)
        if custom_status == "leave":
            return HolidayInfo("请假", "请假", "day_off")
        if custom_status == "holiday":
            return HolidayInfo("自定义假期", "放假", "day_off")
        return holiday_for(day) if self.show_holidays else None

    def render_agenda(self) -> None:
        theme = self.theme
        for child in self.agenda_inner.winfo_children():
            child.destroy()
        items = self.store.agenda_items_on(self.selected)
        events = [item for item in items if isinstance(item, Event)]
        routine_items = [item for item in items if isinstance(item, RoutineItem)]
        holiday = self._display_holiday(self.selected)
        holiday_text = f" · {holiday.name}" if holiday else ""
        self.agenda_title.configure(text=f"{self.selected.month}月{self.selected.day}日 · {WEEKDAYS[self.selected.weekday()]}{holiday_text}")
        total_items = len(events) + len(routine_items)
        self.agenda_count.configure(text=f"{total_items} 项" if total_items else "无安排")
        self.agenda_toggle.configure(text="⌃" if self.agenda_open else "⌄")

        if not events and not routine_items:
            empty = tk.Frame(self.agenda_inner, bg=theme.schedule_background, height=114)
            empty.pack(fill="both", expand=True)
            empty.pack_propagate(False)
            empty_title = "休息日不安排习惯清单" if not self.store.is_workday(self.selected) and self.store.routines else "这一天很清静"
            tk.Label(empty, text=empty_title, bg=theme.schedule_background, fg=theme.text_secondary, font=(FONT, 9, "bold")).pack(pady=(27, 2))
            tk.Label(empty, text="双击日期查看详情 · 习惯清单仅在工作日出现", bg=theme.schedule_background, fg=theme.text_muted, font=(FONT, 8)).pack()
        else:
            for item in items:
                if isinstance(item, RoutineItem):
                    self._build_routine_card(item)
                else:
                    self._build_event_card(item)

        upcoming_count = len(self.store.upcoming(7, include_overdue=True))
        self.upcoming_label.configure(text=f"未来 7 天 · {upcoming_count} 项  ›")
        pinned_ddl, regular_ddl = self.store.grouped_ddl_events()
        self._render_ddl_area(
            pinned_ddl,
            self.pinned_ddl_inner,
            self.pinned_ddl_canvas,
            self.pinned_ddl_count_label,
            self.pinned_ddl_scrollbar,
            pinned=True,
        )
        self._render_ddl_area(
            regular_ddl,
            self.regular_ddl_inner,
            self.regular_ddl_canvas,
            self.regular_ddl_count_label,
            self.regular_ddl_scrollbar,
            pinned=False,
        )
        self._apply_main_layout(len(pinned_ddl), len(regular_ddl))
        self._set_quick_placeholder()
        self.after_idle(self._update_scrollbar)

    def _render_ddl_area(
        self,
        ddl_items: list[Event],
        inner: tk.Frame,
        canvas: tk.Canvas,
        count_label: tk.Label,
        scrollbar: ttk.Scrollbar,
        *,
        pinned: bool,
    ) -> None:
        for child in inner.winfo_children():
            child.destroy()
        if not ddl_items:
            count_label.configure(text="")
            canvas.yview_moveto(0)
            canvas.configure(scrollregion=())
            if scrollbar.winfo_manager():
                scrollbar.pack_forget()
            return
        count_label.configure(text=f"{len(ddl_items)} 项")
        theme = self.theme
        region_background = theme.ddl_pinned_background if pinned else theme.ddl_regular_background
        for item in ddl_items:
            overdue = self.store.is_event_overdue(item)
            deadline = self.store.event_end_date(item)
            row_background = (
                theme.ddl_overdue_background
                if overdue
                else theme.ddl_due_background
                if pinned
                else theme.schedule_card_background
            )
            row = tk.Frame(inner, bg=row_background, cursor="hand2", padx=7, pady=4)
            row.pack(fill="x", pady=(0, 2))
            tk.Frame(row, bg=item.color, width=3).pack(side="left", fill="y", padx=(0, 7))
            title = tk.Label(
                row,
                text=truncate(item.title, 20),
                bg=row_background,
                fg=theme.text_primary,
                font=(FONT, 8),
                anchor="w",
            )
            title.pack(side="left", fill="x", expand=True)
            deadline_text = f"{deadline.month}月{deadline.day}日"
            if overdue:
                deadline_text += " · 已逾期"
            elif pinned:
                deadline_text += " · 24小时内"
            meta = tk.Label(
                row,
                text=deadline_text,
                bg=row_background,
                fg=theme.danger if overdue else theme.schedule_time_text,
                font=(FONT, 8),
                cursor="hand2",
            )
            meta.pack(side="right")
            for widget in (row, title, meta):
                widget.bind("<Button-1>", lambda _event, event=item: self._open_ddl_detail(event))
                widget.bind("<MouseWheel>", lambda event, target=canvas: target.yview_scroll(int(-event.delta / 120), "units"))
        inner.configure(bg=region_background)
        self.after_idle(lambda: self._update_ddl_scrollbar(canvas, inner, scrollbar))

    def _update_ddl_scrollbar(self, canvas: tk.Canvas, inner: tk.Frame, scrollbar: ttk.Scrollbar) -> None:
        if not canvas.winfo_exists():
            return
        inner.update_idletasks()
        bbox = canvas.bbox("all")
        needs_scroll = bool(bbox and bbox[3] > canvas.winfo_height())
        if needs_scroll and not scrollbar.winfo_manager():
            scrollbar.pack(side="right", fill="y")
        elif not needs_scroll and scrollbar.winfo_manager():
            scrollbar.pack_forget()

    def _open_ddl_detail(self, event: Event) -> None:
        self.open_day_detail(self.store.event_end_date(event))

    def _build_event_card(self, item: Event) -> None:
        theme = self.theme
        card_bg = theme.card_done_background if item.done else theme.schedule_card_background
        card = tk.Frame(self.agenda_inner, bg=card_bg, highlightthickness=1, highlightbackground=theme.schedule_card_border, cursor="hand2")
        card.pack(fill="x", pady=(0, 5), padx=1)
        stripe = tk.Frame(card, bg=event_stripe_color(theme, item), width=EVENT_STRIPE_WIDTH)
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        check = TaskCheck(
            card,
            self,
            done=item.done,
            background=card_bg,
            command=lambda event=item: self.toggle_done(event),
        )
        check.pack(side="left", fill="y", padx=(3, 0))
        content = tk.Frame(card, bg=card_bg, padx=1, pady=5)
        content.pack(side="left", fill="both", expand=True)
        title_row = tk.Frame(content, bg=card_bg)
        title_row.pack(fill="x")
        title = tk.Label(
            title_row,
            text=truncate(item.title, 24),
            bg=card_bg,
            fg=theme.text_done if item.done else theme.text_primary,
            font=(FONT, 9, "overstrike" if item.done else "normal"),
            anchor="w",
        )
        title.pack(side="left", fill="x", expand=True)
        badge_style = None if item.done else event_type_badge_style(theme, item.event_type)
        badge = None
        if badge_style:
            badge_text, badge_background, badge_border = badge_style
            badge = tk.Label(
                title_row,
                text=EVENT_TYPE_LABELS[item.event_type],
                bg=badge_background,
                fg=badge_text,
                font=(FONT, 7),
                padx=4,
                pady=0,
                highlightthickness=1,
                highlightbackground=badge_border,
                cursor="hand2",
            )
            badge.pack(side="right", padx=(4, 0))
        if self.store.is_event_overdue(item):
            timing = "已逾期" + (" · " + item.due_at.strftime("%H:%M") if item.has_time else "")
            timing_color = theme.danger
        else:
            timing = (item.due_at.strftime("%H:%M") if item.has_time else "无具体时间") + f" · {EVENT_TYPE_LABELS[item.event_type]}"
            timing_color = event_type_color(theme, item.event_type)
        if item.duration_days > 1:
            timing += f" · 第{self.store.event_day_number(item, self.selected)}/{item.duration_days}天"
        meta = tk.Label(content, text=timing, bg=card_bg, fg=timing_color, font=(FONT, 8), anchor="w")
        meta.pack(fill="x", pady=(1, 0))
        more = tk.Label(card, text="›", bg=card_bg, fg=theme.text_muted, font=(FONT, 12), width=2, cursor="hand2")
        more.pack(side="right", fill="y")
        for widget in (card, content, title_row, title, meta, more, stripe):
            widget.bind("<Button-1>", lambda _event, event=item: self.open_editor(event))
            widget.bind("<Button-3>", lambda event, item=item: self.show_event_menu(item, event.x_root, event.y_root))
            widget.bind("<MouseWheel>", self._agenda_wheel)
        if badge:
            badge.bind("<Button-1>", lambda _event, event=item: self.open_editor(event))
            badge.bind("<Button-3>", lambda event, item=item: self.show_event_menu(item, event.x_root, event.y_root))
            badge.bind("<MouseWheel>", self._agenda_wheel)
        self._bind_card_hover((card, content, title_row, title, meta, more), card_bg)

    def _build_routine_card(self, item: RoutineItem) -> None:
        theme = self.theme
        done = item.is_done_on(self.selected)
        card_bg = theme.card_done_background if done else theme.schedule_card_background
        card = tk.Frame(self.agenda_inner, bg=card_bg, highlightthickness=1, highlightbackground=theme.schedule_card_border, cursor="hand2")
        card.pack(fill="x", pady=(0, 5), padx=1)
        stripe = tk.Frame(card, bg=theme.event_done if done else item.color, width=EVENT_STRIPE_WIDTH)
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        check = TaskCheck(
            card,
            self,
            done=done,
            background=card_bg,
            command=lambda entry=item: self.toggle_routine(entry),
        )
        check.pack(side="left", fill="y", padx=(3, 0))
        content = tk.Frame(card, bg=card_bg, padx=1, pady=5)
        content.pack(side="left", fill="both", expand=True)
        title = tk.Label(
            content,
            text=truncate(item.title, 22),
            bg=card_bg,
            fg=theme.text_done if done else theme.text_primary,
            font=(FONT, 9, "overstrike" if done else "normal"),
            anchor="w",
        )
        title.pack(fill="x")
        if item.kind == "habit":
            meta_text = "习惯 · 今日已完成，下一工作日继续" if done else "习惯 · 每个工作日"
        else:
            meta_text = "待办 · 已完成" if done else "待办 · 完成一次即结束"
        meta = tk.Label(content, text=meta_text, bg=card_bg, fg=theme.schedule_time_text, font=(FONT, 8), anchor="w")
        meta.pack(fill="x", pady=(1, 0))
        tag = tk.Label(
            card,
            text="习惯" if item.kind == "habit" else "待办",
            bg=theme.accent_soft if item.kind == "habit" else theme.todo_tag_background,
            fg=theme.accent if item.kind == "habit" else theme.todo_tag_text,
            font=(FONT, 7),
            padx=5,
            pady=2,
        )
        tag.pack(side="right", padx=(2, 7))
        for widget in (card, content, title, meta, tag, stripe):
            widget.bind("<Button-1>", lambda _event, entry=item: self.open_routine_editor(entry))
            widget.bind("<MouseWheel>", self._agenda_wheel)
        self._bind_card_hover((card, content, title, meta), card_bg)

    def _bind_card_hover(self, widgets: tuple[tk.Widget, ...], normal: str) -> None:
        hover = self.theme.schedule_card_hover

        def apply(color: str) -> None:
            for widget in widgets:
                if widget.winfo_exists():
                    widget.configure(bg=color)

        for widget in widgets:
            widget.bind("<Enter>", lambda _event: apply(hover), add="+")
            widget.bind("<Leave>", lambda _event: apply(normal), add="+")

    def _update_scrollbar(self) -> None:
        self.agenda_inner.update_idletasks()
        bbox = self.agenda_canvas.bbox("all")
        needs_scroll = bool(bbox and bbox[3] > self.agenda_canvas.winfo_height())
        if needs_scroll and not self.agenda_scrollbar.winfo_ismapped():
            self.agenda_scrollbar.pack(side="right", fill="y")
        elif not needs_scroll and self.agenda_scrollbar.winfo_ismapped():
            self.agenda_scrollbar.pack_forget()

    def select_day(self, day: date) -> None:
        self.selected = day
        if day.year != self.shown_year or day.month != self.shown_month:
            self.shown_year, self.shown_month = day.year, day.month
        self.render()

    def change_month(self, delta: int) -> None:
        index = self.shown_year * 12 + self.shown_month - 1 + delta
        year, zero_month = divmod(index, 12)
        month = zero_month + 1
        day_number = min(self.selected.day, calendar.monthrange(year, month)[1])
        self.shown_year, self.shown_month = year, month
        self.selected = date(year, month, day_number)
        self.render()

    def go_today(self) -> None:
        self.selected = date.today()
        self.shown_year, self.shown_month = self.selected.year, self.selected.month
        self.render()

    def _move_selection(self, event: tk.Event, days: int) -> Optional[str]:
        if event.widget.winfo_class() in ("Entry", "Text", "TEntry", "TCombobox"):
            return None
        self.select_day(self.selected + timedelta(days=days))
        return "break"

    @staticmethod
    def _keyboard_command(event: tk.Event, command: Callable[[], None]) -> Optional[str]:
        if event.widget.winfo_class() in ("Entry", "Text", "TEntry", "TCombobox"):
            return None
        command()
        return "break"

    def _calendar_wheel(self, event: tk.Event) -> str:
        self.change_month(-1 if event.delta > 0 else 1)
        return "break"

    def _agenda_wheel(self, event: tk.Event) -> str:
        self.agenda_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def open_editor(self, event: Optional[Event] = None, selected: Optional[date] = None) -> None:
        if self.editor_window and self.editor_window.winfo_exists():
            self.present_overlay(self.editor_window)
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
            self._lower_job = None
        self.attributes("-topmost", False)
        send_to_desktop(self)
        self.editor_window = EventEditor(self, selected or (event.due_date if event else self.selected), event)

    def open_new_event(self, selected: Optional[date] = None) -> None:
        self.open_editor(selected=selected or self.selected)

    def open_day_detail(self, day: Optional[date] = None) -> None:
        target_day = day or self.selected
        self.select_day(target_day)
        if self.day_detail_window and self.day_detail_window.winfo_exists():
            self.day_detail_window.set_day(target_day)
            self.present_overlay(self.day_detail_window)
            return
        self.attributes("-topmost", False)
        send_to_desktop(self)
        self.day_detail_window = DayDetailDialog(self, target_day)

    def open_routine_manager(self) -> None:
        if self.routine_manager and self.routine_manager.winfo_exists():
            self.present_overlay(self.routine_manager)
            return
        self.attributes("-topmost", False)
        send_to_desktop(self)
        self.routine_manager = RoutineManager(self)

    def open_routine_editor(self, item: Optional[RoutineItem] = None) -> None:
        if self.routine_editor and self.routine_editor.winfo_exists():
            self.present_overlay(self.routine_editor)
            return
        self.attributes("-topmost", False)
        send_to_desktop(self)
        self.routine_editor = RoutineEditor(self, item)

    def present_overlay(self, window: tk.Toplevel) -> None:
        if not window.winfo_exists():
            return
        if window not in self.overlay_windows:
            self.overlay_windows.append(window)
            window.bind(
                "<Destroy>",
                lambda event, overlay=window: self._overlay_destroyed(event, overlay),
                add="+",
            )
        self.attributes("-topmost", False)
        send_to_desktop(self)
        make_tool_window(window)
        window.attributes("-topmost", True)
        window.lift()
        bring_to_front(window)

    def _overlay_destroyed(self, event: tk.Event, window: tk.Toplevel) -> None:
        if event.widget is not window:
            return
        if window in self.overlay_windows:
            self.overlay_windows.remove(window)
        if self.winfo_exists() and not self._active_overlays():
            self.after(80, self.apply_window_mode)

    def _active_overlays(self) -> list[tk.Toplevel]:
        active: list[tk.Toplevel] = []
        for window in self.overlay_windows:
            try:
                if window.winfo_exists() and window.state() != "withdrawn":
                    active.append(window)
            except tk.TclError:
                continue
        return active

    def upsert_event(self, event: Event) -> None:
        self.store.upsert(event)
        self.selected = self.store.event_dates(event)[0]
        self.shown_year, self.shown_month = self.selected.year, self.selected.month
        self.render()

    def delete_event(self, event_id: str) -> None:
        self.store.delete(event_id)
        self.render()

    def toggle_done(self, event: Event) -> None:
        event.done = not event.done
        event.snooze_until = None
        self.store.upsert(event)
        self.render()

    def toggle_routine(self, item: RoutineItem, day: Optional[date] = None) -> None:
        target_day = day or self.selected
        self.store.toggle_routine(item, target_day)
        self.render()
        if self.routine_manager and self.routine_manager.winfo_exists():
            self.routine_manager.refresh()

    def quick_add(self, _event=None) -> str:
        title = self.quick_var.get().strip()
        if not title or self.quick_placeholder_active:
            return "break"
        self.store.create_quick(
            title,
            self.selected,
            color=self.quick_color,
            event_type=self.quick_event_type,
        )
        self.quick_var.set("")
        self.quick_placeholder_active = False
        self.render()
        if hasattr(self.quick_entry, "configure") and hasattr(self, "after"):
            self.quick_placeholder_active = True
            self.quick_var.set("已保存 · 可继续输入")
            self.quick_entry.configure(
                fg=self.theme.quick_success,
                highlightbackground=self.theme.quick_success,
                highlightcolor=self.theme.quick_success,
            )
            self.after(650, self._finish_quick_feedback)
        self.quick_entry.focus_set()
        return "break"

    def _finish_quick_feedback(self) -> None:
        if not self.quick_entry.winfo_exists():
            return
        self.quick_var.set("")
        self.quick_placeholder_active = False
        if self.focus_get() == self.quick_entry:
            self.quick_entry.configure(
                fg=self.theme.text_primary,
                highlightbackground=self.theme.input_focus,
                highlightcolor=self.theme.input_focus,
            )
        else:
            self._set_quick_placeholder()

    def _set_quick_color(self, value: str) -> None:
        self.quick_color = value
        self._refresh_quick_options_button()

    def _set_quick_event_type(self, value: str) -> None:
        self.quick_event_type = value
        self._refresh_quick_options_button()

    def _refresh_quick_options_button(self) -> None:
        if not hasattr(self, "quick_options_button"):
            return
        label = EVENT_TYPE_LABELS.get(self.quick_event_type, EVENT_TYPE_LABELS["general"])
        self.quick_options_button.set_text(
            f"{label} · 选项",
            event_type_color(self.theme, self.quick_event_type),
        )

    def show_quick_options(self) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        color_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        self.quick_color_var = tk.StringVar(value=self.quick_color)
        for color_name, color_value in COLORS.items():
            color_menu.add_radiobutton(
                label=color_name,
                variable=self.quick_color_var,
                value=color_value,
                command=lambda value=color_value: self._set_quick_color(value),
            )
        menu.add_cascade(label="颜色", menu=color_menu)

        event_type_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        self.quick_event_type_var = tk.StringVar(value=self.quick_event_type)
        for event_type, label in EVENT_TYPE_OPTIONS:
            event_type_menu.add_radiobutton(
                label=label,
                variable=self.quick_event_type_var,
                value=event_type,
                command=lambda value=event_type: self._set_quick_event_type(value),
            )
        menu.add_cascade(label=f"事项类型 · {EVENT_TYPE_LABELS[self.quick_event_type]}", menu=event_type_menu)
        x = self.quick_options_button.winfo_rootx()
        y = self.quick_options_button.winfo_rooty() + self.quick_options_button.winfo_height()
        menu.tk_popup(x, y)

    def _set_quick_placeholder(self) -> None:
        if self.focus_get() == self.quick_entry and not self.quick_placeholder_active:
            return
        if not self.quick_placeholder_active and self.quick_var.get().strip():
            return
        self.quick_placeholder_active = True
        self.quick_var.set(f"快速添加到 {self.selected.month}月{self.selected.day}日，回车保存")
        self.quick_entry.configure(fg=self.theme.text_muted)

    def _quick_entry_hover(self, hovered: bool) -> None:
        self._quick_entry_hovered = hovered
        if self.focus_get() == self.quick_entry:
            return
        self.quick_entry.configure(
            highlightbackground=self.theme.input_hover_border if hovered else self.theme.input_border,
        )

    def _quick_focus_in(self, _event=None) -> None:
        if self.quick_placeholder_active:
            self.quick_var.set("")
            self.quick_placeholder_active = False
            self.quick_entry.configure(fg=self.theme.text_primary)
        self.quick_entry.configure(
            highlightbackground=self.theme.input_focus,
            highlightcolor=self.theme.input_focus,
        )

    def _quick_focus_out(self, _event=None) -> None:
        if not self.quick_var.get().strip():
            self._set_quick_placeholder()
        self.quick_entry.configure(
            highlightbackground=(
                self.theme.input_hover_border if self._quick_entry_hovered else self.theme.input_border
            ),
            highlightcolor=self.theme.input_focus,
        )

    def toggle_agenda(self) -> None:
        self.agenda_open = not self.agenda_open
        self.agenda_toggle.configure(text="⌃" if self.agenda_open else "⌄")
        pinned_ddl, regular_ddl = self.store.grouped_ddl_events()
        self._apply_main_layout(len(pinned_ddl), len(regular_ddl))
        self.store.settings["agenda_open"] = self.agenda_open
        self._save_window_settings()
        self.after(80, self.apply_window_mode)

    def toggle_window_mode(self) -> None:
        self.window_mode = "pinned" if self.window_mode == "desktop" else "desktop"
        self.desktop_session_active = False
        self.store.settings["window_mode"] = self.window_mode
        self.apply_window_mode(force_desktop=self.window_mode == "desktop")
        self.store.save()

    def apply_window_mode(self, force_desktop: bool = False) -> None:
        if not self.winfo_exists():
            return
        make_tool_window(self)
        overlays = self._active_overlays()
        if overlays:
            self.attributes("-topmost", False)
            send_to_desktop(self)
            for overlay in overlays:
                overlay.attributes("-topmost", True)
            bring_to_front(overlays[-1])
            return
        if self.window_mode == "pinned":
            self.attributes("-topmost", True)
            self.lift()
        else:
            self.attributes("-topmost", False)
            if force_desktop:
                self.desktop_session_active = False
            if self.desktop_session_active:
                raise_for_interaction(self)
            else:
                send_to_desktop(self)
        self._update_mode_badge()

    def _update_mode_badge(self) -> None:
        if self.window_mode == "pinned":
            self.mode_button.set_text("置顶", self.theme.accent if self.theme.style == "flat" else self.theme.control_text)
        elif self.desktop_session_active:
            self.mode_button.set_text("前台", self.theme.accent if self.theme.style == "flat" else self.theme.control_text)
        else:
            self.mode_button.set_text("桌面", self.theme.text_secondary if self.theme.style == "flat" else self.theme.control_text)

    def _activate_desktop_session(self, _event=None) -> None:
        if not self._window_ready or self.window_mode != "desktop":
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
            self._lower_job = None
        self.desktop_session_active = True
        raise_for_interaction(self)
        self._update_mode_badge()

    def _on_focus_out(self, _event=None) -> None:
        if self.window_mode != "desktop" or not self.desktop_session_active:
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
        self._lower_job = self.after(700, self._return_to_desktop_if_inactive)

    def _return_to_desktop_if_inactive(self) -> None:
        self._lower_job = None
        if self.window_mode != "desktop" or not self.desktop_session_active:
            return
        if self._active_overlays() or is_foreground_process():
            self._lower_job = self.after(900, self._return_to_desktop_if_inactive)
            return
        self._end_desktop_session()

    def _end_desktop_session(self) -> None:
        if self.window_mode != "desktop":
            return
        self.desktop_session_active = False
        self.apply_window_mode(force_desktop=True)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if not self._drag_origin:
            return
        start_x, start_y, win_x, win_y = self._drag_origin
        self.geometry(position_at(win_x + event.x_root - start_x, win_y + event.y_root - start_y))

    def _end_drag(self, _event=None) -> None:
        self._drag_origin = None
        new_dpi = self.dpi.current_window_dpi()
        if new_dpi != self.dpi.dpi:
            self._apply_dpi_change(new_dpi)
        width, height = self.winfo_width(), self.winfo_height()
        x, y = clamp_to_work_area(self.winfo_x(), self.winfo_y(), width, height)
        self.geometry(position_at(x, y))
        self._save_window_settings()
        self.after(150, self.apply_window_mode)

    def _save_window_settings(self) -> None:
        self.store.settings["x"] = self.winfo_x()
        self.store.settings["y"] = self.winfo_y()
        self.store.settings["agenda_open"] = self.agenda_open
        self.store.settings["window_mode"] = self.window_mode
        self.store.settings["theme"] = self.theme_name
        self.store.save()

    def show_day_menu(self, day: date, x: int, y: int) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(label="查看当天详情", command=lambda: self.open_day_detail(day))
        menu.add_command(label="新建日程", command=lambda: self.open_editor(selected=day))
        menu.add_command(label="快速输入", command=self._focus_quick_entry)
        menu.add_separator()
        menu.add_command(label="回到今天", command=self.go_today)
        menu.tk_popup(x, y)

    def show_event_menu(self, event: Event, x: int, y: int) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(label="编辑", command=lambda: self.open_editor(event))
        menu.add_command(label="标记为未完成" if event.done else "标记为已完成", command=lambda: self.toggle_done(event))
        if not event.done:
            menu.add_command(label="推迟到明天", command=lambda: self.defer_to_tomorrow(event))
        menu.add_separator()
        menu.add_command(label="删除", command=lambda: self._confirm_delete(event))
        menu.tk_popup(x, y)

    def _confirm_delete(self, event: Event, parent: Optional[tk.Widget] = None) -> None:
        if messagebox.askyesno(APP_NAME, f"确定删除“{event.title}”？", parent=parent or self):
            self.delete_event(event.id)

    def defer_to_tomorrow(self, event: Event) -> None:
        self.store.clear_notifications(event.id)
        event.due = (event.due_at + timedelta(days=1)).isoformat(timespec="minutes")
        event.snooze_until = None
        self.store.upsert(event)
        self.selected = event.due_date
        self.shown_year, self.shown_month = self.selected.year, self.selected.month
        self.render()

    def _focus_quick_entry(self) -> None:
        self.quick_entry.focus_set()

    def show_main_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(
            label="切换为桌面模式" if self.window_mode == "pinned" else "始终置顶",
            command=self.toggle_window_mode,
        )
        menu.add_command(label="收起日程区" if self.agenda_open else "展开日程区", command=self.toggle_agenda)
        menu.add_command(label="查看未来 7 天", command=lambda: UpcomingDialog(self))
        menu.add_command(label="管理习惯清单…", command=self.open_routine_manager)
        menu.add_separator()
        theme_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        self.theme_var = tk.StringVar(value=self.theme_name)
        theme_menu.add_radiobutton(
            label="Modern",
            variable=self.theme_var,
            value="modern",
            command=lambda: self.set_theme("modern"),
        )
        theme_menu.add_radiobutton(
            label="Win7 Aero",
            variable=self.theme_var,
            value="win7_aero",
            command=lambda: self.set_theme("win7_aero"),
        )
        menu.add_cascade(label=f"主题 · {self.theme.display_name}", menu=theme_menu)
        opacity_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        current_opacity = round(float(self.attributes("-alpha")), 2)
        self.opacity_var = tk.DoubleVar(value=current_opacity)
        for label, value in (("清晰（100%，推荐）", 1.0), ("轻透（97%）", 0.97), ("柔和（92%）", 0.92)):
            opacity_menu.add_radiobutton(label=label, variable=self.opacity_var, value=value, command=lambda v=value: self.set_opacity(v))
        menu.add_cascade(label="透明度（半透明会降低文字清晰度）", menu=opacity_menu)
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        menu.add_checkbutton(label="开机自动启动", variable=self.autostart_var, command=lambda: self.toggle_autostart(self.autostart_var.get()))
        self.holiday_var = tk.BooleanVar(value=self.show_holidays)
        menu.add_checkbutton(label="显示节假日与常用节日", variable=self.holiday_var, command=lambda: self.toggle_holidays(self.holiday_var.get()))
        menu.add_command(label=f"检查更新…（当前 v{__version__}）", command=self.check_for_updates)
        menu.add_command(label="导出数据备份…", command=self.export_backup)
        menu.add_separator()
        menu.add_command(label="快捷键与操作说明", command=self.show_help)
        menu.add_command(label="退出", command=self.on_close)
        x = self.menu_button.winfo_rootx()
        y = self.menu_button.winfo_rooty() + self.menu_button.winfo_height()
        menu.tk_popup(x, y)

    def set_theme(self, name: str) -> None:
        normalized = normalize_theme_name(name)
        if normalized == self.theme_name:
            return
        quick_value = self.quick_var.get() if hasattr(self, "quick_var") else ""
        quick_was_placeholder = self.quick_placeholder_active
        self.theme_name = normalized
        self.theme = get_theme(normalized)
        activate_theme(self.theme)
        self._apply_theme_opacity()
        self.store.settings["theme"] = normalized
        self.store.save()
        self._rebuild_main_ui(quick_value, quick_was_placeholder)
        self.after(80, self.apply_window_mode)

    def set_opacity(self, value: float) -> None:
        self.store.settings["opacity"] = value
        self.store.save()
        self._apply_theme_opacity(value)

    def toggle_holidays(self, enabled: bool) -> None:
        self.show_holidays = enabled
        self.store.settings["show_holidays"] = enabled
        self.store.save()
        self.render()

    def _start_tray_icon(self) -> None:
        if self.tray_icon or not self.winfo_exists():
            return
        intro_key = "tray_intro_version"
        first_for_version = self.store.settings.get(intro_key) != __version__
        message = "启动完成。顶部横线可隐藏到这里，单击托盘图标可重新显示月历。" if first_for_version else None
        self.tray_icon = TrayIcon(
            f"{APP_NAME} v{__version__}",
            resource_path("assets/calendar.ico"),
            self.tray_actions.put,
            startup_message=message,
        )
        if self.tray_icon.start() and first_for_version:
            self.store.settings[intro_key] = __version__
            self.store.save()
        elif self.tray_icon.error:
            log_exception(
                RuntimeError,
                RuntimeError(f"系统托盘初始化失败：{self.tray_icon.error}"),
                None,
            )

    def _poll_tray_actions(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                self._handle_tray_action(self.tray_actions.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._poll_tray_actions)

    def _handle_tray_action(self, action: str) -> None:
        if action == "exit":
            self.on_close()
            return
        self.deiconify()
        self._activate_desktop_session()
        if self.window_mode == "pinned":
            bring_to_front(self)
        if action == "new":
            self.after(40, self.open_editor)
        elif action == "today":
            self.go_today()
        elif action == "update":
            self.after(40, self.check_for_updates)

    def toggle_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled, APP_DIR / "app.py")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"设置开机启动失败：\n{exc}", parent=self)

    def export_backup(self) -> None:
        self.store.save()
        filename = filedialog.asksaveasfilename(
            parent=self,
            title="导出日历备份",
            defaultextension=".json",
            filetypes=(("JSON 备份", "*.json"), ("所有文件", "*.*")),
            initialfile=f"桌面月历备份-{date.today().isoformat()}.json",
        )
        if filename:
            shutil.copy2(self.store.data_file, Path(filename))

    def show_help(self) -> None:
        messagebox.showinfo(
            "操作说明",
            "单击日期：查看当天安排\n"
            "双击日期：打开当天事项详情\n"
            "右键日期：打开快捷菜单\n"
            "单击日程：编辑；方框：完成\n"
            "习惯清单：习惯每天重置，待办完成一次即结束\n"
            "滚轮 / PgUp / PgDn：切换月份\n"
            "方向键：移动所选日期\n"
            "Ctrl+N：打开当天事项详情\n"
            "Ctrl+T：回到今天\n"
            "拖动顶部：移动挂件位置\n\n"
            "顶部横线 / Alt+F4：隐藏到通知区域，提醒继续运行\n"
            "真正退出：右键托盘图标并选择“退出桌面月历”\n\n"
            "桌面模式空闲时会待在普通应用窗口后面。单击月历后会临时进入前台，完成操作并切换到其他应用后自动回到桌面层；按 Esc 可立即归位。需要一直覆盖其他窗口时，再点击顶部“桌面”切换为置顶。",
            parent=self,
        )

    def _call_main(self, callback: Callable[[], None]) -> None:
        try:
            self.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def check_for_updates(self) -> None:
        if self.update_busy:
            if self.update_dialog and self.update_dialog.winfo_exists():
                self.update_dialog.lift()
            return
        self.update_busy = True
        self.update_dialog = UpdateProgressDialog(self, "正在连接 GitHub Releases…")

        def worker() -> None:
            try:
                info = check_for_update()
                self._call_main(lambda: self._finish_update_check(info, None))
            except Exception as exc:
                self._call_main(lambda error=exc: self._finish_update_check(None, error))

        threading.Thread(target=worker, name="calendar-update-check", daemon=True).start()

    def _close_update_dialog(self) -> None:
        if self.update_dialog and self.update_dialog.winfo_exists():
            self.update_dialog.destroy()
        self.update_dialog = None

    def _finish_update_check(self, info: Optional[UpdateInfo], error: Optional[Exception]) -> None:
        self._close_update_dialog()
        self.update_busy = False
        if error:
            messagebox.showerror(APP_NAME, f"检查更新失败：\n\n{error}", parent=self)
            self.after(100, self.apply_window_mode)
            return
        if not info:
            return
        if not is_newer_version(info.version):
            messagebox.showinfo(APP_NAME, f"当前已经是最新版 v{__version__}。", parent=self)
            self.after(100, self.apply_window_mode)
            return
        notes = info.notes.strip() or "这个版本没有附加说明。"
        notes = truncate(notes.replace("\r", ""), 420)
        question = (
            f"发现新版本 {info.tag}\n\n"
            f"{notes}\n\n"
            "是否立即下载、安装并重启桌面月历？"
        )
        if messagebox.askyesno("发现桌面月历更新", question, parent=self):
            self._start_update_download(info)
        else:
            self.after(100, self.apply_window_mode)

    def _start_update_download(self, info: UpdateInfo) -> None:
        if not running_as_packaged_app():
            messagebox.showinfo(
                APP_NAME,
                "当前是源码运行模式，不能覆盖安装。\n请用发布版 DesktopCalendar.exe 测试自动更新。",
                parent=self,
            )
            return
        self.update_busy = True
        self.update_dialog = UpdateProgressDialog(self, f"正在下载 {info.tag}…")

        def update_progress(value: int) -> None:
            self._call_main(lambda amount=value: self.update_dialog and self.update_dialog.set_progress(amount))

        def worker() -> None:
            try:
                archive = download_update(info, update_progress)
                self._call_main(lambda: self._finish_update_download(archive, None))
            except Exception as exc:
                self._call_main(lambda error=exc: self._finish_update_download(None, error))

        threading.Thread(target=worker, name="calendar-update-download", daemon=True).start()

    def _finish_update_download(self, archive: Optional[Path], error: Optional[Exception]) -> None:
        if error or not archive:
            self._close_update_dialog()
            self.update_busy = False
            messagebox.showerror(APP_NAME, f"下载更新失败：\n\n{error}", parent=self)
            self.after(100, self.apply_window_mode)
            return
        try:
            if self.update_dialog:
                self.update_dialog.set_progress(100)
                self.update_dialog.set_status("校验通过，正在启动更新器…")
            launch_updater(archive)
            self.store.save()
            self.after(900, self.on_close)
        except UpdateError as exc:
            self._close_update_dialog()
            self.update_busy = False
            messagebox.showerror(APP_NAME, f"无法安装更新：\n\n{exc}", parent=self)

    def check_reminders(self) -> None:
        now = datetime.now()
        changed = False
        for event in list(self.store.events):
            if event.done:
                continue
            trigger: Optional[datetime] = None
            kind = "reminder"
            if event.snooze_until:
                try:
                    trigger = datetime.fromisoformat(event.snooze_until)
                    kind = "snooze"
                except ValueError:
                    event.snooze_until = None
            elif event.reminder is not None:
                trigger = self.store.event_starts_at(event) - timedelta(minutes=event.reminder)
            if trigger is None:
                continue
            key = f"{event.id}:{kind}:{trigger.isoformat(timespec='minutes')}"
            latest = self.store.event_starts_at(event) + timedelta(hours=2) if kind == "reminder" else trigger + timedelta(minutes=10)
            if trigger <= now <= latest and key not in self.store.notified:
                self.store.notified.add(key)
                if kind == "snooze":
                    event.snooze_until = None
                self.show_notification(event)
                changed = True
        if self._check_routine_reminder(now):
            changed = True
        if changed:
            self.store.save()
            self.render()
        self.after(15000, self.check_reminders)

    def _check_routine_reminder(self, now: datetime) -> bool:
        due = self.store.due_routine_reminders(now)
        if not due:
            return False
        for item in due:
            key = self.store.routine_notification_key(item, now.date())
            if key:
                self.store.notified.add(key)
        self.show_routine_notification(due)
        return True

    def show_routine_notification(self, items: list[RoutineItem]) -> None:
        try:
            self.bell()
        except tk.TclError:
            pass
        popup = tk.Toplevel(self)
        popup.title("习惯清单提醒")
        popup.configure(bg=BORDER)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.geometry(f"{self.dpi.px(350)}x{self.dpi.px(190)}")
        shell = tk.Frame(popup, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(shell, bg=ACCENT, height=5).pack(fill="x")
        tk.Label(shell, text="今天的习惯清单", bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(10, 2))
        tk.Label(shell, text=f"还有 {len(items)} 项没有完成", bg=CARD, fg=INK, font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=15)
        preview = "  ·  ".join(truncate(item.title, 8) for item in items[:3])
        if len(items) > 3:
            preview += f"  等 {len(items)} 项"
        tk.Label(shell, text=preview, bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(5, 12))
        actions = tk.Frame(shell, bg=CARD, padx=12)
        actions.pack(fill="x")
        tk.Button(actions, text="打开习惯清单", command=lambda: self._open_from_routine_notification(popup), bg=ACCENT, fg=current_theme().text_on_accent, relief="flat", bd=0, padx=14, pady=6, font=(FONT, 9, "bold"), cursor="hand2").pack(side="right")
        tk.Button(actions, text="知道了", command=popup.destroy, bg=CONTROL_BACKGROUND, fg=SUBTLE, relief="flat", bd=0, padx=12, pady=6, cursor="hand2").pack(side="right", padx=(0, 7))
        popup.update_idletasks()
        area = self.dpi.work_area()
        offset = self.dpi.px(min(len(self.notification_windows), 3) * 198)
        x = area.right - self.dpi.px(368)
        y = area.bottom - self.dpi.px(247) - offset
        x, y = clamp_to_work_area(x, y, self.dpi.px(350), self.dpi.px(190))
        popup.geometry(geometry_at(350, 190, x, y))
        self.present_overlay(popup)
        self.notification_windows.append(popup)
        popup.bind("<Destroy>", lambda _event, window=popup: self._forget_notification(window), add="+")
        popup.after(90000, lambda: popup.destroy() if popup.winfo_exists() else None)

    def _open_from_routine_notification(self, popup: tk.Toplevel) -> None:
        popup.destroy()
        self.go_today()
        if not self.agenda_open:
            self.toggle_agenda()
        self.deiconify()
        self._activate_desktop_session()
        if self.window_mode == "pinned":
            bring_to_front(self)

    def show_notification(self, event: Event) -> None:
        try:
            self.bell()
        except tk.TclError:
            pass
        popup = tk.Toplevel(self)
        popup.title("日程提醒")
        popup.configure(bg=BORDER)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.geometry(f"{self.dpi.px(340)}x{self.dpi.px(168)}")
        shell = tk.Frame(popup, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(shell, bg=event.color, height=5).pack(fill="x")
        tk.Label(shell, text="DDL 提醒", bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(10, 1))
        tk.Label(shell, text=truncate(event.title, 26), bg=CARD, fg=INK, font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=15)
        start_date = self.store.event_start_date(event)
        date_text = start_date.strftime("%m月%d日")
        if event.duration_days > 1:
            end_date = self.store.event_end_date(event)
            date_text += f"—{end_date.month}月{end_date.day}日"
        due_text = f"{date_text} {event.due_at.strftime('%H:%M')}" if event.has_time else f"{date_text} · 无具体时间"
        overdue = self.store.is_event_overdue(event)
        if overdue:
            due_text += " · 已逾期"
        tk.Label(shell, text=due_text, bg=CARD, fg=DANGER if overdue else SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(3, 8))
        actions = tk.Frame(shell, bg=CARD, padx=12)
        actions.pack(fill="x")
        tk.Button(actions, text="稍后 10 分钟", command=lambda: self.snooze_event(event, popup), bg=CONTROL_BACKGROUND, fg=SUBTLE, relief="flat", bd=0, padx=8, pady=5, cursor="hand2").pack(side="left")
        tk.Button(actions, text="完成", command=lambda: self.complete_from_notification(event, popup), bg=ACCENT, fg=current_theme().text_on_accent, relief="flat", bd=0, padx=12, pady=5, cursor="hand2").pack(side="right")
        tk.Button(actions, text="知道了", command=popup.destroy, bg=CONTROL_BACKGROUND, fg=SUBTLE, relief="flat", bd=0, padx=10, pady=5, cursor="hand2").pack(side="right", padx=(0, 6))
        popup.update_idletasks()
        area = self.dpi.work_area()
        offset = self.dpi.px(min(len(self.notification_windows), 3) * 178)
        x = area.right - self.dpi.px(358)
        y = area.bottom - self.dpi.px(225) - offset
        x, y = clamp_to_work_area(x, y, self.dpi.px(340), self.dpi.px(168))
        popup.geometry(geometry_at(340, 168, x, y))
        self.present_overlay(popup)
        self.notification_windows.append(popup)
        popup.bind("<Destroy>", lambda _event, window=popup: self._forget_notification(window), add="+")
        popup.after(90000, lambda: popup.destroy() if popup.winfo_exists() else None)

    def _forget_notification(self, popup: tk.Toplevel) -> None:
        if popup in self.notification_windows:
            self.notification_windows.remove(popup)

    def snooze_event(self, event: Event, popup: tk.Toplevel) -> None:
        event.snooze_until = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="minutes")
        self.store.save()
        popup.destroy()

    def complete_from_notification(self, event: Event, popup: tk.Toplevel) -> None:
        event.done = True
        event.snooze_until = None
        self.store.upsert(event)
        popup.destroy()
        self.render()

    def hide_to_tray(self) -> None:
        if not self.tray_icon:
            self._start_tray_icon()
        if not self.tray_icon or self.tray_icon.error or not self.tray_icon.is_available:
            messagebox.showwarning(APP_NAME, "系统通知区域图标暂时不可用，月历没有隐藏。", parent=self)
            return
        try:
            self._save_window_settings()
        except (OSError, tk.TclError):
            pass
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
            self._lower_job = None
        self.desktop_session_active = False
        self.withdraw()

    def on_close(self) -> None:
        try:
            self._save_window_settings()
        except (OSError, tk.TclError):
            pass
        if self.instance_guard:
            self.instance_guard.close()
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.destroy()


def main() -> None:
    enable_dpi_awareness()
    sys.excepthook = log_exception
    instance = SingleInstance()
    if instance.already_running:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "桌面月历已经在运行了。")
        root.destroy()
        instance.close()
        return
    app = CalendarApp(instance=instance)
    app.mainloop()


if __name__ == "__main__":
    main()
