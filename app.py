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

from dpi_utils import DpiManager, LogicalCanvas, enable_dpi_awareness, scale_px, scaled_geometry, unscale_px, work_area_for_rect


DPI_AWARENESS_MODE = enable_dpi_awareness()

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
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
    EventCategory,
    RoutineItem,
    Store,
    normalize_reminder_time,
)
from holiday_data import HolidayInfo, holiday_for
from calendar_flow_layout import (
    CalendarFlowLayout,
    build_calendar_flow_layout,
    flow_card_detail_level,
    flow_day_at,
    flow_date_range_text,
    normalize_flow_drag_range,
    normalize_global_display_mode,
)
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
from tray_icon import TrayIcon
from win_integration import (
    SingleInstance,
    bring_to_front,
    clamp_to_work_area,
    is_autostart_enabled,
    is_foreground_process,
    make_app_window,
    make_tool_window,
    raise_for_interaction,
    send_to_desktop,
    set_autostart,
)
from timeline_model import TimelineItem, TimelineMonth, TimelineSelection, build_month_timeline
from view_mode import WindowGeometry, fit_geometry_to_work_area, initial_global_geometry
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
    draw_bubble_motif,
    draw_calendar_date_ring,
    draw_calendar_date_state,
    draw_calendar_today_accent,
    draw_color_swatch,
    draw_ecology_horizon,
    glossy_control,
    rounded_rectangle,
    vertical_gradient,
    vertical_multi_gradient,
)
from ui_theme import THEMES, Theme, get_theme, normalize_theme_name


WINDOW_WIDTH = 372
OPEN_HEIGHT = 548
CLOSED_HEIGHT = 405
DDL_VISIBLE_ROWS = 2
DDL_ROW_HEIGHT = 30
DDL_REGION_CHROME_HEIGHT = 58
DDL_LIST_MIN_HEIGHT = 250
DDL_LIST_MAX_HEIGHT = 590
DDL_LIST_CHROME_HEIGHT = 128
DDL_LIST_GROUP_HEIGHT = 34
DDL_LIST_ROW_HEIGHT = 66
EVENT_STRIPE_WIDTH = 4
ROUTINE_ENTRY_LABEL = "习惯"
DDL_LIST_ENTRY_LABEL = "DDL列表"
DATE_STATE_HALF_WIDTH = 14
DATE_STATE_TOP = 1
DATE_STATE_BOTTOM = 23
DATE_RING_HALF_WIDTH = 17
DATE_RING_TOP = 0
DATE_RING_BOTTOM = 26
GLOBAL_MIN_WIDTH = 720
GLOBAL_MIN_HEIGHT = 520
GLOBAL_TITLE_WIDTH = GLOBAL_TIMELINE_LAYOUT.label_width
GLOBAL_DAY_MIN_WIDTH = GLOBAL_TIMELINE_LAYOUT.day_min_width
GLOBAL_HEADER_HEIGHT = GLOBAL_TIMELINE_LAYOUT.date_header_height
GLOBAL_ROW_HEIGHT = GLOBAL_TIMELINE_LAYOUT.row_height

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


def event_stripe_color(theme: Theme, event: Event, effective_color: Optional[str] = None) -> str:
    """Keep the card color channel independent from the event's type."""
    return theme.event_done if event.done else (effective_color or event.color)


def run_owned_modal(owner: tk.Toplevel, callback: Callable[[], object]) -> object:
    """Run a native modal above an overrideredirect/tool window safely."""
    owned_grab = False
    try:
        owned_grab = owner.grab_current() is owner
    except tk.TclError:
        pass
    if owned_grab:
        try:
            owner.grab_release()
        except tk.TclError:
            owned_grab = False
    try:
        owner.lift()
        owner.focus_force()
        owner.update_idletasks()
        return callback()
    finally:
        try:
            exists = bool(owner.winfo_exists())
        except tk.TclError:
            exists = False
        if exists:
            owner.lift()
            owner.focus_force()
            if owned_grab:
                try:
                    owner.grab_set()
                except tk.TclError:
                    pass


def owned_messagebox(
    owner: tk.Toplevel,
    dialog: Callable[..., object],
    title: str,
    message: str,
    **options: object,
) -> object:
    return run_owned_modal(
        owner,
        lambda: dialog(title, message, parent=owner, **options),
    )


def event_type_badge_style(theme: Theme, event_type: str) -> tuple[str, str, str] | None:
    """Return the compact badge palette for exceptional event types."""
    if event_type == "urgent":
        return theme.event_type_urgent, theme.event_type_urgent_background, theme.event_type_urgent_border
    if event_type == "ddl":
        return theme.event_type_ddl, theme.event_type_ddl_background, theme.event_type_ddl_border
    return None


def ddl_relative_label(deadline: datetime, now: datetime) -> str:
    if deadline < now:
        return "已逾期"
    day_delta = (deadline.date() - now.date()).days
    if day_delta == 0:
        return "今天"
    if day_delta == 1:
        return "明天"
    return f"{day_delta}天后"


def ddl_display_datetime(deadline: datetime, now: datetime) -> str:
    """Format a compact deadline without changing deadline semantics."""
    date_text = (
        f"{deadline.year}年{deadline.month}月{deadline.day}日"
        if deadline.year != now.year
        else f"{deadline.month}月{deadline.day}日"
    )
    return f"{date_text} {deadline:%H:%M}"


def ddl_list_logical_height(
    overdue_count: int,
    due_soon_count: int,
    future_count: int,
    completed_count: int,
    completed_open: bool,
) -> int:
    """Size the list for scanability while capping long collections."""
    counts = (overdue_count, due_soon_count, future_count, completed_count)
    group_count = sum(count > 0 for count in counts)
    visible_rows = overdue_count + due_soon_count + future_count
    if completed_open:
        visible_rows += completed_count
    content_height = group_count * DDL_LIST_GROUP_HEIGHT + visible_rows * DDL_LIST_ROW_HEIGHT
    return max(
        DDL_LIST_MIN_HEIGHT,
        min(DDL_LIST_MAX_HEIGHT, DDL_LIST_CHROME_HEIGHT + content_height),
    )


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
        if theme.style == "frutiger":
            background = (
                {
                    "normal": theme.accent,
                    "hover": theme.accent_hover,
                    "pressed": blend(theme.accent, theme.control_pressed, 0.24),
                }[self.state]
                if self.accented
                else {
                    "normal": theme.control_background,
                    "hover": theme.control_hover,
                    "pressed": theme.control_pressed,
                }[self.state]
            )
            border = (
                theme.header_highlight
                if self.accented and self.state == "hover"
                else theme.accent
                if self.accented
                else theme.accent_hover
                if self.state == "hover"
                else theme.control_border
            )
            self.configure(bg=self.surface_background or theme.header_background)
            glossy_control(
                self,
                width,
                height,
                background=background,
                border=border,
                highlight=theme.control_highlight,
                depth=theme.header_shadow,
                radius=theme.metrics.control_radius,
                pressed=self.state == "pressed",
            )
        elif theme.style == "aero":
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
        y_offset = 1 if self.state == "pressed" and theme.style != "frutiger" else 0
        text_color = self.foreground or (theme.text_on_accent if self.accented and theme.style != "aero" else theme.control_text)
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


class CanvasTooltip:
    """One reusable tooltip for tagged Canvas content."""

    def __init__(self, owner: tk.Widget) -> None:
        self.owner = owner
        self.window: Optional[tk.Toplevel] = None
        self.after_id: Optional[str] = None
        self.pending: Optional[tuple[str, int, int]] = None

    def schedule(self, text: str, x_root: int, y_root: int) -> None:
        self.hide()
        self.pending = (text, x_root, y_root)
        self.after_id = self.owner.after(420, self.show)

    def show(self) -> None:
        self.after_id = None
        if self.window or not self.pending or not self.owner.winfo_exists():
            return
        text, x_root, y_root = self.pending
        palette = current_theme()
        self.window = tk.Toplevel(self.owner)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(position_at(x_root + 12, y_root + 16))
        tk.Label(
            self.window,
            text=text,
            justify="left",
            bg=palette.tooltip_background,
            fg=palette.tooltip_text,
            font=(FONT, 8),
            padx=8,
            pady=6,
        ).pack()

    def hide(self, _event=None) -> None:
        if self.after_id:
            try:
                self.owner.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        self.pending = None
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
                top_highlight=(
                    blend(theme.date_hover_background, theme.control_highlight, 0.42)
                    if theme.style == "frutiger"
                    else blend(theme.date_hover_background, theme.control_highlight, 0.30)
                    if theme.style == "aero"
                    else None
                ),
            )
        if self.selected:
            selected_glass = theme.style in ("aero", "frutiger")
            selected_paper = theme.style == "paper"
            draw_calendar_date_state(
                self,
                center_x - DATE_STATE_HALF_WIDTH,
                DATE_STATE_TOP,
                center_x + DATE_STATE_HALF_WIDTH,
                DATE_STATE_BOTTOM,
                fill=theme.date_selected_background,
                border=theme.date_selected_border,
                radius=max(4, theme.metrics.date_radius - 1) if selected_glass else theme.metrics.date_radius,
                gradient_start=theme.date_selected_gradient_start if selected_glass or selected_paper else None,
                gradient_end=theme.date_selected_gradient_end if selected_glass or selected_paper else None,
                inner_border=theme.date_selected_inner_border if selected_glass or selected_paper else None,
                top_highlight=blend(
                    theme.date_selected_gradient_start,
                    theme.control_highlight,
                    0.30 if theme.style == "frutiger" else 0.18 if selected_glass else 0.10,
                ) if selected_glass or selected_paper else None,
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
                top_highlight=(
                    blend(theme.date_today_background, theme.control_highlight, 0.48)
                    if theme.style == "frutiger"
                    else blend(theme.date_today_background, theme.control_highlight, 0.34)
                    if theme.style == "aero"
                    else None
                ),
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
                # Windows glass keeps a restrained double edge. Frutiger's
                # brighter coral ring remains single so combined states do
                # not turn into an onion of outlines.
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
                highlight=(
                    blend(theme.date_selected_today, theme.control_highlight, 0.58)
                    if theme.style == "frutiger"
                    else blend(theme.date_selected_today, theme.control_highlight, 0.42)
                    if theme.style == "aero"
                    else None
                ),
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
                    0.12 if theme.style == "frutiger" else 0.15 if theme.style == "aero" else 0.22,
                )
            self.create_text(
                center_x,
                27,
                text=self.holiday.short_name,
                fill=holiday_color,
                font=(
                    FONT,
                    7 if len(self.holiday.short_name) <= 4 else 6,
                    "bold" if theme.style in ("aero", "frutiger") and self.holiday.kind != "festival" else "normal",
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
    HEIGHT = 658

    def __init__(
        self,
        master: "CalendarApp",
        selected: date,
        event: Optional[Event] = None,
        *,
        initial_duration_days: int = 1,
    ) -> None:
        super().__init__(master)
        self.master_app = master
        self.event = event
        self.title("编辑日程" if event else "新建日程")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self._closing = False
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")

        due = event.due_at if event else datetime.combine(selected, datetime.min.time()).replace(hour=23, minute=59)
        self.title_var = tk.StringVar(value=event.title if event else "")
        self.date_var = tk.StringVar(value=due.strftime("%Y-%m-%d"))
        self.time_var = tk.StringVar(value=due.strftime("%H:%M") if event and event.has_time else "")
        self.duration_var = tk.StringVar(value=str(event.duration_days if event else max(1, initial_duration_days)))
        self.skip_non_working_var = tk.BooleanVar(value=event.skip_non_working_days if event else False)
        self.end_as_ddl_var = tk.BooleanVar(value=event.end_as_ddl if event else False)
        self.event_type_var = tk.StringVar(value=event.event_type if event else "general")
        self.categories = master.store.sorted_categories()
        self.category_by_label = {category.name: category for category in self.categories}
        selected_category = master.store.category_by_id(event.category_id) if event else None
        self.category_var = tk.StringVar(value=selected_category.name if selected_category else "无分类")
        self.color_mode_var = tk.StringVar(value=event.color_mode if event else "override")
        self.color_var = tk.StringVar(
            value=master.store.effective_event_color(event) if event else COLORS["海盐蓝"]
        )
        reminder_value = event.reminder if event else None
        reminder_label = next((label for label, value in REMINDERS.items() if value == reminder_value), "不提醒")
        self.reminder_var = tk.StringVar(value=reminder_label)
        self._drag_origin: Optional[tuple[int, int, int, int]] = None
        self.color_canvases: list[tuple[tk.Canvas, str]] = []
        self._hover_color: Optional[str] = None
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
        # Close on release so destroying this modal cannot hand the matching
        # release event to a ThemeButton underneath it.
        close.unbind("<Button-1>")
        close.bind("<ButtonRelease-1>", self._close_from_pointer)
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

        category_row = tk.Frame(shell, bg=CARD)
        category_row.pack(fill="x", pady=(0, 10))
        category_col = tk.Frame(category_row, bg=CARD)
        category_col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._field_label(category_col, "事项分类")
        category_select = tk.Frame(category_col, bg=CARD)
        category_select.pack(fill="x", pady=(4, 0))
        self.category_color_preview = LogicalCanvas(
            category_select,
            dpi=master.dpi,
            width=28,
            height=28,
            bg=FIELD_BACKGROUND,
            bd=0,
            highlightthickness=0,
        )
        self.category_color_preview.pack(side="left", padx=(0, master.dpi.px(5)))
        self.category_box = ttk.Combobox(
            category_select,
            textvariable=self.category_var,
            values=("无分类", *(category.name for category in self.categories)),
            state="readonly",
            font=(FONT, 9),
        )
        self.category_box.pack(side="left", fill="x", expand=True)
        self.category_box.bind("<<ComboboxSelected>>", self._category_changed)
        manage_categories = ThemeButton(
            category_row,
            master,
            "管理分类",
            master.open_category_manager,
            width=68,
            height=31,
            font_size=8,
            surface_background=CARD,
            outlined=True,
        )
        manage_categories.pack(side="right", anchor="s")

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

        color_heading = tk.Frame(shell, bg=CARD)
        color_heading.pack(fill="x")
        tk.Label(color_heading, text="事项颜色", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(side="left")
        self.color_source_label = tk.Label(
            color_heading,
            text="",
            bg=CARD,
            fg=SUBTLE,
            font=(FONT, 8),
            anchor="e",
        )
        self.color_source_label.pack(side="right")
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
            swatch.bind("<Enter>", lambda _event, value=color: self._set_color_hover(value))
            swatch.bind("<Leave>", lambda _event: self._set_color_hover(None))
            self.color_canvases.append((swatch, color))
        custom_color = ThemeButton(
            color_row,
            master,
            "＋",
            self._choose_custom_color,
            width=28,
            height=28,
            font_size=10,
            surface_background=CARD,
            outlined=True,
        )
        custom_color.pack(side="left", padx=(0, 6))
        Tooltip(custom_color, "选择自定义颜色")
        self.follow_category_button = ThemeButton(
            color_row,
            master,
            "跟随分类",
            self._follow_category_color,
            width=68,
            height=28,
            font_size=8,
            surface_background=CARD,
            outlined=True,
        )
        self.follow_category_button.pack(side="left")
        self._draw_colors()
        self._update_color_source_controls()

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

    def _present(self) -> None:
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        self.master_app.present_modal(self, self.master_app)
        try:
            self.grab_set()
        except tk.TclError:
            return
        self.lift()
        self.title_entry.focus_force()

    def _close_from_pointer(self, _event: tk.Event) -> str:
        # Let Tk finish dispatching the release before the native child window
        # disappears, otherwise Windows can deliver the tail of the gesture to
        # the Global View underneath it.
        self.after_idle(self.close)
        return "break"

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
        self.color_mode_var.set("override")
        self.color_var.set(color)
        self._draw_colors()
        self._update_color_source_controls()

    def _set_color_hover(self, color: Optional[str]) -> None:
        self._hover_color = color
        self._draw_colors()

    def _choose_custom_color(self) -> None:
        _rgb, color = run_owned_modal(
            self,
            lambda: colorchooser.askcolor(
                color=self.color_var.get(),
                title="选择事项颜色",
                parent=self,
            ),
        )
        if color:
            self._choose_color(color)

    def _selected_category(self) -> Optional[EventCategory]:
        return self.category_by_label.get(self.category_var.get())

    def refresh_categories(self) -> None:
        selected_id = self._selected_category().id if self._selected_category() else None
        self.categories = self.master_app.store.sorted_categories()
        self.category_by_label = {category.name: category for category in self.categories}
        selected = self.master_app.store.category_by_id(selected_id)
        self.category_box.configure(values=("无分类", *(category.name for category in self.categories)))
        self.category_var.set(selected.name if selected else "无分类")
        if self.color_mode_var.get() == "inherit":
            if selected:
                self.color_var.set(selected.color)
                self._draw_colors()
            else:
                self.color_mode_var.set("override")
        self._update_color_source_controls()

    def _category_changed(self, _event: Optional[tk.Event] = None) -> None:
        category = self._selected_category()
        if category:
            self.color_mode_var.set("inherit")
            self.color_var.set(category.color)
            self._draw_colors()
        elif self.color_mode_var.get() == "inherit":
            self.color_mode_var.set("override")
        self._update_color_source_controls()

    def _follow_category_color(self) -> None:
        category = self._selected_category()
        if not category:
            return
        self.color_mode_var.set("inherit")
        self.color_var.set(category.color)
        self._draw_colors()
        self._update_color_source_controls()

    def _update_color_source_controls(self) -> None:
        category = self._selected_category()
        follows = bool(category and self.color_mode_var.get() == "inherit")
        if follows:
            source_text = f"● 跟随“{category.name}”"
            source_color = category.color
            button_text = "已跟随"
        elif category:
            source_text = "● 自定义颜色"
            source_color = self.color_var.get()
            button_text = "恢复跟随"
        else:
            source_text = "○ 无分类 · 单独颜色"
            source_color = self.master_app.theme.text_muted
            button_text = "跟随分类"
        self.color_source_label.configure(text=source_text, fg=source_color)
        self.follow_category_button.set_text(button_text)
        self.follow_category_button.foreground = (
            self.master_app.theme.text_disabled
            if not category
            else self.master_app.theme.accent
            if not follows
            else self.master_app.theme.text_secondary
        )
        self.follow_category_button.draw()
        self._draw_category_preview()

    def _draw_category_preview(self) -> None:
        canvas = self.category_color_preview
        canvas.delete("all")
        category = self._selected_category()
        color = category.color if category else self.master_app.theme.text_muted
        canvas.create_oval(
            7,
            7,
            21,
            21,
            fill=color if category else self.master_app.theme.input_background,
            outline=color,
            width=2,
        )

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
            draw_color_swatch(
                canvas,
                color,
                selected=color == selected,
                hovered=color == self._hover_color,
                theme=self.master_app.theme,
                font_family=FONT,
            )

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
            owned_messagebox(self, messagebox.showinfo, APP_NAME, "请先填写日程标题。")
            self.title_entry.focus_set()
            return
        time_text = self.time_var.get().strip()
        try:
            due, has_time = parse_event_due(self.date_var.get(), time_text)
        except ValueError:
            owned_messagebox(
                self,
                messagebox.showinfo,
                APP_NAME,
                "日期或时间格式不正确。\n日期请使用 YYYY-MM-DD；时间可以留空，填写时请使用 HH:MM。",
            )
            return
        try:
            duration_days = int(self.duration_var.get().strip())
            if not 1 <= duration_days <= 365:
                raise ValueError
        except ValueError:
            owned_messagebox(self, messagebox.showinfo, APP_NAME, "持续天数请输入 1～365 之间的整数。")
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
            category_id=self._selected_category().id if self._selected_category() else None,
            color_mode=self.color_mode_var.get(),
            event_type=self.event_type_var.get(),
            reminder=REMINDERS[self.reminder_var.get()] if has_time else None,
            notes=self.notes.get("1.0", "end").strip(),
            done=self.event.done if self.event else False,
            created_at=self.event.created_at if self.event else "",
        )
        self.master_app.upsert_event(item)
        self.close()

    def delete(self) -> None:
        if self.event and owned_messagebox(
            self,
            messagebox.askyesno,
            APP_NAME,
            f"确定删除“{self.event.title}”？",
        ):
            self.master_app.delete_event(self.event.id)
            self.close()

    def close(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if self.master_app.editor_window is self:
            self.master_app.editor_window = None
        try:
            if self.grab_current() is self:
                self.grab_release()
        except (AttributeError, tk.TclError):
            pass
        self.destroy()
        detail = self.master_app.day_detail_window
        if detail and detail.winfo_exists():
            detail.refresh()
            self.master_app.after(60, lambda: self.master_app.present_overlay(detail))
        else:
            self.master_app.after(120, self.master_app.restore_window_mode_if_idle)


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


class CategoryEditor(tk.Toplevel):
    WIDTH = 360
    HEIGHT = 280

    def __init__(self, master: "CalendarApp", category: Optional[EventCategory] = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.category = category
        self.title("编辑事项分类" if category else "新增事项分类")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")
        self._closing = False
        self.name_var = tk.StringVar(value=category.name if category else "")
        self.color_var = tk.StringVar(value=category.color if category else COLORS["海盐蓝"])
        self.color_canvases: list[tuple[tk.Canvas, str]] = []
        self._hover_color: Optional[str] = None

        shell = tk.Frame(self, bg=CARD, padx=20)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="编辑事项分类" if category else "新增事项分类",
            bg=CARD,
            fg=INK,
            font=(FONT, 12, "bold"),
        ).pack(side="left", pady=12)
        button_label(header, "×", self.close, width=2, bg=CARD, font_size=13).pack(side="right", pady=8)

        tk.Label(shell, text="分类名称", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        self.name_entry = tk.Entry(
            shell,
            textvariable=self.name_var,
            bg=FIELD_BACKGROUND,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(FONT, 10),
        )
        self.name_entry.pack(fill="x", pady=(4, 14), ipady=6)

        tk.Label(shell, text="分类颜色", bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")
        color_row = tk.Frame(shell, bg=CARD)
        color_row.pack(fill="x", pady=(5, 18))
        color_values = list(COLORS.values())
        if self.color_var.get() not in color_values:
            color_values.append(self.color_var.get())
        for color in color_values:
            swatch = LogicalCanvas(color_row, dpi=master.dpi, width=28, height=28, bg=CARD, bd=0, highlightthickness=0, cursor="hand2")
            swatch.pack(side="left", padx=(0, 7))
            swatch.bind("<Button-1>", lambda _event, value=color: self._choose_color(value))
            swatch.bind("<Enter>", lambda _event, value=color: self._set_color_hover(value))
            swatch.bind("<Leave>", lambda _event: self._set_color_hover(None))
            self.color_canvases.append((swatch, color))
        custom_color = ThemeButton(color_row, master, "＋", self._choose_custom_color, width=28, height=28, font_size=10, surface_background=CARD, outlined=True)
        custom_color.pack(side="left")
        Tooltip(custom_color, "选择自定义颜色")
        self._draw_colors()

        actions = tk.Frame(shell, bg=CARD)
        actions.pack(fill="x", side="bottom", pady=(0, 16))
        ThemeButton(actions, master, "保存", self.save, width=64, height=31, font_size=9, surface_background=CARD, accented=True).pack(side="right")
        ThemeButton(actions, master, "取消", self.close, width=58, height=31, font_size=9, surface_background=CARD, outlined=True).pack(side="right", padx=(0, 7))

        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Control-Return>", lambda _event: self.save())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.update_idletasks()
        center_toplevel(self, master, self.WIDTH, self.HEIGHT, y_offset=36)

    def _present(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        parent = self.master_app.category_manager or self.master_app
        self.master_app.present_modal(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            return
        self.lift()
        self.name_entry.focus_force()

    def _choose_color(self, color: str) -> None:
        self.color_var.set(color)
        self._draw_colors()

    def _set_color_hover(self, color: Optional[str]) -> None:
        self._hover_color = color
        self._draw_colors()

    def _choose_custom_color(self) -> None:
        _rgb, color = run_owned_modal(
            self,
            lambda: colorchooser.askcolor(color=self.color_var.get(), title="选择分类颜色", parent=self),
        )
        if color:
            self._choose_color(color)

    def _draw_colors(self) -> None:
        selected = self.color_var.get()
        for canvas, color in self.color_canvases:
            draw_color_swatch(
                canvas,
                color,
                selected=color == selected,
                hovered=color == self._hover_color,
                theme=self.master_app.theme,
                font_family=FONT,
            )

    def save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            owned_messagebox(self, messagebox.showinfo, APP_NAME, "请填写分类名称。")
            self.name_entry.focus_set()
            return
        duplicate = next(
            (
                item
                for item in self.master_app.store.categories
                if item.name.casefold() == name.casefold() and (not self.category or item.id != self.category.id)
            ),
            None,
        )
        if duplicate:
            owned_messagebox(self, messagebox.showinfo, APP_NAME, "已经存在同名分类。")
            self.name_entry.focus_set()
            return
        category = EventCategory(
            id=self.category.id if self.category else str(uuid.uuid4()),
            name=name,
            color=self.color_var.get(),
            sort_order=self.category.sort_order if self.category else len(self.master_app.store.categories),
            created_at=self.category.created_at if self.category else "",
        )
        self.master_app.store.upsert_category(category)
        self.master_app.category_data_changed()
        self.close()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.master_app.category_editor is self:
            self.master_app.category_editor = None
        try:
            if self.grab_current() is self:
                self.grab_release()
        except (AttributeError, tk.TclError):
            pass
        self.destroy()
        manager = self.master_app.category_manager
        if manager and manager.winfo_exists():
            manager.refresh()
            self.master_app.after_idle(manager._present)


class CategoryManager(tk.Toplevel):
    WIDTH = 400
    HEIGHT = 480

    def __init__(self, master: "CalendarApp") -> None:
        super().__init__(master)
        self.master_app = master
        self.title("事项分类")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.geometry(f"{scale_px(self.WIDTH)}x{scale_px(self.HEIGHT)}")
        self._closing = False

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=18, pady=10)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left")
        tk.Label(title_box, text="事项分类", bg=CARD, fg=INK, font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(title_box, text="一个事项最多属于一个分类", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(anchor="w")
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
        tk.Label(footer, text="删除分类不会删除事项", bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        ThemeButton(footer, master, "＋ 新建分类", lambda: master.open_category_editor(), width=88, height=32, font_size=9, surface_background=CARD, accented=True).pack(side="right")

        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.update_idletasks()
        center_toplevel(self, master, self.WIDTH, self.HEIGHT, y_offset=24)
        self.after_idle(self._present)

    def _present(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        parent = self.master_app.editor_window or self.master_app
        self.master_app.present_modal(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            return
        self.lift()
        self.focus_force()

    def refresh(self) -> None:
        for child in self.list_inner.winfo_children():
            child.destroy()
        categories = self.master_app.store.sorted_categories()
        if not categories:
            tk.Label(self.list_inner, text="还没有事项分类\n点击下方按钮创建第一个分类", bg=CARD, fg=FAINT, font=(FONT, 9), justify="center", pady=48).pack(fill="x")
            return
        counts = {category.id: 0 for category in categories}
        for event in self.master_app.store.events:
            if event.category_id in counts:
                counts[event.category_id] += 1
        for category in categories:
            row = tk.Frame(self.list_inner, bg=CARD)
            row.pack(fill="x", padx=1)
            marker = LogicalCanvas(row, dpi=self.master_app.dpi, width=28, height=42, bg=CARD, bd=0, highlightthickness=0)
            marker.pack(side="left", padx=(4, 2))
            marker.create_oval(8, 13, 20, 25, fill=category.color, outline=blend(category.color, INK, 0.18))
            content = tk.Frame(row, bg=CARD, padx=6, pady=8)
            content.pack(side="left", fill="both", expand=True)
            tk.Label(content, text=category.name, bg=CARD, fg=INK, font=(FONT, 9, "bold"), anchor="w").pack(fill="x")
            tk.Label(content, text=f"{counts[category.id]} 个事项", bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))
            ThemeButton(row, self.master_app, "删除", lambda item=category: self._delete(item), width=42, height=27, font_size=8, foreground=DANGER, surface_background=CARD).pack(side="right", padx=(2, 6))
            ThemeButton(row, self.master_app, "编辑", lambda item=category: self.master_app.open_category_editor(item), width=42, height=27, font_size=8, surface_background=CARD).pack(side="right")
            tk.Frame(self.list_inner, bg=BORDER, height=1).pack(fill="x", padx=(32, 6))

    def _delete(self, category: EventCategory) -> None:
        if not owned_messagebox(
            self,
            messagebox.askyesno,
            APP_NAME,
            f"确定删除分类“{category.name}”？\n\n所属事项会保留并转为无分类，当前显示颜色也会保留。",
        ):
            return
        self.master_app.store.delete_category(category.id)
        self.master_app.category_data_changed()
        self.refresh()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.master_app.category_editor and self.master_app.category_editor.winfo_exists():
            self.master_app.category_editor.close()
        if self.master_app.category_manager is self:
            self.master_app.category_manager = None
        try:
            if self.grab_current() is self:
                self.grab_release()
        except (AttributeError, tk.TclError):
            pass
        self.destroy()
        editor = self.master_app.editor_window
        if editor and editor.winfo_exists():
            editor.refresh_categories()
            self.master_app.after(50, editor._present)
        else:
            self.master_app.after(120, self.master_app.restore_window_mode_if_idle)


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
            event_stripe_color(current_theme(), item, self.master_app.store.effective_event_color(item)),
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


class DDLListDialog(tk.Toplevel):
    WIDTH = 440
    HEIGHT = DDL_LIST_MAX_HEIGHT

    def __init__(self, master: "CalendarApp") -> None:
        super().__init__(master)
        self.master_app = master
        self.completed_open = False
        self.logical_height = self.HEIGHT
        self.title("DDL列表")
        self.configure(bg=master.theme.window_border_outer)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{master.dpi.px(self.WIDTH)}x{master.dpi.px(self.HEIGHT)}")

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=18, pady=10)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text="DDL列表", bg=CARD, fg=INK, font=(FONT, 13, "bold"), anchor="w").pack(anchor="w")
        self.summary_label = tk.Label(title_box, text="", bg=CARD, fg=FAINT, font=(FONT, 8), anchor="w")
        self.summary_label.pack(anchor="w")
        button_label(header, "×", self.close, width=2, bg=CARD, font_size=13).pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        list_shell = tk.Frame(shell, bg=CARD, padx=12, pady=10)
        list_shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(list_shell, bg=CARD, bd=0, highlightthickness=0, height=master.dpi.px(1))
        self.scrollbar = ttk.Scrollbar(
            list_shell,
            orient="vertical",
            command=self.canvas.yview,
            style="DDL.Vertical.TScrollbar",
        )
        self.list_inner = tk.Frame(self.canvas, bg=CARD)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.list_window, width=event.width))
        self.canvas.bind("<MouseWheel>", self._scroll)

        footer = tk.Frame(shell, bg=CARD, padx=15, pady=10)
        footer.pack(side="bottom", fill="x")
        self.footer_hint = tk.Label(
            footer,
            text="",
            bg=CARD,
            fg=FAINT,
            font=(FONT, 8),
        )
        self.footer_hint.pack(side="left")

        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.update_idletasks()
        self.after_idle(lambda: master.present_overlay(self))

    def refresh(self) -> None:
        if not self.winfo_exists():
            return
        scroll_position = self.canvas.yview()[0] if self.canvas.bbox("all") else 0.0
        for child in self.list_inner.winfo_children():
            child.destroy()
        groups = self.master_app.store.complete_ddl_groups()
        unfinished_count = len(groups.overdue) + len(groups.due_soon) + len(groups.future)
        self.summary_label.configure(text=f"未完成 {unfinished_count} 项 · 已完成 {len(groups.completed)} 项")
        self.footer_hint.configure(
            text="点击事项可编辑，复选框可完成或取消完成"
            if groups.total
            else "新增或标记 DDL 后会显示在这里"
        )
        self._resize_for_groups(groups)
        if groups.total == 0:
            empty = tk.Frame(self.list_inner, bg=CARD)
            empty.pack(fill="both", expand=True, pady=56)
            tk.Label(
                empty,
                text="暂无 DDL",
                bg=CARD,
                fg=current_theme().text_secondary,
                font=(FONT, 10, "bold"),
            ).pack()
            tk.Label(
                empty,
                text="最近没有截止事项",
                bg=CARD,
                fg=FAINT,
                font=(FONT, 8),
            ).pack(pady=(5, 0))
        else:
            self._build_group("已逾期", groups.overdue, "overdue")
            self._build_group("24小时内", groups.due_soon, "due_soon")
            self._build_group("未来 DDL", groups.future, "future")
            self._build_group("已完成", groups.completed, "completed", collapsible=True)
        self.after_idle(lambda: self._finish_refresh(scroll_position))

    def _resize_for_groups(self, groups) -> None:
        self.logical_height = ddl_list_logical_height(
            len(groups.overdue),
            len(groups.due_soon),
            len(groups.future),
            len(groups.completed),
            self.completed_open,
        )
        self.geometry(
            f"{self.master_app.dpi.px(self.WIDTH)}x{self.master_app.dpi.px(self.logical_height)}"
        )
        self.update_idletasks()
        center_toplevel(self, self.master_app, self.WIDTH, self.logical_height, y_offset=16)

    def _finish_refresh(self, scroll_position: float) -> None:
        if not self.winfo_exists():
            return
        self._update_scrollbar()
        self.canvas.yview_moveto(scroll_position if self.scrollbar.winfo_manager() else 0.0)

    @staticmethod
    def _group_palette(theme: Theme, group_name: str) -> tuple[str, str, str]:
        if group_name == "overdue":
            return theme.danger, theme.ddl_overdue_background, theme.ddl_pinned_border
        if group_name == "due_soon":
            return theme.event_type_urgent, theme.ddl_due_background, theme.event_type_urgent_border
        if group_name == "completed":
            return theme.text_done, theme.card_done_background, theme.divider
        return theme.text_secondary, theme.panel_secondary, theme.divider

    def _build_group(
        self,
        title: str,
        items: tuple[Event, ...],
        group_name: str,
        *,
        collapsible: bool = False,
    ) -> None:
        if not items:
            return
        theme = current_theme()
        accent, hover_background, separator_color = self._group_palette(theme, group_name)
        cursor = "hand2" if collapsible else "arrow"
        header = tk.Frame(self.list_inner, bg=CARD, padx=4, pady=6, cursor=cursor)
        header.pack(fill="x", pady=(7, 2), padx=1)
        marker = tk.Frame(header, bg=accent, width=2, cursor=cursor)
        marker.pack(side="left", fill="y", padx=(0, 7))
        marker.pack_propagate(False)
        label = tk.Label(
            header,
            text=title,
            bg=CARD,
            fg=accent,
            font=(FONT, 9, "bold"),
            cursor=cursor,
        )
        label.pack(side="left")
        count_text = f"{len(items)} 项"
        if collapsible:
            count_text += "  ⌄" if self.completed_open else "  ›"
        count = tk.Label(
            header,
            text=count_text,
            bg=CARD,
            fg=theme.text_muted,
            font=(FONT, 8),
            cursor=cursor,
            padx=4,
        )
        count.pack(side="right")
        tk.Frame(self.list_inner, bg=separator_color, height=1).pack(fill="x", padx=1, pady=(0, 5))
        if collapsible:
            widgets = (header, marker, label, count)
            for widget in widgets:
                widget.bind(
                    "<Enter>",
                    lambda _event, ws=widgets, bg=hover_background: self._set_group_hover(ws, bg),
                )
                widget.bind(
                    "<Leave>",
                    lambda _event, ws=widgets: self._set_group_hover(ws, CARD),
                )
                widget.bind("<Button-1>", lambda _event: self._toggle_completed())
        if collapsible and not self.completed_open:
            return
        for item in items:
            self._build_event_row(item, group_name)

    @staticmethod
    def _set_group_hover(widgets: tuple[tk.Widget, ...], background: str) -> None:
        for widget in widgets:
            if isinstance(widget, tk.Frame) and int(widget.cget("width") or 0) == 2:
                continue
            widget.configure(bg=background)

    def _build_event_row(self, item: Event, group_name: str) -> None:
        theme = current_theme()
        if item.done:
            background = theme.card_done_background
            border = theme.divider
        elif group_name == "overdue":
            background = theme.ddl_overdue_background
            border = theme.ddl_pinned_border
        elif group_name == "due_soon":
            background = theme.ddl_due_background
            border = theme.event_type_urgent_border
        else:
            background = theme.schedule_card_background
            border = theme.schedule_card_border
        row = tk.Frame(
            self.list_inner,
            bg=background,
            highlightthickness=1,
            highlightbackground=border,
            cursor="hand2",
        )
        row.pack(fill="x", pady=(0, 7), padx=1)
        stripe = tk.Frame(
            row,
            bg=event_stripe_color(theme, item, self.master_app.store.effective_event_color(item)),
            width=EVENT_STRIPE_WIDTH,
            cursor="hand2",
        )
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        check = TaskCheck(
            row,
            self.master_app,
            done=item.done,
            background=background,
            command=lambda event=item: self.master_app.toggle_done(event),
            height=58,
        )
        check.pack(side="left", fill="y", padx=(3, 0))
        content = tk.Frame(row, bg=background, padx=2, pady=6, cursor="hand2")
        content.pack(side="left", fill="both", expand=True)
        if theme.style == "aero":
            tk.Frame(
                content,
                bg=blend(background, theme.control_highlight, 0.58),
                height=1,
            ).pack(fill="x", pady=(0, 3))
        title_row = tk.Frame(content, bg=background, cursor="hand2")
        title_row.pack(fill="x")
        title = tk.Label(
            title_row,
            text=truncate(item.title, 31),
            bg=background,
            fg=theme.text_done if item.done else theme.text_primary,
            font=(FONT, 9, "overstrike" if item.done else "normal"),
            anchor="w",
            cursor="hand2",
        )
        title.pack(side="left", fill="x", expand=True)

        source_badge = None
        if item.end_as_ddl:
            badge_style = event_type_badge_style(theme, item.event_type)
            if badge_style:
                badge_text, badge_background, badge_border = badge_style
            else:
                badge_text = theme.event_type_general
                badge_background = theme.panel_secondary
                badge_border = theme.divider
            source_badge = tk.Label(
                title_row,
                text=EVENT_TYPE_LABELS[item.event_type],
                bg=badge_background,
                fg=badge_text,
                font=(FONT, 7),
                padx=4,
                highlightthickness=1,
                highlightbackground=badge_border,
                cursor="hand2",
            )
            source_badge.pack(side="right", padx=(4, 0))

        deadline = self.master_app.store.event_ends_at(item)
        reference = datetime.now()
        date_text = ddl_display_datetime(deadline, reference)
        if not item.has_time:
            date_text = date_text.rsplit(" ", 1)[0]
        status_text = "已完成" if item.done else ddl_relative_label(deadline, reference)
        meta_row = tk.Frame(content, bg=background, cursor="hand2")
        meta_row.pack(fill="x", pady=(3, 0))
        date_label = tk.Label(
            meta_row,
            text=date_text,
            bg=background,
            fg=theme.text_done if item.done else theme.schedule_time_text,
            font=(FONT, 8),
            anchor="w",
            cursor="hand2",
        )
        date_label.pack(side="left")

        status_background = background
        status_border = background
        status_color = theme.schedule_time_text
        if item.done:
            status_color = theme.text_done
        elif group_name == "overdue":
            status_background = theme.danger_soft
            status_border = theme.event_type_ddl_border
            status_color = theme.danger
        elif group_name == "due_soon":
            status_background = theme.event_type_urgent_background
            status_border = theme.event_type_urgent_border
            status_color = theme.event_type_urgent
        status = tk.Label(
            meta_row,
            text=status_text,
            bg=status_background,
            fg=status_color,
            font=(FONT, 7, "bold" if group_name in ("overdue", "due_soon") and not item.done else "normal"),
            padx=4,
            highlightthickness=1 if status_border != background else 0,
            highlightbackground=status_border,
            cursor="hand2",
        )
        status.pack(side="left", padx=(7, 0))

        actions = tk.Frame(row, bg=background, padx=5, cursor="hand2")
        actions.pack(side="right", fill="y")
        open_hint = tk.Label(actions, text="›", bg=background, fg=theme.text_muted, font=(FONT, 12), cursor="hand2")
        open_hint.pack(pady=(7, 0))
        delete = tk.Label(actions, text="删除", bg=background, fg=theme.text_muted, font=(FONT, 7), cursor="hand2")
        delete.pack(pady=(0, 4))

        surface_widgets = [row, check, content, title_row, title, meta_row, date_label, actions, open_hint, delete]
        interactive = [row, stripe, content, title_row, title, meta_row, date_label, status, actions, open_hint]
        if source_badge:
            interactive.append(source_badge)
        for widget in interactive + [check, delete]:
            widget.bind(
                "<Enter>",
                lambda _event, widgets=surface_widgets, task_check=check, normal=background, edge=border: self._set_row_state(
                    row, widgets, task_check, normal, edge, "hover"
                ),
            )
            widget.bind(
                "<Leave>",
                lambda _event, widgets=surface_widgets, task_check=check, normal=background, edge=border: self._set_row_state(
                    row, widgets, task_check, normal, edge, "normal"
                ),
            )
            widget.bind("<MouseWheel>", self._scroll)
        for widget in interactive:
            widget.bind(
                "<Button-1>",
                lambda _event, event=item, widgets=surface_widgets, task_check=check, normal=background, edge=border: self._open_item(
                    event, row, widgets, task_check, normal, edge
                ),
            )
        delete.bind("<Button-1>", lambda _event, event=item: self.master_app._confirm_delete(event, parent=self))
        delete.bind("<Enter>", lambda _event: delete.configure(fg=theme.danger), add="+")
        delete.bind("<Leave>", lambda _event: delete.configure(fg=theme.text_muted), add="+")

    def _set_row_state(
        self,
        row: tk.Frame,
        widgets: list[tk.Widget],
        check: TaskCheck,
        normal_background: str,
        normal_border: str,
        state: str,
    ) -> None:
        theme = current_theme()
        if state == "pressed":
            background = blend(normal_background, theme.control_pressed, 0.24)
            border = theme.control_border
        elif state == "hover":
            background = blend(normal_background, theme.schedule_card_hover, 0.56)
            border = theme.control_border
        else:
            background = normal_background
            border = normal_border
        row.configure(highlightbackground=border)
        for widget in widgets:
            widget.configure(bg=background)
        check.draw()

    def _open_item(
        self,
        item: Event,
        row: tk.Frame,
        widgets: list[tk.Widget],
        check: TaskCheck,
        background: str,
        border: str,
    ) -> None:
        self._set_row_state(row, widgets, check, background, border, "pressed")
        self.master_app.open_editor(item)

    def _toggle_completed(self) -> None:
        self.completed_open = not self.completed_open
        self.refresh()

    def _scroll(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _update_scrollbar(self) -> None:
        if not self.winfo_exists():
            return
        self.list_inner.update_idletasks()
        bbox = self.canvas.bbox("all")
        needs_scroll = bool(bbox and bbox[3] > self.canvas.winfo_height())
        if needs_scroll and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y")
        elif not needs_scroll and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()

    def close(self) -> None:
        if self.master_app.ddl_list_window is self:
            self.master_app.ddl_list_window = None
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
            tk.Frame(row, bg=master.store.effective_event_color(item), width=4).pack(side="left", fill="y", padx=(0, 8))
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
        # The desktop gadget remains the startup mode even if the previous
        # session ended while the large workspace was open.
        self.view_mode = "compact"
        self.global_display_mode = normalize_global_display_mode(self.store.settings.get("global_display_mode"))
        self.timeline_selection = TimelineSelection()
        self.timeline_model: Optional[TimelineMonth] = None
        self.calendar_flow_layout: Optional[CalendarFlowLayout] = None
        self._global_normal_geometry: Optional[WindowGeometry] = None
        self._geometry_save_job: Optional[str] = None
        self._global_render_job: Optional[str] = None
        self._global_resize_job: Optional[str] = None
        self._global_day_width = 0
        self._global_content_width = 0
        self._global_content_height = 0
        self._global_flow_column_width = 0
        self._global_flow_week_height = 0
        self._global_flow_drag_start: Optional[date] = None
        self._global_flow_drag_end: Optional[date] = None
        self._global_flow_drag_anchor: Optional[date] = None
        self._global_flow_drag_moved = False
        self._global_flow_hover_item_id: Optional[str] = None
        self._global_flow_card_styles: dict[str, list[tuple[str, str, str, int]]] = {}
        self._global_category_filter_ids: Optional[set[str]] = None
        self._global_include_uncategorized = True
        self._global_category_sidebar_open = True
        self._global_detail_visible = True
        self._global_tooltip: Optional[CanvasTooltip] = None
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
        self.category_manager: Optional[CategoryManager] = None
        self.category_editor: Optional[CategoryEditor] = None
        self.day_detail_window: Optional[DayDetailDialog] = None
        self.ddl_list_window: Optional[DDLListDialog] = None
        self.update_dialog: Optional[UpdateProgressDialog] = None
        self.update_busy = False
        self._dpi_check_job: Optional[str] = None
        self._dpi_rebuilding = False
        self.show_holidays = bool(self.store.settings.get("show_holidays", True))
        self.quick_color = COLORS["海盐蓝"]
        self.quick_event_type = "general"
        self.desktop_session_active = False
        self._window_ready = False
        self._startup_foreground_done = False
        self._startup_foreground_active = False
        self._startup_foreground_restore_job: Optional[str] = None
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
        self.bind("<Configure>", self._schedule_view_geometry_save, add="+")
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
        style.configure(
            "DDL.Vertical.TScrollbar",
            width=self.dpi.px(9),
            background=self.theme.control_background,
            troughcolor=self.theme.panel_background,
            bordercolor=self.theme.divider,
            arrowcolor=self.theme.control_text,
            relief="flat",
        )
        style.map(
            "DDL.Vertical.TScrollbar",
            background=[
                ("pressed", self.theme.control_pressed),
                ("active", self.theme.control_hover),
            ],
            arrowcolor=[("disabled", self.theme.text_disabled)],
        )
        for orientation in ("Vertical", "Horizontal"):
            style_name = f"Global.{orientation}.TScrollbar"
            style.configure(
                style_name,
                width=self.dpi.px(8),
                background=self.theme.control_background,
                troughcolor=self.theme.panel_background,
                bordercolor=self.theme.divider,
                arrowcolor=self.theme.control_text,
                relief="flat",
            )
            style.map(
                style_name,
                background=[
                    ("pressed", self.theme.control_pressed),
                    ("active", self.theme.control_hover),
                ],
                arrowcolor=[("disabled", self.theme.text_disabled)],
            )

    def _apply_theme_opacity(self, value: Optional[float] = None) -> None:
        if value is None:
            try:
                value = float(self.store.settings.get("opacity", 1.0))
            except (TypeError, ValueError):
                value = 1.0
        value = min(1.0, max(0.90, value))
        # Tk applies alpha to every child uniformly. Simulated glass themes
        # therefore use an opaque composite so desktop content cannot ghost
        # through text and controls.
        self.attributes("-alpha", 1.0 if self.theme.style in ("aero", "frutiger") else value)

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        log_exception(exc_type, exc_value, exc_traceback)
        try:
            messagebox.showerror(APP_NAME, f"操作没有完成，错误已经记录。\n\n{exc_value}", parent=self)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        if self.view_mode == "global":
            self._build_global_ui()
            return
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
            highlightthickness=dp(1) if theme.style in ("aero", "frutiger") else 0,
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
        widths = (25, 25, 25, 37, 29, 25, 25) if compact else (27, 27, 27, 39, 31, 27, 27)
        control_height = 25 if compact else 27
        control_y = 15 if compact else 14
        previous = ThemeButton(self.header, self, "‹", lambda: self.change_month(-1), width=widths[0], height=control_height, font_size=13 if compact else 14)
        today = ThemeButton(self.header, self, "今", self.go_today, width=widths[1], height=control_height, font_size=9)
        following = ThemeButton(self.header, self, "›", lambda: self.change_month(1), width=widths[2], height=control_height, font_size=13 if compact else 14)
        self.mode_button = ThemeButton(self.header, self, "桌面", self.toggle_window_mode, width=widths[3], height=control_height, font_size=8)
        self.menu_button = ThemeButton(self.header, self, "···", self.show_main_menu, width=widths[4], height=control_height, font_size=9 if compact else 10)
        global_view = ThemeButton(self.header, self, "□", self.toggle_global_view, width=widths[5], height=control_height, font_size=10)
        minimize = ThemeButton(self.header, self, "−", self.hide_to_tray, width=widths[6], height=control_height, font_size=10)
        controls = (previous, today, following, self.mode_button, self.menu_button, global_view, minimize)
        x = WINDOW_WIDTH - 13 - sum(widths) - 5 * 2
        for control, control_width in zip(controls, widths):
            control.place(x=dp(x), y=dp(control_y), width=dp(control_width), height=dp(control_height))
            x += control_width + 2
        Tooltip(previous, "上个月（滚轮向上 / PgUp）")
        Tooltip(today, "回到今天（Ctrl+T）")
        Tooltip(following, "下个月（滚轮向下 / PgDn）")
        Tooltip(self.mode_button, "桌面模式空闲时不遮挡应用；点击月历会临时前置")
        Tooltip(global_view, "打开全局视图")
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
            foreground=None if theme.style != "aero" else theme.control_text,
            surface_background=theme.schedule_background,
            accented=theme.style != "aero",
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
            foreground=theme.text_secondary if theme.style != "aero" else theme.control_text,
            surface_background=theme.schedule_background,
            outlined=True,
        )
        routines.pack(side="right", pady=dp(4), padx=(0, dp(3)))
        Tooltip(add, "打开当天详情并管理事项")
        Tooltip(routines, "管理习惯清单：工作日习惯与一次性待办")
        for widget in (self.agenda_bar, self.agenda_toggle, self.agenda_title, self.agenda_count):
            widget.bind("<Button-1>", lambda _event: self.toggle_agenda())

        self._build_agenda_body()

    def _build_global_ui(self) -> None:
        """Build the complete Global workspace without changing Compact layout."""
        theme = self.theme
        dp = self.dpi.px
        self.configure(bg=theme.window_shadow)
        self.window_frame = tk.Frame(self, bg=theme.window_border_outer)
        self.window_frame.pack(fill="both", expand=True, padx=dp(theme.metrics.shadow_depth), pady=dp(theme.metrics.shadow_depth))
        self.inner_frame = tk.Frame(self.window_frame, bg=theme.window_border_inner)
        self.inner_frame.pack(fill="both", expand=True, padx=dp(theme.metrics.outer_border_width), pady=dp(theme.metrics.outer_border_width))
        self.shell = tk.Frame(self.inner_frame, bg=theme.panel_background)
        self.shell.pack(fill="both", expand=True, padx=dp(theme.metrics.inner_border_width), pady=dp(theme.metrics.inner_border_width))

        toolbar = tk.Frame(
            self.shell,
            bg=theme.header_background,
            highlightthickness=dp(1) if theme.style in ("aero", "frutiger") else 0,
            highlightbackground=theme.header_highlight,
            padx=dp(14),
            pady=dp(9),
        )
        toolbar.pack(fill="x")
        title_box = tk.Frame(toolbar, bg=theme.header_background)
        title_box.pack(side="left", fill="x", expand=True)
        self.global_month_label = tk.Label(
            title_box,
            text="",
            bg=theme.header_background,
            fg=theme.header_text,
            font=(FONT, 14, "bold"),
            anchor="w",
        )
        self.global_month_label.pack(side="left")
        self.global_view_title_label = tk.Label(
            title_box,
            text="全局时间轴",
            bg=theme.header_background,
            fg=theme.header_subtext,
            font=(FONT, 8),
        )
        self.global_view_title_label.pack(side="left", padx=(dp(10), 0), pady=(dp(5), 0))
        if theme.style == "frutiger":
            motif = LogicalCanvas(title_box, dpi=self.dpi, width=42, height=30, bg=theme.header_background, bd=0, highlightthickness=0)
            motif.pack(side="left", padx=(dp(8), 0))
            draw_bubble_motif(
                motif,
                ((10, 13, 6), (25, 8, 4), (31, 20, 3)),
                outline=blend(theme.header_gradient_mid, theme.header_highlight, 0.64),
                highlight=theme.header_highlight,
                accent=theme.environment_accent,
            )

        minimize = ThemeButton(toolbar, self, "−", self.hide_to_tray, width=29, height=29, font_size=11, surface_background=theme.header_background)
        restore = ThemeButton(toolbar, self, "紧凑视图", self.return_to_compact_view, width=68, height=29, font_size=8, surface_background=theme.header_background, outlined=True)
        self.menu_button = ThemeButton(toolbar, self, "···", self.show_main_menu, width=34, height=29, font_size=10, surface_background=theme.header_background)
        self.mode_button = ThemeButton(toolbar, self, "窗口置顶", self.toggle_window_mode, width=62, height=29, font_size=8, surface_background=theme.header_background, outlined=True)
        ddl_button = ThemeButton(toolbar, self, "DDL列表", self.open_ddl_list, width=58, height=29, font_size=8, surface_background=theme.header_background, outlined=True)
        habit_button = ThemeButton(toolbar, self, ROUTINE_ENTRY_LABEL, self.open_routine_manager, width=46, height=29, font_size=8, surface_background=theme.header_background, outlined=True)
        create_button = ThemeButton(toolbar, self, "+ 新建事项", self.open_new_event, width=82, height=29, font_size=8, surface_background=theme.header_background, accented=True)
        following = ThemeButton(toolbar, self, "›", lambda: self.change_month(1), width=29, height=29, font_size=13, surface_background=theme.header_background)
        today = ThemeButton(toolbar, self, "今", self.go_today, width=29, height=29, font_size=9, surface_background=theme.header_background)
        previous = ThemeButton(toolbar, self, "‹", lambda: self.change_month(-1), width=29, height=29, font_size=13, surface_background=theme.header_background)
        for control in (minimize, restore, self.menu_button, self.mode_button, ddl_button, habit_button, create_button, following, today, previous):
            control.pack(side="right", padx=(dp(4), 0))
        Tooltip(previous, "上个月（PgUp）")
        Tooltip(today, "回到今天（Ctrl+T）")
        Tooltip(following, "下个月（PgDn）")
        Tooltip(create_button, "在当前选中日期新建事项")
        Tooltip(habit_button, "打开习惯清单")
        Tooltip(ddl_button, "查看完整 DDL 列表")
        Tooltip(self.mode_button, "切换桌面 / 置顶模式")
        Tooltip(self.menu_button, "设置、主题、更新与数据工具")
        Tooltip(restore, "返回紧凑视图")
        Tooltip(minimize, "隐藏到通知区域")

        quick_surface = blend(theme.schedule_background, theme.environment_haze, 0.18) if theme.style == "frutiger" else theme.schedule_background
        quick_frame = tk.Frame(self.shell, bg=quick_surface, padx=dp(14), pady=dp(8))
        quick_frame.pack(fill="x")
        tk.Label(quick_frame, text="快速添加", bg=quick_surface, fg=theme.text_secondary, font=(FONT, 8, "bold")).pack(side="left", padx=(0, dp(9)))
        self._quick_entry_hovered = False
        self.quick_var = tk.StringVar(value="")
        self.quick_entry = tk.Entry(
            quick_frame,
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
            quick_frame,
            self,
            "一般 · 选项",
            self.show_quick_options,
            width=72,
            height=29,
            font_size=8,
            surface_background=quick_surface,
            outlined=True,
        )
        self.quick_options_button.pack(side="right", padx=(dp(6), 0))
        self._refresh_quick_options_button()

        info_bar = tk.Frame(self.shell, bg=theme.panel_secondary, padx=dp(14), pady=dp(5))
        info_bar.pack(fill="x")
        self.global_summary_label = tk.Label(info_bar, text="", bg=theme.panel_secondary, fg=theme.text_secondary, font=(FONT, 8), anchor="w")
        self.global_summary_label.pack(side="left")
        day_detail_button = ThemeButton(info_bar, self, "当天详情", self.open_day_detail, width=66, height=23, font_size=8, surface_background=theme.panel_secondary, outlined=True)
        day_detail_button.pack(side="right", padx=(dp(8), 0))
        mode_switch = tk.Frame(
            info_bar,
            bg=theme.divider,
            padx=dp(1),
            pady=dp(1),
        )
        mode_switch.pack(side="right", padx=(dp(8), 0))
        self.global_flow_mode_button = ThemeButton(
            mode_switch,
            self,
            "▦ 月度排期",
            lambda: self.set_global_display_mode("flow"),
            width=82,
            height=23,
            font_size=8,
            surface_background=theme.panel_secondary,
            outlined=True,
        )
        self.global_flow_mode_button.pack(side="left")
        self.global_timeline_mode_button = ThemeButton(
            mode_switch,
            self,
            "▤ 时间轴",
            lambda: self.set_global_display_mode("timeline"),
            width=76,
            height=23,
            font_size=8,
            surface_background=theme.panel_secondary,
            outlined=True,
        )
        self.global_timeline_mode_button.pack(side="left")
        Tooltip(day_detail_button, "查看当前选中日期的完整事项列表")
        Tooltip(self.global_flow_mode_button, "按周查看本月每天的安排")
        Tooltip(self.global_timeline_mode_button, "按事项查看持续时间与 DDL")

        self.global_workspace = tk.Frame(self.shell, bg=theme.panel_background, padx=dp(10), pady=dp(9))
        self.global_workspace.pack(fill="both", expand=True)
        self.global_workspace.grid_rowconfigure(0, weight=1)
        self.global_workspace.grid_columnconfigure(0, weight=0)
        self.global_workspace.grid_columnconfigure(1, weight=1)
        self.global_workspace.bind("<Configure>", self._on_global_workspace_configure)

        self.global_category_sidebar = tk.Frame(
            self.global_workspace,
            bg=theme.panel_secondary,
            highlightthickness=dp(1),
            highlightbackground=theme.divider,
        )
        self.global_category_sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, dp(8)))
        self.global_category_sidebar.pack_propagate(False)
        self._refresh_global_category_sidebar()

        self.global_timeline_shell = tk.Frame(
            self.global_workspace,
            bg=theme.schedule_background,
            highlightthickness=dp(1),
            highlightbackground=theme.divider,
        )
        self.global_timeline_shell.grid(row=0, column=1, sticky="nsew")
        self.global_timeline_shell.grid_rowconfigure(1, weight=1)
        self.global_timeline_shell.grid_columnconfigure(1, weight=1)
        label_width = dp(GLOBAL_TITLE_WIDTH)
        header_height = dp(GLOBAL_HEADER_HEIGHT)
        self.global_corner_canvas = tk.Canvas(self.global_timeline_shell, width=label_width, height=header_height, bg=theme.panel_secondary, bd=0, highlightthickness=0)
        self.global_date_canvas = tk.Canvas(self.global_timeline_shell, height=header_height, bg=theme.schedule_background, bd=0, highlightthickness=0)
        self.global_label_canvas = tk.Canvas(self.global_timeline_shell, width=label_width, bg=theme.panel_background, bd=0, highlightthickness=0)
        self.global_timeline_canvas = tk.Canvas(self.global_timeline_shell, bg=theme.schedule_background, bd=0, highlightthickness=0)
        self.global_canvas = self.global_timeline_canvas
        self.global_vscroll = ttk.Scrollbar(
            self.global_timeline_shell,
            orient="vertical",
            command=self._global_yview,
            style="Global.Vertical.TScrollbar",
        )
        self.global_hscroll = ttk.Scrollbar(
            self.global_timeline_shell,
            orient="horizontal",
            command=self._global_xview,
            style="Global.Horizontal.TScrollbar",
        )
        self.global_timeline_canvas.configure(xscrollcommand=self._sync_global_xscroll, yscrollcommand=self._sync_global_yscroll)
        self.global_corner_canvas.grid(row=0, column=0, sticky="nsew")
        self.global_date_canvas.grid(row=0, column=1, sticky="nsew")
        self.global_label_canvas.grid(row=1, column=0, sticky="nsew")
        self.global_timeline_canvas.grid(row=1, column=1, sticky="nsew")
        self.global_vscroll.grid(row=1, column=2, sticky="ns")
        self.global_hscroll.grid(row=2, column=1, sticky="ew")
        self.global_timeline_canvas.bind("<Configure>", self._schedule_global_render)
        for canvas in (self.global_date_canvas, self.global_label_canvas, self.global_timeline_canvas):
            canvas.bind("<MouseWheel>", self._global_mousewheel)
            canvas.bind("<Shift-MouseWheel>", self._global_shift_mousewheel)
        self.global_date_canvas.bind("<Button-1>", self._select_global_day)
        self.global_date_canvas.bind("<Double-Button-1>", self._create_global_day)
        self.global_date_canvas.bind("<Button-3>", self._show_global_day_menu)
        self.global_timeline_canvas.bind("<Button-1>", self._select_global_day)
        self.global_timeline_canvas.bind("<Double-Button-1>", self._create_global_day)
        self.global_timeline_canvas.bind("<Button-3>", self._show_global_day_menu)

        self.global_flow_shell = tk.Frame(
            self.global_workspace,
            bg=theme.schedule_background,
            highlightthickness=dp(1),
            highlightbackground=theme.divider,
        )
        self.global_flow_shell.grid(row=0, column=1, sticky="nsew")
        self.global_flow_shell.grid_rowconfigure(1, weight=1)
        self.global_flow_shell.grid_columnconfigure(0, weight=1)
        self.global_flow_header_canvas = tk.Canvas(
            self.global_flow_shell,
            height=dp(34),
            bg=theme.panel_secondary,
            bd=0,
            highlightthickness=0,
        )
        self.global_flow_canvas = tk.Canvas(
            self.global_flow_shell,
            bg=theme.schedule_background,
            bd=0,
            highlightthickness=0,
        )
        self.global_flow_vscroll = ttk.Scrollbar(
            self.global_flow_shell,
            orient="vertical",
            command=self.global_flow_canvas.yview,
            style="Global.Vertical.TScrollbar",
        )
        self.global_flow_canvas.configure(yscrollcommand=self.global_flow_vscroll.set)
        self.global_flow_header_canvas.grid(row=0, column=0, sticky="ew")
        self.global_flow_canvas.grid(row=1, column=0, sticky="nsew")
        self.global_flow_vscroll.grid(row=1, column=1, sticky="ns")
        self.global_flow_canvas.bind("<Configure>", self._schedule_global_render)
        self.global_flow_canvas.bind("<MouseWheel>", self._global_flow_mousewheel)
        self.global_flow_header_canvas.bind("<MouseWheel>", self._global_flow_mousewheel)
        self.global_flow_canvas.bind("<ButtonPress-1>", self._start_flow_drag)
        self.global_flow_canvas.bind("<B1-Motion>", self._update_flow_drag)
        self.global_flow_canvas.bind("<ButtonRelease-1>", self._finish_flow_drag)
        self.global_flow_canvas.bind("<Double-ButtonRelease-1>", self._create_flow_day)
        self.global_flow_canvas.bind("<Button-3>", self._show_flow_day_menu)
        self._update_global_display_mode_widgets()

        self.global_detail_frame = tk.Frame(
            self.global_workspace,
            width=dp(GLOBAL_TIMELINE_LAYOUT.detail_width),
            bg=theme.panel_secondary,
            highlightthickness=dp(1),
            highlightbackground=theme.divider,
            padx=dp(14),
            pady=dp(14),
        )
        self.global_detail_frame.grid(row=0, column=2, sticky="nsew", padx=(dp(10), 0))
        self.global_detail_frame.grid_propagate(False)
        detail_heading = tk.Frame(self.global_detail_frame, bg=theme.panel_secondary)
        detail_heading.pack(fill="x")
        tk.Label(detail_heading, text="事项详情", bg=theme.panel_secondary, fg=theme.text_secondary, font=(FONT, 8, "bold"), anchor="w").pack(side="left")
        self.global_detail_state_label = tk.Label(
            detail_heading,
            text="未选择",
            bg=theme.control_background,
            fg=theme.text_muted,
            font=(FONT, 7, "bold"),
            padx=dp(6),
            pady=dp(2),
        )
        self.global_detail_state_label.pack(side="right")
        self.global_detail_title = tk.Label(
            self.global_detail_frame,
            text="选择一个事项查看详情",
            bg=theme.panel_secondary,
            fg=theme.text_primary,
            font=(FONT, 12, "bold"),
            anchor="nw",
            justify="left",
            wraplength=dp(220),
        )
        self.global_detail_title.pack(fill="x", pady=(dp(12), dp(8)))
        detail_identity = tk.Frame(self.global_detail_frame, bg=theme.panel_secondary)
        detail_identity.pack(fill="x", pady=(0, dp(9)))
        self.global_detail_category_dot = LogicalCanvas(
            detail_identity,
            dpi=self.dpi,
            width=18,
            height=18,
            bg=theme.panel_secondary,
            bd=0,
            highlightthickness=0,
        )
        self.global_detail_category_dot.pack(side="left", padx=(0, dp(4)))
        self.global_detail_category_label = tk.Label(
            detail_identity,
            text="无分类",
            bg=theme.panel_secondary,
            fg=theme.text_secondary,
            font=(FONT, 8, "bold"),
            anchor="w",
        )
        self.global_detail_category_label.pack(side="left", fill="x", expand=True)
        self.global_detail_color_label = tk.Label(
            detail_identity,
            text="",
            bg=theme.panel_secondary,
            fg=theme.text_muted,
            font=(FONT, 7),
            anchor="e",
        )
        self.global_detail_color_label.pack(side="right")
        self.global_detail_meta = tk.Label(self.global_detail_frame, text="", bg=theme.panel_secondary, fg=theme.text_secondary, font=(FONT, 8), anchor="nw", justify="left", wraplength=dp(220))
        self.global_detail_meta.pack(fill="x")
        self.global_detail_notes = tk.Label(self.global_detail_frame, text="", bg=theme.panel_secondary, fg=theme.text_muted, font=(FONT, 8), anchor="nw", justify="left", wraplength=dp(220))
        self.global_detail_notes.pack(fill="both", expand=True, pady=(dp(12), dp(8)))
        detail_actions = tk.Frame(self.global_detail_frame, bg=theme.panel_secondary)
        detail_actions.pack(side="bottom", fill="x")
        self.global_detail_edit_button = ThemeButton(detail_actions, self, "编辑", self._edit_selected_timeline, width=58, height=29, font_size=8, surface_background=theme.panel_secondary, accented=True)
        self.global_detail_edit_button.pack(side="left")
        self.global_detail_toggle_button = ThemeButton(detail_actions, self, "完成", self._toggle_selected_timeline, width=64, height=29, font_size=8, surface_background=theme.panel_secondary, outlined=True)
        self.global_detail_toggle_button.pack(side="left", padx=(dp(5), 0))
        self.global_detail_delete_button = ThemeButton(detail_actions, self, "删除", self._delete_selected_timeline, width=54, height=29, font_size=8, foreground=theme.danger, surface_background=theme.panel_secondary, outlined=True)
        self.global_detail_delete_button.pack(side="right")

        self.global_status_label = tk.Label(
            self.shell,
            text="",
            bg=quick_surface if theme.style == "frutiger" else theme.panel_background,
            fg=theme.text_secondary,
            font=(FONT, 8),
            anchor="w",
            padx=dp(14),
            pady=dp(5),
        )
        self.global_status_label.pack(fill="x")
        self._global_tooltip = CanvasTooltip(self)
        self._set_quick_placeholder()
        self._update_mode_badge()

    def _draw_header(self, _event=None) -> None:
        width = max(1, self.header.logical_width())
        height = max(1, self.header.logical_height())
        self.header.delete("header_art")
        if self.theme.style == "frutiger":
            vertical_multi_gradient(
                self.header,
                0,
                0,
                width,
                height,
                (
                    (0.0, self.theme.header_highlight),
                    (0.10, self.theme.header_gradient_start),
                    (0.28, blend(self.theme.header_gradient_start, self.theme.header_gradient_mid, 0.35)),
                    (0.56, self.theme.header_gradient_mid),
                    (0.82, blend(self.theme.header_gradient_mid, self.theme.header_gradient_end, 0.48)),
                    (1.0, self.theme.header_gradient_end),
                ),
                tags="header_art",
            )
            rounded_rectangle(
                self.header,
                5,
                2,
                width - 5,
                round(height * 0.36),
                11,
                fill=blend(self.theme.header_gradient_start, self.theme.header_highlight, 0.58),
                outline="",
                tags="header_art",
            )
            self.header.create_line(
                8,
                3,
                width - 9,
                3,
                fill=self.theme.header_highlight,
                tags="header_art",
            )
            draw_bubble_motif(
                self.header,
                ((133, 10, 6), (146, 26, 4), (126, 31, 2.5)),
                outline=blend(self.theme.header_gradient_mid, self.theme.header_highlight, 0.66),
                highlight=self.theme.header_highlight,
                accent=blend(self.theme.environment_accent, self.theme.header_highlight, 0.34),
                tags="header_art",
            )
            reflection_y = round(height * 0.39)
            self.header.create_line(
                4,
                reflection_y,
                width - 5,
                reflection_y,
                fill=blend(self.theme.header_highlight, self.theme.header_gradient_mid, 0.55),
                tags="header_art",
            )
            self.header.create_line(
                11,
                height - 3,
                91,
                height - 3,
                fill=blend(self.theme.header_gradient_end, self.theme.environment_accent, 0.45),
                width=1,
                tags="header_art",
            )
            self.header.create_line(
                0,
                height - 2,
                width,
                height - 2,
                fill=self.theme.header_shadow,
                tags="header_art",
            )
        elif self.theme.style == "aero":
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

    def _draw_frutiger_empty_state(self, canvas: LogicalCanvas, title: str) -> None:
        canvas.delete("all")
        width = max(1, canvas.logical_width())
        height = max(1, canvas.logical_height())
        draw_ecology_horizon(
            canvas,
            width,
            height,
            background=self.theme.schedule_background,
            haze=self.theme.environment_haze,
            horizon=self.theme.environment_horizon,
            highlight=self.theme.environment_highlight,
            accent=self.theme.environment_accent,
            tags="environment_art",
        )
        canvas.create_text(
            width // 2,
            34,
            text=title,
            fill=self.theme.text_secondary,
            font=(FONT, 9, "bold"),
            tags="empty_text",
        )
        canvas.create_text(
            width // 2,
            55,
            text="双击日期查看详情 · 习惯清单仅在工作日出现",
            fill=self.theme.text_muted,
            font=(FONT, 8),
            tags="empty_text",
        )

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
            foreground=theme.text_secondary if theme.style != "aero" else theme.control_text,
            surface_background=theme.schedule_background,
            outlined=True,
        )
        self.quick_options_button.pack(side="right", padx=(dp(6), 0))
        self._refresh_quick_options_button()

        self.agenda_bar.pack(fill="x")

        footer_height = 21
        footer_background = (
            blend(theme.schedule_background, theme.environment_haze, 0.28)
            if theme.style == "frutiger"
            else theme.schedule_background
        )
        self.footer_frame = tk.Frame(
            self.schedule_section,
            bg=footer_background,
            padx=dp(13),
            height=dp(footer_height),
        )
        self.footer_frame.pack(side="bottom", fill="x")
        self.footer_frame.pack_propagate(False)
        footer_parent = self.footer_frame
        self.upcoming_label = tk.Label(footer_parent, text="", bg=footer_background, fg=theme.text_muted, font=(FONT, 8), cursor="hand2")
        self.upcoming_label.pack(side="left")
        self.upcoming_label.bind("<Button-1>", lambda _event: UpcomingDialog(self))
        Tooltip(self.upcoming_label, "查看未来 7 天和已逾期日程")
        self.ddl_list_label = tk.Label(
            footer_parent,
            text=f"{DDL_LIST_ENTRY_LABEL} ›",
            bg=footer_background,
            fg=theme.text_secondary,
            font=(FONT, 8),
            cursor="hand2",
            padx=dp(3),
        )
        self.ddl_list_label.pack(side="left", fill="y", padx=(dp(6), 0))
        self.ddl_list_label.bind("<Enter>", lambda _event: self._set_ddl_list_entry_state("hover"))
        self.ddl_list_label.bind("<Leave>", lambda _event: self._set_ddl_list_entry_state("normal"))
        self.ddl_list_label.bind("<ButtonPress-1>", lambda _event: self._set_ddl_list_entry_state("pressed"))
        self.ddl_list_label.bind("<ButtonRelease-1>", self._activate_ddl_list_entry)
        Tooltip(self.ddl_list_label, "查看全部未完成和已完成 DDL")
        tk.Label(footer_parent, text="双击日期查看详情", bg=footer_background, fg=theme.text_muted, font=(FONT, 8)).pack(side="right")

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
        saved = WindowGeometry.from_mapping(self.store.settings.get("compact_geometry"))
        if saved is not None:
            height = saved.height
            x, y = saved.x, saved.y
        else:
            saved_x = self.store.settings.get("x")
            saved_y = self.store.settings.get("y")
            try:
                x = int(saved_x) if saved_x is not None else area.right - window_width - self.dpi.px(26)
                y = int(saved_y) if saved_y is not None else area.top + self.dpi.px(44)
            except (TypeError, ValueError):
                x, y = area.right - window_width - self.dpi.px(26), area.top + self.dpi.px(44)
        window_height = self.dpi.px(height)
        x, y = clamp_to_work_area(x, y, window_width, window_height)
        self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))

    def get_view_mode(self) -> str:
        return self.view_mode

    def _capture_window_geometry(self) -> WindowGeometry:
        self.update_idletasks()
        return WindowGeometry(
            self.dpi.logical(self.winfo_width()),
            self.dpi.logical(self.winfo_height()),
            self.winfo_x(),
            self.winfo_y(),
        )

    def _apply_saved_geometry(self, geometry: WindowGeometry) -> WindowGeometry:
        device_width = self.dpi.px(geometry.width)
        device_height = self.dpi.px(geometry.height)
        target_area = work_area_for_rect(geometry.x, geometry.y, device_width, device_height)
        fitted = fit_geometry_to_work_area(geometry, target_area, self.dpi.dpi)
        self.geometry(scaled_geometry(fitted.width, fitted.height, fitted.x, fitted.y, self.dpi.dpi))
        return fitted

    def enter_global_view(self) -> None:
        if self.view_mode == "global":
            return
        compact_geometry = self._capture_window_geometry()
        self.store.settings["compact_geometry"] = compact_geometry.as_dict()
        self.store.settings["x"] = compact_geometry.x
        self.store.settings["y"] = compact_geometry.y
        self.store.settings["view_mode"] = "global"
        self.view_mode = "global"
        self.desktop_session_active = False
        self.timeline_selection.clear()
        self.withdraw()
        self.overrideredirect(False)
        self.resizable(True, True)
        self.minsize(self.dpi.px(GLOBAL_MIN_WIDTH), self.dpi.px(GLOBAL_MIN_HEIGHT))
        self.title(f"{APP_NAME} · 全局视图")
        self._rebuild_main_ui("", True)
        saved = WindowGeometry.from_mapping(
            self.store.settings.get("global_geometry"),
            minimum_width=GLOBAL_MIN_WIDTH,
            minimum_height=GLOBAL_MIN_HEIGHT,
        )
        geometry = saved or initial_global_geometry(self.dpi.work_area(), self.dpi.dpi)
        self._global_normal_geometry = self._apply_saved_geometry(geometry)
        self.deiconify()
        self.update_idletasks()
        make_app_window(self)
        self.render()
        self.store.save()
        self.after(80, self.apply_window_mode)

    def return_to_compact_view(self) -> None:
        if self.view_mode == "compact":
            return
        if self.state() == "normal":
            self._global_normal_geometry = self._capture_window_geometry()
        if self._global_normal_geometry is not None:
            self.store.settings["global_geometry"] = self._global_normal_geometry.as_dict()
        compact = WindowGeometry.from_mapping(self.store.settings.get("compact_geometry"))
        self.withdraw()
        try:
            self.state("normal")
        except tk.TclError:
            pass
        self.overrideredirect(True)
        self.resizable(False, False)
        self.minsize(1, 1)
        self.title(APP_NAME)
        self.view_mode = "compact"
        self._activate_compact_return_session()
        self.store.settings["view_mode"] = "compact"
        self._rebuild_main_ui("", True)
        if compact is None:
            self._set_initial_geometry()
        else:
            compact = WindowGeometry(WINDOW_WIDTH, compact.height, compact.x, compact.y)
            self._apply_saved_geometry(compact)
        self.deiconify()
        self.update_idletasks()
        make_tool_window(self)
        self.store.save()
        self.after(80, self.apply_window_mode)

    def _activate_compact_return_session(self) -> None:
        """Keep a restored desktop-mode gadget interactable without making it permanently topmost."""
        self.desktop_session_active = self.window_mode == "desktop"

    def toggle_global_view(self) -> None:
        if self.view_mode == "global":
            self.return_to_compact_view()
        else:
            self.enter_global_view()

    def _schedule_view_geometry_save(self, event: tk.Event) -> None:
        if event.widget is not self or self.view_mode != "global" or self._dpi_rebuilding:
            return
        if self._geometry_save_job:
            try:
                self.after_cancel(self._geometry_save_job)
            except tk.TclError:
                pass
        self._geometry_save_job = self.after(180, self._save_global_geometry)

    def _save_global_geometry(self) -> None:
        self._geometry_save_job = None
        if self.view_mode != "global" or not self.winfo_exists() or self.state() != "normal":
            return
        self._global_normal_geometry = self._capture_window_geometry()
        self.store.settings["global_geometry"] = self._global_normal_geometry.as_dict()
        self.store.save()

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
        if self.view_mode != "compact":
            return
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
        self._present_initial_foreground()

    def _present_initial_foreground(self) -> None:
        """Give a normal launch one foreground pulse without changing preferences."""
        if self._startup_foreground_done or not self.winfo_exists():
            return
        try:
            active_grab = self.grab_current()
        except (AttributeError, tk.TclError):
            active_grab = None
        if active_grab is not None and active_grab is not self:
            # A child modal means the user has already reached the app.  Do not
            # let the delayed startup pulse steal its grab/focus.
            self._startup_foreground_done = True
            return
        self._startup_foreground_done = True
        self._startup_foreground_active = True
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self.focus_force()
        bring_to_front(self)
        self._startup_foreground_restore_job = self.after(360, self._restore_after_initial_foreground)

    def _restore_after_initial_foreground(self) -> None:
        self._startup_foreground_restore_job = None
        self._startup_foreground_active = False
        if not self.winfo_exists():
            return
        try:
            active_grab = self.grab_current()
        except (AttributeError, tk.TclError):
            active_grab = None
        if active_grab is not None and active_grab is not self:
            # The modal owns the foreground now.  Its close path restores the
            # saved desktop/pinned mode without competing for Z-order here.
            self.attributes("-topmost", False)
            self._update_mode_badge()
            return
        if self.window_mode == "pinned":
            self.attributes("-topmost", True)
            self.lift()
        else:
            self.attributes("-topmost", False)
            self.desktop_session_active = True
            raise_for_interaction(self)
        self._update_mode_badge()

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
        if self._global_tooltip:
            self._global_tooltip.hide()
            self._global_tooltip = None
        if self._global_render_job:
            try:
                self.after_cancel(self._global_render_job)
            except tk.TclError:
                pass
            self._global_render_job = None
        self.window_frame.destroy()
        self._configure_style()
        self._build_ui()
        self.render()
        if quick_value and not quick_was_placeholder:
            self.quick_var.set(quick_value)
            self.quick_placeholder_active = False
            self.quick_entry.configure(fg=self.theme.text_primary)
        if self.view_mode == "compact":
            self.after_idle(self._draw_header)

    def _apply_dpi_change(self, new_dpi: int) -> None:
        quick_value = self.quick_var.get() if hasattr(self, "quick_var") else ""
        quick_was_placeholder = self.quick_placeholder_active
        x, y = self.winfo_x(), self.winfo_y()
        old_dpi = self.dpi.dpi
        global_was_maximized = self.view_mode == "global" and self.state() == "zoomed"
        global_geometry = (
            self._global_normal_geometry
            if global_was_maximized and self._global_normal_geometry is not None
            else WindowGeometry(
                unscale_px(self.winfo_width(), old_dpi),
                unscale_px(self.winfo_height(), old_dpi),
                x,
                y,
            )
            if self.view_mode == "global"
            else None
        )
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
            if self.view_mode == "global":
                self.minsize(self.dpi.px(GLOBAL_MIN_WIDTH), self.dpi.px(GLOBAL_MIN_HEIGHT))
                if global_was_maximized:
                    self.state("normal")
                self._global_normal_geometry = self._apply_saved_geometry(global_geometry or initial_global_geometry(self.dpi.work_area(), new_dpi))
                if global_was_maximized:
                    self.state("zoomed")
                make_app_window(self)
            else:
                pinned, regular = self.store.grouped_ddl_events()
                height = self._desired_window_height(len(pinned), len(regular))
                x, y = clamp_to_work_area(x, y, self.dpi.px(WINDOW_WIDTH), self.dpi.px(height))
                self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))
                make_tool_window(self)
        finally:
            self._dpi_rebuilding = False
        self.after(80, self.apply_window_mode)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-n>", lambda _event: self._open_primary_action())
        self.bind("<Control-t>", lambda _event: self.go_today())
        self.bind("<Control-g>", lambda _event: self.toggle_global_view())
        self.bind("<Home>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-t>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-n>", lambda event: self._keyboard_command(event, self._open_primary_action))
        self.bind("<Return>", lambda event: self._keyboard_command(event, self._open_primary_action))
        self.bind("<Prior>", lambda _event: self.change_month(-1))
        self.bind("<Next>", lambda _event: self.change_month(1))
        self.bind("<Left>", lambda event: self._move_selection(event, -1))
        self.bind("<Right>", lambda event: self._move_selection(event, 1))
        self.bind("<Up>", lambda event: self._move_selection(event, -7))
        self.bind("<Down>", lambda event: self._move_selection(event, 7))
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<ButtonPress>", self._activate_desktop_session, add="+")
        self.bind("<Escape>", self._handle_escape)

    def _open_primary_action(self) -> None:
        if primary_action_for_view(self.view_mode) == "create":
            self.open_new_event()
        else:
            self.open_day_detail()

    def _handle_escape(self, _event=None) -> str:
        if self.view_mode == "global" and self.global_display_mode == "flow" and self._global_flow_drag_start:
            self._cancel_flow_drag()
        else:
            self._end_desktop_session()
        return "break"

    def render(self) -> None:
        if self.view_mode == "global":
            self._render_global_timeline()
            if self.day_detail_window and self.day_detail_window.winfo_exists():
                self.day_detail_window.refresh()
            if self.ddl_list_window and self.ddl_list_window.winfo_exists():
                self.ddl_list_window.refresh()
            return
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
            colors = [
                self.store.effective_event_color(event) if not event.done else self.theme.event_done
                for event in day_events
            ]
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
        if self.ddl_list_window and self.ddl_list_window.winfo_exists():
            self.ddl_list_window.refresh()

    def _schedule_global_render(self, _event=None) -> None:
        if self.view_mode != "global" or self._global_render_job:
            return
        self._global_render_job = self.after_idle(self._finish_global_render)

    def _finish_global_render(self) -> None:
        self._global_render_job = None
        if self.view_mode == "global" and self.winfo_exists():
            self._draw_active_global_view()

    def _render_global_timeline(self) -> None:
        if not hasattr(self, "global_timeline_canvas") or not self.global_timeline_canvas.winfo_exists():
            return
        self._prune_global_category_filter()
        model = build_month_timeline(
            self.store,
            self.shown_year,
            self.shown_month,
            category_ids=self._global_category_filter_ids,
            include_uncategorized=self._global_include_uncategorized,
        )
        self.timeline_model = model
        self.calendar_flow_layout = build_calendar_flow_layout(model)
        self.timeline_selection.get(model)
        self.global_month_label.configure(text=f"{model.year}年 {model.month}月")
        unfinished = sum(not item.completed for item in model.items)
        ddl_count = sum(bool(item.ddl_date) and not item.completed for item in model.items)
        self.global_summary_label.configure(text=f"本月 {len(model.items)} 项工作 · {unfinished} 项未完成 · {ddl_count} 个 DDL")
        self._refresh_global_category_filter_button()
        self._refresh_global_category_sidebar()
        self._draw_active_global_view()

    def set_global_display_mode(self, mode: str) -> None:
        """Switch Global renderers in place while keeping month and selection stable."""
        normalized = normalize_global_display_mode(mode)
        if normalized == self.global_display_mode:
            return
        self._cancel_flow_drag(redraw=False)
        self.global_display_mode = normalized
        self.store.settings["global_display_mode"] = normalized
        self.store.save()
        self._update_global_display_mode_widgets()
        self._draw_active_global_view()

    def _update_global_display_mode_widgets(self) -> None:
        if not hasattr(self, "global_timeline_shell"):
            return
        is_flow = self.global_display_mode == "flow"
        if is_flow:
            self.global_timeline_shell.grid_remove()
            self.global_flow_shell.grid()
        else:
            self._global_flow_hover_item_id = None
            self.global_flow_shell.grid_remove()
            self.global_timeline_shell.grid()
        if hasattr(self, "global_view_title_label"):
            self.global_view_title_label.configure(text="月度排期" if is_flow else "全局时间轴")
        for button, active in (
            (getattr(self, "global_flow_mode_button", None), is_flow),
            (getattr(self, "global_timeline_mode_button", None), not is_flow),
        ):
            if button is not None:
                button.accented = active
                button.outlined = not active
                button.foreground = None
                button.draw()

    def _draw_active_global_view(self) -> None:
        self._update_global_display_mode_widgets()
        if self.global_display_mode == "flow":
            self._draw_calendar_flow()
        else:
            self._draw_global_timeline()

    def _on_global_workspace_configure(self, event: tk.Event) -> None:
        logical_width = self.dpi.logical(event.width)
        show_detail = GLOBAL_TIMELINE_LAYOUT.show_detail_panel(logical_width)
        if show_detail != self._global_detail_visible:
            self._global_detail_visible = show_detail
            if show_detail:
                self.global_detail_frame.grid()
            else:
                self.global_detail_frame.grid_remove()
        if show_detail:
            detail_width = GLOBAL_TIMELINE_LAYOUT.detail_panel_width(logical_width)
            self.global_detail_frame.configure(width=self.dpi.px(detail_width))
            wraplength = self.dpi.px(max(160, detail_width - 36))
            self.global_detail_title.configure(wraplength=wraplength)
            self.global_detail_meta.configure(wraplength=wraplength)
            self.global_detail_notes.configure(wraplength=wraplength)
        self._schedule_global_render()

    def _draw_global_timeline(self) -> None:
        model = self.timeline_model
        if model is None or not hasattr(self, "global_timeline_canvas") or not self.global_timeline_canvas.winfo_exists():
            return
        canvas = self.global_timeline_canvas
        date_canvas = self.global_date_canvas
        label_canvas = self.global_label_canvas
        corner = self.global_corner_canvas
        x_position = canvas.xview()[0] if canvas.bbox("all") else 0.0
        y_position = canvas.yview()[0] if canvas.bbox("all") else 0.0
        for target in (canvas, date_canvas, label_canvas, corner):
            target.delete("all")
        dp = self.dpi.px
        title_width = dp(GLOBAL_TITLE_WIDTH)
        header_height = dp(GLOBAL_HEADER_HEIGHT)
        row_height = dp(GLOBAL_ROW_HEIGHT)
        viewport_width = max(1, canvas.winfo_width())
        day_width = max(dp(GLOBAL_DAY_MIN_WIDTH), viewport_width // max(1, len(model.days)))
        content_width = day_width * len(model.days)
        content_height = max(canvas.winfo_height(), max(1, len(model.items)) * row_height)
        self._global_day_width = day_width
        self._global_content_width = content_width
        self._global_content_height = content_height
        theme = self.theme
        selected = self.timeline_selection.get(model)

        for index, day_meta in enumerate(model.days):
            x1 = index * day_width
            x2 = x1 + day_width
            if day_meta.is_user_leave or day_meta.is_user_holiday:
                background = blend(theme.schedule_background, theme.date_leave_indicator, 0.12)
            elif day_meta.is_legal_holiday:
                background = blend(theme.schedule_background, theme.ddl_due_background, 0.44)
            elif day_meta.is_weekend and not day_meta.is_adjusted_workday:
                background = blend(theme.schedule_background, theme.panel_secondary, 0.64)
            else:
                background = theme.schedule_background
            header_background = blend(background, theme.panel_secondary, 0.24)
            date_tag = f"timeline-day:{day_meta.date.isoformat()}"
            date_canvas.create_rectangle(x1, 0, x2, header_height, fill=header_background, outline="", tags=(date_tag,))
            canvas.create_rectangle(x1, 0, x2, content_height, fill=background, outline="")
            canvas.create_line(x1, 0, x1, content_height, fill=theme.divider)
            date_canvas.create_line(x1, 0, x1, header_height, fill=theme.divider)
            if day_meta.is_today:
                today_fill = blend(theme.schedule_background, theme.date_today_background, 0.18)
                canvas.create_rectangle(x1, 0, x2, content_height, fill=today_fill, outline="")
                canvas.create_line(x1 + dp(1), 0, x1 + dp(1), content_height, fill=theme.date_today_border, width=dp(2))
                date_canvas.create_rectangle(
                    x1 + dp(1),
                    dp(1),
                    x2 - dp(1),
                    header_height - dp(1),
                    fill=blend(header_background, theme.date_today_background, 0.16),
                    outline=theme.date_today_border,
                    width=dp(2),
                    tags=(date_tag,),
                )
            if day_meta.date == self.selected:
                date_canvas.create_rectangle(x1 + dp(3), dp(3), x2 - dp(3), header_height - dp(3), outline=theme.date_selected_border, width=dp(1), tags=(date_tag,))
            weekday = "一二三四五六日"[day_meta.weekday]
            date_canvas.create_text(
                (x1 + x2) // 2,
                dp(18),
                text=str(day_meta.date.day),
                fill=theme.date_weekend_text if day_meta.is_weekend else theme.text_primary,
                font=(FONT, 8, "bold" if day_meta.is_today else "normal"),
                tags=(date_tag,),
            )
            day_hint = day_meta.holiday_name if day_width >= dp(55) and day_meta.holiday_name else f"周{weekday}"
            date_canvas.create_text(
                (x1 + x2) // 2,
                dp(38),
                text=truncate(day_hint, max(2, day_width // max(1, dp(8)))),
                fill=theme.holiday_workday if day_meta.is_adjusted_workday else theme.text_muted,
                font=(FONT, 7),
                tags=(date_tag,),
            )
            tooltip = day_meta.holiday_name or ("请假" if day_meta.is_user_leave else "自定义假期" if day_meta.is_user_holiday else f"周{weekday}")
            date_canvas.tag_bind(date_tag, "<Enter>", lambda event, text=f"{day_meta.date:%Y-%m-%d} · {tooltip}": self._show_global_tooltip(text, event))
            date_canvas.tag_bind(date_tag, "<Leave>", self._hide_global_tooltip)

        corner.create_rectangle(0, 0, title_width, header_height, fill=theme.panel_secondary, outline="")
        corner.create_text(dp(12), dp(18), text="事项", fill=theme.text_primary, font=(FONT, 9, "bold"), anchor="w")
        corner.create_text(dp(12), dp(39), text="名称固定 · 时间轴可滚动", fill=theme.text_muted, font=(FONT, 7), anchor="w")
        corner.create_line(0, header_height - 1, title_width, header_height - 1, fill=theme.divider)
        date_canvas.create_line(0, header_height - 1, content_width, header_height - 1, fill=theme.divider)

        if not model.items:
            canvas.create_text(
                max(viewport_width // 2, content_width // 2),
                dp(54),
                text="这个月还没有事项 · 双击日期即可新建",
                fill=theme.text_muted,
                font=(FONT, 10),
            )
        for row_index, item in enumerate(model.items):
            y1 = row_index * row_height
            y2 = y1 + row_height
            row_tag = f"timeline-item:{item.id}"
            if selected and selected.id == item.id:
                canvas.create_rectangle(0, y1, content_width, y2, fill=theme.accent_soft, outline="", tags=(row_tag,))
                label_canvas.create_rectangle(0, y1, title_width, y2, fill=theme.accent_soft, outline="", tags=(row_tag,))
            else:
                label_canvas.create_rectangle(0, y1, title_width, y2, fill=theme.panel_background, outline="", tags=(row_tag,))
            canvas.create_line(0, y2, content_width, y2, fill=theme.divider)
            label_canvas.create_line(0, y2, title_width, y2, fill=theme.divider)
            label_canvas.create_rectangle(0, y1 + dp(6), dp(4), y2 - dp(6), fill=theme.event_done if item.completed else item.color, outline="", tags=(row_tag,))
            title_color = theme.text_done if item.completed else theme.text_primary
            label_canvas.create_text(
                dp(12),
                y1 + dp(13),
                text=truncate(item.title, 25),
                fill=title_color,
                font=(FONT, 8, "overstrike" if item.completed else "normal"),
                anchor="w",
                tags=(row_tag,),
            )
            type_text = timeline_type_label(item)
            if item.is_urgent:
                type_text = f"! {type_text}"
            if item.ddl_date:
                type_text += " · DDL"
            label_canvas.create_text(
                dp(12),
                y1 + dp(29),
                text=f"{type_text} · {item.effective_days_count}天",
                fill=theme.text_muted,
                font=(FONT, 7),
                anchor="w",
                tags=(row_tag,),
            )
            bar_fill = blend(item.color, theme.schedule_background, 0.68) if item.completed else item.color
            outline = (
                theme.accent
                if selected and selected.id == item.id
                else theme.schedule_card_border
            )
            visible_segments = []
            for segment in item.segments:
                start_index = (segment.start_date - model.period_start).days
                end_index = (segment.end_date - model.period_start).days
                segment_x1 = start_index * day_width + dp(3)
                segment_x2 = (end_index + 1) * day_width - dp(3)
                visible_segments.append((segment_x1, segment_x2))
                rounded_rectangle(
                    canvas,
                    segment_x1,
                    y1 + dp(8),
                    segment_x2,
                    y2 - dp(8),
                    dp(5),
                    fill=bar_fill,
                    outline=outline,
                    width=dp(2) if selected and selected.id == item.id else dp(1),
                    tags=(row_tag,),
                )
            if visible_segments:
                label_x1, label_x2 = max(visible_segments, key=lambda bounds: bounds[1] - bounds[0])
                label_text = timeline_bar_label(item.title, self.dpi.logical(label_x2 - label_x1))
                if label_text:
                    canvas.create_text(label_x1 + dp(9), (y1 + y2) // 2, text=label_text, fill=theme.text_on_accent, font=(FONT, 7), anchor="w", tags=(row_tag,))
            if item.continues_from_previous_period:
                x = dp(3)
                canvas.create_polygon(x, (y1 + y2) // 2, x + dp(7), y1 + dp(12), x + dp(7), y2 - dp(12), fill=outline, tags=(row_tag,))
            if item.continues_to_next_period:
                x = content_width - dp(3)
                canvas.create_polygon(x, (y1 + y2) // 2, x - dp(7), y1 + dp(12), x - dp(7), y2 - dp(12), fill=outline, tags=(row_tag,))
            if item.is_urgent and item.segments:
                first_index = (item.segments[0].start_date - model.period_start).days
                marker_x = first_index * day_width + dp(9)
                canvas.create_text(marker_x, y1 + dp(10), text="!", fill=theme.event_type_urgent, font=(FONT, 7, "bold"), tags=(row_tag,))
            if item.ddl_date and model.period_start <= item.ddl_date <= model.period_end:
                ddl_index = (item.ddl_date - model.period_start).days
                center_x = ddl_index * day_width + day_width // 2
                center_y = (y1 + y2) // 2
                radius = dp(5)
                canvas.create_polygon(
                    center_x,
                    center_y - radius,
                    center_x + radius,
                    center_y,
                    center_x,
                    center_y + radius,
                    center_x - radius,
                    center_y,
                    fill=theme.event_type_ddl,
                    outline=theme.ddl_indicator_highlight,
                    tags=(row_tag,),
                )
            for target in (canvas, label_canvas):
                target.tag_bind(row_tag, "<Button-1>", lambda _event, item_id=item.id: self.select_timeline_item(item_id))
                target.tag_bind(row_tag, "<Double-Button-1>", lambda _event, item_id=item.id: self.open_timeline_item(item_id))
                target.tag_bind(row_tag, "<Button-3>", lambda event, item_id=item.id: self._show_global_event_menu(item_id, event))
                target.tag_bind(row_tag, "<Enter>", lambda event, item_id=item.id: self._show_timeline_item_tooltip(item_id, event))
                target.tag_bind(row_tag, "<Leave>", self._hide_global_tooltip)

        date_canvas.configure(scrollregion=(0, 0, content_width, header_height))
        canvas.configure(scrollregion=(0, 0, content_width, content_height), xscrollincrement=dp(12), yscrollincrement=row_height)
        label_canvas.configure(scrollregion=(0, 0, title_width, content_height), yscrollincrement=row_height)
        canvas.xview_moveto(x_position)
        canvas.yview_moveto(y_position)
        date_canvas.xview_moveto(x_position)
        label_canvas.yview_moveto(y_position)
        self._update_global_detail(selected)
        if selected:
            self.global_status_label.configure(
                text=f"已选择：{selected.title} · {selected.start_date:%Y-%m-%d} → {selected.end_date:%Y-%m-%d}"
            )
        else:
            self.global_status_label.configure(text="滚轮纵向浏览 · Shift + 滚轮横向浏览 · 双击日期空白新建")

    def _draw_calendar_flow(self) -> None:
        model = self.timeline_model
        layout = self.calendar_flow_layout
        if (
            model is None
            or layout is None
            or not hasattr(self, "global_flow_canvas")
            or not self.global_flow_canvas.winfo_exists()
        ):
            return
        canvas = self.global_flow_canvas
        header = self.global_flow_header_canvas
        y_position = canvas.yview()[0] if canvas.bbox("all") else 0.0
        canvas.delete("all")
        header.delete("all")
        dp = self.dpi.px
        theme = self.theme
        viewport_width = max(dp(420), canvas.winfo_width())
        column_width = max(dp(58), viewport_width // 7)
        content_width = column_width * 7
        day_header_height = dp(30)
        card_height = dp(28)
        lane_gap = dp(3)
        week_height = day_header_height + layout.max_visible_lanes * (card_height + lane_gap) + dp(15)
        content_height = max(canvas.winfo_height(), len(layout.weeks) * week_height)
        self._global_flow_column_width = column_width
        self._global_flow_week_height = week_height
        selected = self.timeline_selection.get(model)
        self._global_flow_card_styles.clear()
        day_meta = {entry.date: entry for entry in model.days}
        ddl_dates = model.active_ddl_dates
        today = date.today()
        drag_bounds: Optional[tuple[date, date]] = None
        if self._global_flow_drag_start and self._global_flow_drag_end:
            drag_start, drag_end, _duration = normalize_flow_drag_range(
                self._global_flow_drag_start,
                self._global_flow_drag_end,
            )
            drag_bounds = (drag_start, drag_end)

        for column, weekday in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日")):
            x1 = column * column_width
            x2 = x1 + column_width
            header.create_rectangle(x1, 0, x2, dp(34), fill=theme.panel_secondary, outline=theme.divider)
            header.create_text(
                (x1 + x2) // 2,
                dp(17),
                text=weekday,
                fill=theme.date_weekend_text if column >= 5 else theme.text_secondary,
                font=(FONT, 8, "bold"),
            )

        for week in layout.weeks:
            row_y1 = week.index * week_height
            row_y2 = row_y1 + week_height
            for column, day in enumerate(week.dates):
                x1 = column * column_width
                x2 = x1 + column_width
                meta = day_meta.get(day)
                in_month = day.month == model.month
                background = theme.schedule_background
                if not in_month:
                    background = blend(theme.schedule_background, theme.panel_secondary, 0.66)
                elif meta and (meta.is_user_leave or meta.is_user_holiday):
                    status_color = theme.date_leave_indicator if meta.is_user_leave else theme.date_holiday_indicator
                    background = blend(theme.schedule_background, status_color, 0.10)
                elif meta and meta.is_legal_holiday:
                    background = blend(theme.schedule_background, theme.event_type_ddl_background, 0.34)
                elif column >= 5:
                    background = blend(theme.schedule_background, theme.panel_secondary, 0.40)
                in_drag_range = bool(drag_bounds and drag_bounds[0] <= day <= drag_bounds[1])
                if in_drag_range:
                    background = blend(background, theme.accent_soft, 0.58)
                if day == today:
                    background = blend(background, theme.date_today_background, 0.56)
                canvas.create_rectangle(
                    x1,
                    row_y1,
                    x2,
                    row_y2,
                    fill=background,
                    outline=theme.date_selected_border if in_drag_range else theme.divider,
                    width=dp(1),
                )
                if in_drag_range:
                    canvas.create_line(
                        x1 + dp(2),
                        row_y1 + dp(1),
                        x2 - dp(2),
                        row_y1 + dp(1),
                        fill=theme.accent,
                        width=dp(2),
                    )
                if day in ddl_dates:
                    rounded_rectangle(
                        canvas,
                        x1 + dp(3),
                        row_y1 + dp(3),
                        x2 - dp(3),
                        row_y2 - dp(3),
                        dp(6),
                        fill="",
                        outline=theme.ddl_indicator,
                        width=dp(1),
                    )
                    canvas.create_line(
                        x1 + dp(10),
                        row_y1 + dp(4),
                        x2 - dp(10),
                        row_y1 + dp(4),
                        fill=theme.ddl_indicator_highlight,
                        width=dp(1),
                    )
                if day == self.selected and not in_drag_range:
                    selected_inset = dp(7) if day in ddl_dates else dp(2)
                    rounded_rectangle(
                        canvas,
                        x1 + selected_inset,
                        row_y1 + selected_inset,
                        x2 - selected_inset,
                        row_y2 - selected_inset,
                        dp(5),
                        fill="",
                        outline=theme.date_selected_border,
                        width=dp(1),
                    )
                number_color = theme.date_other_month if not in_month else theme.date_weekend_text if column >= 5 else theme.date_text
                if day == today:
                    number_color = theme.date_today_border
                date_tag = f"flow-date:{day.isoformat()}"
                if day == today:
                    draw_calendar_date_state(
                        canvas,
                        x1 + dp(4),
                        row_y1 + dp(3),
                        x1 + dp(36),
                        row_y1 + dp(28),
                        fill=theme.date_today_background,
                        border=theme.date_today_border,
                        radius=dp(theme.metrics.date_radius),
                        top_highlight=(
                            blend(theme.date_today_background, theme.control_highlight, 0.34)
                            if theme.style in ("aero", "frutiger")
                            else None
                        ),
                        today_ring=theme.date_today_border,
                        tags=(date_tag,),
                    )
                canvas.create_text(
                    x1 + dp(8),
                    row_y1 + dp(8),
                    text=f"{day.month}/{day.day}" if not in_month else str(day.day),
                    fill=number_color,
                    font=(FONT, 8, "bold" if day in (today, self.selected) else "normal"),
                    anchor="nw",
                    tags=(date_tag,),
                )
                if day == today and column_width >= dp(82):
                    canvas.create_text(
                        x1 + dp(41),
                        row_y1 + dp(9),
                        text="今天",
                        fill=theme.date_today_border,
                        font=(FONT, 7, "bold"),
                        anchor="nw",
                        tags=(date_tag,),
                    )
                if meta and meta.holiday_name and column_width >= dp(92):
                    canvas.create_text(
                        x2 - dp(7),
                        row_y1 + dp(9),
                        text=truncate(meta.holiday_name, max(3, self.dpi.logical(column_width) // 13)),
                        fill=theme.holiday_workday if meta.is_adjusted_workday else theme.holiday_festival,
                        font=(FONT, 7),
                        anchor="ne",
                        tags=(date_tag,),
                    )
                if meta and (meta.is_user_leave or meta.is_user_holiday):
                    canvas.create_text(
                        x2 - dp(7),
                        row_y1 + dp(20) if meta.holiday_name and column_width >= dp(92) else row_y1 + dp(9),
                        text="请假" if meta.is_user_leave else "放假",
                        fill=theme.date_leave_indicator if meta.is_user_leave else theme.date_holiday_indicator,
                        font=(FONT, 7),
                        anchor="ne",
                        tags=(date_tag,),
                    )

            for block in week.blocks:
                if not block.visible:
                    continue
                item = block.item
                item_tag = f"timeline-item:{item.id}"
                x1 = block.start_column * column_width + dp(4)
                x2 = (block.end_column + 1) * column_width - dp(4)
                y1 = row_y1 + day_header_height + block.lane * (card_height + lane_gap)
                y2 = y1 + card_height
                stripe_color = theme.event_done if item.completed else item.color
                is_selected = bool(selected and selected.id == item.id)
                is_hovered = self._global_flow_hover_item_id == item.id
                if item.completed:
                    card_fill = blend(theme.schedule_card_background, theme.card_done_background, 0.62)
                    outline = theme.schedule_card_border
                elif item.native_ddl or block.ddl_date:
                    card_fill = blend(theme.schedule_card_background, theme.event_type_ddl_background, 0.20)
                    outline = theme.event_type_ddl_border
                elif item.is_urgent:
                    card_fill = blend(theme.schedule_card_background, theme.event_type_urgent_background, 0.20)
                    outline = theme.event_type_urgent_border
                else:
                    card_fill = blend(theme.schedule_card_background, item.color, 0.09)
                    outline = blend(theme.schedule_card_border, stripe_color, 0.34)
                if is_selected:
                    card_fill = blend(card_fill, theme.accent_soft, 0.48)
                    outline = theme.accent
                card_tag = f"flow-card:{item.id}"
                card_style_tag = f"flow-card-style:{item.id}:{week.index}:{block.start_column}:{block.lane}"
                card_width = dp(2) if is_selected else dp(1)
                self._global_flow_card_styles.setdefault(item.id, []).append(
                    (card_style_tag, card_fill, outline, card_width)
                )
                if is_hovered:
                    card_fill = blend(card_fill, theme.schedule_card_hover, 0.56)
                    if not is_selected:
                        outline = blend(outline, theme.accent_hover, 0.42)
                rounded_rectangle(
                    canvas,
                    x1,
                    y1,
                    x2,
                    y2,
                    dp(5),
                    fill=card_fill,
                    outline=outline,
                    width=card_width,
                    tags=(item_tag, card_tag, card_style_tag),
                )
                if theme.style in ("aero", "frutiger"):
                    canvas.create_line(
                        x1 + dp(7),
                        y1 + dp(2),
                        x2 - dp(7),
                        y1 + dp(2),
                        fill=blend(card_fill, theme.control_highlight, 0.58),
                        tags=(item_tag,),
                    )
                canvas.create_rectangle(
                    x1 + dp(2),
                    y1 + dp(5),
                    x1 + dp(5),
                    y2 - dp(5),
                    fill=stripe_color,
                    outline="",
                    tags=(item_tag,),
                )
                for boundary in range(block.start_column + 1, block.end_column + 1):
                    boundary_x = boundary * column_width
                    canvas.create_line(
                        boundary_x,
                        y1 + dp(4),
                        boundary_x,
                        y2 - dp(4),
                        fill=blend(theme.divider, stripe_color, 0.18),
                        tags=(item_tag,),
                    )
                logical_card_width = self.dpi.logical(x2 - x1)
                detail_level = flow_card_detail_level(
                    logical_card_width,
                    span_columns=block.end_column - block.start_column + 1,
                )
                left_inset = 19 if block.continues_before else 11
                right_reserve = 18 if block.continues_after or block.ddl_date else 8
                available_width = max(20, logical_card_width - left_inset - right_reserve)
                title_chars = max(2, available_width // 8)
                title_text = truncate(f"↳ {item.title}" if block.continues_before else item.title, title_chars)
                title_y = (y1 + y2) // 2 if detail_level == "compact" else y1 + dp(6)
                canvas.create_text(
                    x1 + dp(left_inset),
                    title_y,
                    text=title_text,
                    fill=theme.text_done if item.completed else theme.text_primary,
                    font=(FONT, 7, "overstrike" if item.completed else "bold"),
                    anchor="w" if detail_level == "compact" else "nw",
                    tags=(item_tag,),
                )
                type_text = timeline_type_label(item)
                if item.is_urgent:
                    type_text = f"! {type_text}"
                if block.ddl_date and not item.native_ddl:
                    type_text += " · DDL"
                if detail_level != "compact":
                    meta_text = type_text if detail_level == "medium" else f"{flow_date_range_text(item)} · {type_text}"
                    meta_chars = max(2, available_width // 7)
                    meta_color = (
                        theme.text_done
                        if item.completed
                        else theme.event_type_urgent
                        if item.is_urgent
                        else theme.event_type_ddl
                        if item.native_ddl or block.ddl_date
                        else theme.text_secondary
                    )
                    canvas.create_text(
                        x1 + dp(left_inset),
                        y2 - dp(4),
                        text=truncate(meta_text, meta_chars),
                        fill=meta_color,
                        font=(FONT, 6),
                        anchor="sw",
                        tags=(item_tag,),
                    )
                if block.continues_after:
                    canvas.create_text(x2 - dp(4), (y1 + y2) // 2, text="续›", fill=stripe_color, font=(FONT, 6, "bold"), anchor="e", tags=(item_tag,))
                if block.ddl_date:
                    ddl_column = (block.ddl_date - week.dates[0]).days
                    marker_x = ddl_column * column_width + column_width - dp(10)
                    marker_y = y1 + dp(8)
                    radius = dp(4)
                    canvas.create_polygon(
                        marker_x,
                        marker_y - radius,
                        marker_x + radius,
                        marker_y,
                        marker_x,
                        marker_y + radius,
                        marker_x - radius,
                        marker_y,
                        fill=theme.event_type_ddl,
                        outline=theme.ddl_indicator_highlight,
                        tags=(item_tag,),
                    )
                canvas.tag_bind(item_tag, "<Button-1>", lambda _event, item_id=item.id: self._select_flow_item(item_id))
                canvas.tag_bind(item_tag, "<Double-Button-1>", lambda _event, item_id=item.id: self._open_flow_item(item_id))
                canvas.tag_bind(item_tag, "<Button-3>", lambda event, item_id=item.id: self._show_flow_item_menu(item_id, event))
                canvas.tag_bind(item_tag, "<Enter>", lambda event, item_id=item.id: self._enter_flow_item(item_id, event))
                canvas.tag_bind(item_tag, "<Leave>", lambda event, item_id=item.id: self._leave_flow_item(item_id, event))

            for column, hidden_count in enumerate(week.hidden_counts):
                if hidden_count <= 0:
                    continue
                more_tag = f"flow-more:{week.dates[column].isoformat()}"
                x1 = column * column_width
                canvas.create_text(
                    x1 + dp(8),
                    row_y2 - dp(8),
                    text=f"+{hidden_count} 更多",
                    fill=theme.accent,
                    font=(FONT, 7, "bold"),
                    anchor="sw",
                    tags=(more_tag,),
                )
                canvas.tag_bind(more_tag, "<Button-1>", lambda _event, day=week.dates[column]: self._open_flow_day_detail(day))

        if not model.items:
            canvas.create_text(
                content_width // 2,
                dp(72),
                text="这个月还没有事项 · 双击日期空白即可新建",
                fill=theme.text_muted,
                font=(FONT, 10),
            )
        header.configure(scrollregion=(0, 0, content_width, dp(34)))
        canvas.configure(scrollregion=(0, 0, content_width, content_height), yscrollincrement=week_height)
        canvas.yview_moveto(y_position)
        self._update_global_detail(selected)
        if selected:
            self.global_status_label.configure(text=f"已选择：{selected.title} · {selected.start_date:%Y-%m-%d} → {selected.end_date:%Y-%m-%d}")
        elif self._global_flow_drag_start and self._global_flow_drag_end:
            range_start, range_end, _duration = normalize_flow_drag_range(
                self._global_flow_drag_start,
                self._global_flow_drag_end,
            )
            self.global_status_label.configure(
                text=f"已选择日期范围：{range_start:%Y-%m-%d} 至 {range_end:%Y-%m-%d} · 右键进行操作"
            )
        else:
            self.global_status_label.configure(text="一周一行 · 滚轮浏览 · 双击日期空白新建 · 双击事项编辑")

    def _global_flow_mousewheel(self, event: tk.Event) -> str:
        units = wheel_units(event.delta)
        if units:
            self.global_flow_canvas.yview_scroll(units, "units")
        return "break"

    def _enter_flow_item(self, item_id: str, event: tk.Event) -> None:
        if self._global_flow_hover_item_id != item_id:
            previous = self._global_flow_hover_item_id
            self._global_flow_hover_item_id = item_id
            if previous:
                self._restore_flow_card_style(previous)
            styles = self._global_flow_card_styles.get(item_id, ())
            selected = self.timeline_selection.get(self.timeline_model) if self.timeline_model else None
            for card_tag, fill, outline, width in styles:
                self.global_flow_canvas.itemconfigure(
                    card_tag,
                    fill=blend(fill, self.theme.schedule_card_hover, 0.56),
                    outline=outline if selected and selected.id == item_id else blend(outline, self.theme.accent_hover, 0.42),
                    width=width,
                )
        self._show_timeline_item_tooltip(item_id, event)

    def _leave_flow_item(self, item_id: str, _event: tk.Event) -> None:
        self._hide_global_tooltip()
        self.after_idle(lambda value=item_id: self._clear_flow_item_hover(value))

    def _clear_flow_item_hover(self, item_id: str) -> None:
        canvas = getattr(self, "global_flow_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        current = canvas.find_withtag("current")
        if current and f"timeline-item:{item_id}" in canvas.gettags(current[0]):
            return
        if self._global_flow_hover_item_id == item_id:
            self._global_flow_hover_item_id = None
            self._restore_flow_card_style(item_id)

    def _restore_flow_card_style(self, item_id: str) -> None:
        styles = self._global_flow_card_styles.get(item_id, ())
        canvas = getattr(self, "global_flow_canvas", None)
        if not styles or canvas is None or not canvas.winfo_exists():
            return
        for card_tag, fill, outline, width in styles:
            canvas.itemconfigure(card_tag, fill=fill, outline=outline, width=width)

    def _global_flow_day_from_event(self, event: tk.Event) -> Optional[date]:
        layout = self.calendar_flow_layout
        if layout is None or not self._global_flow_column_width or not self._global_flow_week_height:
            return None
        canvas_x = event.widget.canvasx(event.x)
        canvas_y = event.widget.canvasy(event.y)
        return flow_day_at(
            canvas_x,
            canvas_y,
            layout=layout,
            column_width=self._global_flow_column_width,
            week_height=self._global_flow_week_height,
        )

    def _start_flow_drag(self, event: tk.Event) -> Optional[str]:
        if self._canvas_has_timeline_item(event.widget):
            return None
        selected = self._global_flow_day_from_event(event)
        if selected is None:
            return None
        existing_range = self._flow_selected_range()
        preserve_range = bool(
            existing_range
            and existing_range[0] != existing_range[1]
            and existing_range[0] <= selected <= existing_range[1]
        )
        self._global_flow_drag_anchor = selected
        try:
            event.widget.grab_set()
        except tk.TclError:
            pass
        if not preserve_range:
            self._global_flow_drag_start = selected
            self._global_flow_drag_end = selected
        self._global_flow_drag_moved = False
        self.timeline_selection.clear()
        self.selected = selected
        self._set_quick_placeholder()
        self._draw_calendar_flow()
        return "break"

    def _update_flow_drag(self, event: tk.Event) -> Optional[str]:
        anchor = self._global_flow_drag_anchor
        if anchor is None:
            return None
        selected = self._global_flow_day_from_event(event)
        if selected is None:
            return "break"
        if selected != self._global_flow_drag_end or self._global_flow_drag_start != anchor:
            self._global_flow_drag_start = anchor
            self._global_flow_drag_end = selected
            self._global_flow_drag_moved = selected != anchor
            self._draw_calendar_flow()
        return "break"

    def _finish_flow_drag(self, event: tk.Event) -> Optional[str]:
        anchor = self._global_flow_drag_anchor
        if anchor is None:
            return None
        last = self._global_flow_day_from_event(event) or self._global_flow_drag_end or anchor
        moved = self._global_flow_drag_moved or last != anchor
        if moved:
            start_date, end_date, _duration_days = normalize_flow_drag_range(anchor, last)
            self._global_flow_drag_start = start_date
            self._global_flow_drag_end = end_date
        elif self._global_flow_drag_start is None or self._global_flow_drag_end is None:
            self._global_flow_drag_start = anchor
            self._global_flow_drag_end = anchor
        range_start, _range_end, _duration_days = normalize_flow_drag_range(
            self._global_flow_drag_start,
            self._global_flow_drag_end,
        )
        self._global_flow_drag_anchor = None
        self._global_flow_drag_moved = False
        try:
            if event.widget.grab_current() is event.widget:
                event.widget.grab_release()
        except tk.TclError:
            pass
        self.selected = range_start
        self._set_quick_placeholder()
        self._draw_calendar_flow()
        return "break"

    def _flow_selected_range(self) -> Optional[tuple[date, date, int]]:
        if self._global_flow_drag_start is None or self._global_flow_drag_end is None:
            return None
        return normalize_flow_drag_range(self._global_flow_drag_start, self._global_flow_drag_end)

    def _cancel_flow_drag(self, *, redraw: bool = True) -> None:
        had_drag = self._global_flow_drag_start is not None
        canvas = getattr(self, "global_flow_canvas", None)
        if canvas is not None:
            try:
                if canvas.grab_current() is canvas:
                    canvas.grab_release()
            except tk.TclError:
                pass
        self._global_flow_drag_start = None
        self._global_flow_drag_end = None
        self._global_flow_drag_anchor = None
        self._global_flow_drag_moved = False
        if had_drag and redraw and self.view_mode == "global" and self.global_display_mode == "flow":
            self._draw_calendar_flow()

    def _select_flow_item(self, item_id: str) -> str:
        self._cancel_flow_drag(redraw=False)
        self.select_timeline_item(item_id)
        return "break"

    def _open_flow_item(self, item_id: str) -> str:
        self.open_timeline_item(item_id)
        return "break"

    def _show_flow_item_menu(self, item_id: str, event: tk.Event) -> str:
        self._cancel_flow_drag(redraw=False)
        item = self.select_timeline_item(item_id)
        source = self.store.event_by_id(item.id) if item else None
        if source:
            menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
            menu.add_command(
                label="编辑",
                command=lambda: self._queue_context_menu_action(menu, lambda: self.open_editor(source)),
            )
            menu.add_command(
                label="取消完成" if source.done else "完成",
                command=lambda: self._queue_context_menu_action(menu, lambda: self.toggle_done(source)),
            )
            menu.add_separator()
            menu.add_command(
                label="删除",
                command=lambda: self._queue_context_menu_action(
                    menu,
                    lambda: self._confirm_delete(source, parent=self),
                ),
            )
            menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _queue_context_menu_action(self, menu: tk.Menu, callback: Callable[[], None]) -> None:
        """Run an action only after Tk has finished unposting its native menu."""

        def complete() -> None:
            try:
                menu.unpost()
                if menu.grab_current() is menu:
                    menu.grab_release()
            except tk.TclError:
                pass
            try:
                menu.destroy()
            except tk.TclError:
                pass
            callback()

        self.after_idle(complete)

    def _open_flow_day_detail(self, day: date) -> str:
        self.open_day_detail(day)
        return "break"

    def _create_flow_day(self, event: tk.Event) -> str:
        if self._canvas_has_timeline_item(event.widget):
            return "break"
        selected = self._global_flow_day_from_event(event)
        if selected is not None:
            selected_range = self._flow_selected_range()
            if not selected_range or not selected_range[0] <= selected <= selected_range[1]:
                selected_range = (selected, selected, 1)
            self._open_flow_range_editor(*selected_range)
        return "break"

    def _show_flow_day_menu(self, event: tk.Event) -> str:
        if self._canvas_has_timeline_item(event.widget):
            return "break"
        selected = self._global_flow_day_from_event(event)
        if selected is not None:
            selected_range = self._flow_selected_range()
            if not selected_range or not selected_range[0] <= selected <= selected_range[1]:
                self._global_flow_drag_start = selected
                self._global_flow_drag_end = selected
                selected_range = (selected, selected, 1)
            self._global_flow_drag_anchor = None
            self.selected = selected_range[0]
            self.timeline_selection.clear()
            self._draw_calendar_flow()
            self._show_flow_range_menu(selected_range, event.x_root, event.y_root)
        return "break"

    def _open_flow_range_editor(self, start_date: date, _end_date: date, duration_days: int) -> None:
        self.selected = start_date
        self._cancel_flow_drag(redraw=False)
        self.open_new_event(start_date, duration_days=duration_days)

    def _show_flow_range_menu(self, selected_range: tuple[date, date, int], x: int, y: int) -> None:
        start_date, end_date, duration_days = selected_range
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(
            label="新增事项",
            command=lambda: self._open_flow_range_editor(start_date, end_date, duration_days),
        )
        menu.add_command(label="新增习惯", command=self.open_routine_editor)
        status_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        for status, label in DATE_STATUS_LABELS.items():
            status_menu.add_command(
                label=label,
                command=lambda value=status: self._set_flow_range_status(start_date, end_date, value),
            )
        menu.add_cascade(label="设置日期状态", menu=status_menu)
        menu.add_separator()
        menu.add_command(label="取消选择", command=self._cancel_flow_drag)
        menu.tk_popup(x, y)

    def _set_flow_range_status(self, start_date: date, end_date: date, status: str) -> None:
        current = start_date
        while current <= end_date:
            self.store.set_date_status(current, status)
            current += timedelta(days=1)
        self.render()

    def _sync_global_xscroll(self, first: str, last: str) -> None:
        self.global_hscroll.set(first, last)
        self.global_date_canvas.xview_moveto(float(first))

    def _sync_global_yscroll(self, first: str, last: str) -> None:
        self.global_vscroll.set(first, last)
        self.global_label_canvas.yview_moveto(float(first))

    def _global_xview(self, *args) -> None:
        self.global_date_canvas.xview(*args)
        self.global_timeline_canvas.xview(*args)

    def _global_yview(self, *args) -> None:
        self.global_label_canvas.yview(*args)
        self.global_timeline_canvas.yview(*args)

    def _global_mousewheel(self, event: tk.Event) -> str:
        units = wheel_units(event.delta)
        if units:
            self._global_yview("scroll", units, "units")
        return "break"

    def _global_shift_mousewheel(self, event: tk.Event) -> str:
        units = wheel_units(event.delta)
        if units:
            self._global_xview("scroll", units * 3, "units")
        return "break"

    @staticmethod
    def _canvas_has_timeline_item(canvas: tk.Canvas) -> bool:
        current = canvas.find_withtag("current")
        return bool(current and any(tag.startswith("timeline-item:") for tag in canvas.gettags(current[0])))

    def _global_day_from_event(self, event: tk.Event) -> Optional[date]:
        if not self.timeline_model or not self._global_day_width:
            return None
        canvas = event.widget
        canvas_x = canvas.canvasx(event.x)
        return canvas_day_at(
            canvas_x,
            period_start=self.timeline_model.period_start,
            day_width=self._global_day_width,
            day_count=len(self.timeline_model.days),
        )

    def _select_global_day(self, event: tk.Event) -> Optional[str]:
        if self._canvas_has_timeline_item(event.widget):
            return None
        selected = self._global_day_from_event(event)
        if selected is None:
            return None
        self.selected = selected
        self._set_quick_placeholder()
        self._draw_active_global_view()
        return "break"

    def _create_global_day(self, event: tk.Event) -> str:
        if self._canvas_has_timeline_item(event.widget):
            return "break"
        selected = self._global_day_from_event(event)
        if selected is not None:
            self.selected = selected
            self.open_new_event(selected)
        return "break"

    def _show_global_day_menu(self, event: tk.Event) -> str:
        if self._canvas_has_timeline_item(event.widget):
            return "break"
        selected = self._global_day_from_event(event)
        if selected is not None:
            self.selected = selected
            self._draw_active_global_view()
            self.show_day_menu(selected, event.x_root, event.y_root)
        return "break"

    def _show_global_tooltip(self, text: str, event: tk.Event) -> None:
        if self._global_tooltip:
            self._global_tooltip.schedule(text, event.x_root, event.y_root)

    def _show_timeline_item_tooltip(self, item_id: str, event: tk.Event) -> None:
        if not self.timeline_model:
            return
        item = self.timeline_model.item_by_id(item_id)
        if item:
            self._show_global_tooltip(timeline_tooltip_text(item), event)

    def _hide_global_tooltip(self, _event=None) -> None:
        if self._global_tooltip:
            self._global_tooltip.hide()

    def _show_global_event_menu(self, item_id: str, event: tk.Event) -> None:
        item = self.select_timeline_item(item_id)
        source = self.store.event_by_id(item.id) if item else None
        if source:
            self.show_event_menu(source, event.x_root, event.y_root)

    def _update_global_detail(self, item: Optional[TimelineItem]) -> None:
        if not hasattr(self, "global_detail_title"):
            return
        if item is None:
            total = len(self.timeline_model.items) if self.timeline_model else 0
            self.global_detail_title.configure(text="选择一个事项查看详情")
            self.global_detail_meta.configure(text=f"本月共 {total} 项工作\n单击事项选择，双击直接编辑")
            self.global_detail_notes.configure(text="日期空白区域可双击新建事项。")
            if hasattr(self, "global_detail_category_dot"):
                self.global_detail_category_dot.delete("all")
                self.global_detail_category_dot.create_oval(
                    4,
                    4,
                    14,
                    14,
                    fill=self.theme.panel_secondary,
                    outline=self.theme.text_muted,
                    width=1,
                )
                self.global_detail_category_label.configure(text="无分类", fg=self.theme.text_muted)
                self.global_detail_color_label.configure(text="")
            if hasattr(self, "global_detail_state_label"):
                self.global_detail_state_label.configure(text="未选择", bg=self.theme.control_background, fg=self.theme.text_muted)
                self.global_detail_edit_button.accented = False
                self.global_detail_edit_button.outlined = True
                self.global_detail_edit_button.foreground = self.theme.text_disabled
                self.global_detail_edit_button.draw()
                self.global_detail_toggle_button.foreground = self.theme.text_disabled
                self.global_detail_toggle_button.draw()
                self.global_detail_delete_button.foreground = self.theme.text_disabled
                self.global_detail_delete_button.draw()
            self.global_detail_toggle_button.set_text("完成")
            return
        date_text = f"{item.start_date:%Y-%m-%d}" if item.start_date == item.end_date else f"{item.start_date:%Y-%m-%d} → {item.end_date:%Y-%m-%d}"
        duration = f"{item.calendar_span_days} 个自然日 · {item.effective_days_count} 个有效工作日"
        ddl_text = f"\nDDL：{item.ddl_date:%Y-%m-%d}" if item.ddl_date else ""
        category_text = getattr(item, "category_name", None) or "无分类"
        source_event = None
        store = getattr(self, "store", None)
        if store is not None and hasattr(store, "event_by_id"):
            source_event = store.event_by_id(item.id)
        self.global_detail_title.configure(text=item.title)
        state_text = detail_state_text(item)
        self.global_detail_meta.configure(
            text=f"日期：{date_text}\n\n持续：{duration}\n\n事项性质：{timeline_type_label(item)}\n\n事项分类：{category_text}{ddl_text}\n\n状态：{state_text}"
        )
        self.global_detail_notes.configure(text=f"备注\n{item.notes}" if item.notes else "备注\n暂无备注")
        if hasattr(self, "global_detail_category_dot"):
            category_color = getattr(item, "color", self.theme.text_muted)
            self.global_detail_category_dot.delete("all")
            self.global_detail_category_dot.create_oval(
                4,
                4,
                14,
                14,
                fill=category_color if category_text != "无分类" else self.theme.panel_secondary,
                outline=category_color,
                width=2 if category_text == "无分类" else 1,
            )
            self.global_detail_category_label.configure(text=category_text, fg=self.theme.text_primary)
            color_source = (
                "跟随分类"
                if source_event is not None and source_event.color_mode == "inherit"
                else "自定义颜色"
                if source_event is not None and source_event.category_id
                else "单独颜色"
            )
            self.global_detail_color_label.configure(text=color_source)
        if hasattr(self, "global_detail_state_label"):
            if item.completed:
                state_background, state_foreground = self.theme.card_done_background, self.theme.text_done
            elif state_text == "已逾期":
                state_background, state_foreground = self.theme.danger_soft, self.theme.danger
            elif getattr(item, "native_ddl", False) or item.ddl_date:
                state_background, state_foreground = self.theme.event_type_ddl_background, self.theme.event_type_ddl
            elif getattr(item, "is_urgent", False):
                state_background, state_foreground = self.theme.event_type_urgent_background, self.theme.event_type_urgent
            else:
                state_background, state_foreground = self.theme.control_background, self.theme.text_secondary
            self.global_detail_state_label.configure(text=state_text, bg=state_background, fg=state_foreground)
            self.global_detail_edit_button.accented = True
            self.global_detail_edit_button.outlined = False
            self.global_detail_edit_button.foreground = None
            self.global_detail_edit_button.draw()
            self.global_detail_toggle_button.foreground = None
            self.global_detail_toggle_button.draw()
            self.global_detail_delete_button.foreground = self.theme.danger
            self.global_detail_delete_button.draw()
        self.global_detail_toggle_button.set_text("取消完成" if item.completed else "完成")

    def _selected_timeline_event(self) -> Optional[Event]:
        item = self.get_selected_timeline_item()
        return self.store.event_by_id(item.id) if item else None

    def _edit_selected_timeline(self) -> None:
        event = self._selected_timeline_event()
        if event:
            self.open_editor(event)

    def _toggle_selected_timeline(self) -> None:
        event = self._selected_timeline_event()
        if event:
            self.toggle_done(event)

    def _delete_selected_timeline(self) -> None:
        event = self._selected_timeline_event()
        if event:
            self._confirm_delete(event, parent=self)

    def select_timeline_item(self, item_id: str) -> Optional[TimelineItem]:
        model = self.timeline_model or build_month_timeline(self.store, self.shown_year, self.shown_month)
        selected = self.timeline_selection.select(item_id, model)
        self._draw_active_global_view()
        return selected

    def get_selected_timeline_item(self) -> Optional[TimelineItem]:
        model = self.timeline_model or build_month_timeline(self.store, self.shown_year, self.shown_month)
        return self.timeline_selection.get(model)

    def clear_timeline_selection(self) -> None:
        self.timeline_selection.clear()
        if self.view_mode == "global":
            self._draw_active_global_view()

    def open_timeline_item(self, item_id: str) -> None:
        event = self.store.event_by_id(item_id)
        if event is not None:
            self.open_editor(event)

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
            empty_title = "休息日不安排习惯清单" if not self.store.is_workday(self.selected) and self.store.routines else "这一天很清静"
            if theme.style == "frutiger":
                empty = LogicalCanvas(
                    self.agenda_inner,
                    dpi=self.dpi,
                    height=126,
                    bg=theme.schedule_background,
                    bd=0,
                    highlightthickness=0,
                )
                empty.pack(fill="both", expand=True)
                empty.bind(
                    "<Configure>",
                    lambda _event, canvas=empty, title=empty_title: self._draw_frutiger_empty_state(canvas, title),
                )
                self.after_idle(lambda canvas=empty, title=empty_title: self._draw_frutiger_empty_state(canvas, title))
            else:
                empty = tk.Frame(self.agenda_inner, bg=theme.schedule_background, height=114)
                empty.pack(fill="both", expand=True)
                empty.pack_propagate(False)
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
            tk.Frame(row, bg=self.store.effective_event_color(item), width=3).pack(side="left", fill="y", padx=(0, 7))
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
        stripe = tk.Frame(
            card,
            bg=event_stripe_color(theme, item, self.store.effective_event_color(item)),
            width=EVENT_STRIPE_WIDTH,
        )
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

    def open_editor(
        self,
        event: Optional[Event] = None,
        selected: Optional[date] = None,
        *,
        initial_duration_days: int = 1,
    ) -> None:
        if self.editor_window and self.editor_window.winfo_exists():
            self.editor_window._present()
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
            self._lower_job = None
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        editor = EventEditor(
            self,
            selected or (event.due_date if event else self.selected),
            event,
            initial_duration_days=initial_duration_days,
        )
        self.editor_window = editor
        editor._present()

    def open_new_event(self, selected: Optional[date] = None, *, duration_days: int = 1) -> None:
        self.open_editor(selected=selected or self.selected, initial_duration_days=duration_days)

    def open_day_detail(self, day: Optional[date] = None) -> None:
        target_day = day or self.selected
        self.select_day(target_day)
        if self.day_detail_window and self.day_detail_window.winfo_exists():
            self.day_detail_window.set_day(target_day)
            self.present_overlay(self.day_detail_window)
            return
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        self.day_detail_window = DayDetailDialog(self, target_day)

    def open_routine_manager(self) -> None:
        if self.routine_manager and self.routine_manager.winfo_exists():
            self.present_overlay(self.routine_manager)
            return
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        self.routine_manager = RoutineManager(self)

    def open_category_manager(self) -> None:
        if self.category_editor and self.category_editor.winfo_exists():
            self.category_editor._present()
            return
        if self.category_manager and self.category_manager.winfo_exists():
            self.category_manager._present()
            return
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        self.category_manager = CategoryManager(self)

    def open_category_editor(self, category: Optional[EventCategory] = None) -> None:
        if self.category_editor and self.category_editor.winfo_exists():
            self.category_editor._present()
            return
        self.category_editor = None
        editor = CategoryEditor(self, category)
        self.category_editor = editor
        editor._present()

    def category_data_changed(self) -> None:
        self._prune_global_category_filter()
        self.store.save()
        if self.category_manager and self.category_manager.winfo_exists():
            self.category_manager.refresh()
        if self.editor_window and self.editor_window.winfo_exists():
            self.editor_window.refresh_categories()
        self.render()

    def _prune_global_category_filter(self) -> None:
        if self._global_category_filter_ids is None:
            return
        existing = {category.id for category in self.store.categories}
        self._global_category_filter_ids.intersection_update(existing)

    def _global_category_filter_label(self) -> str:
        categories = self.store.sorted_categories()
        if self._global_category_filter_ids is None and self._global_include_uncategorized:
            return "分类 · 全部"
        selected_count = len(self._global_category_filter_ids or ())
        if self._global_include_uncategorized:
            selected_count += 1
        total_count = len(categories) + 1
        return f"分类 · {selected_count}/{total_count}"

    def _refresh_global_category_filter_button(self) -> None:
        button = getattr(self, "global_category_filter_button", None)
        if button and button.winfo_exists():
            button.set_text(self._global_category_filter_label())

    def _category_filter_enabled(self, category_id: str) -> bool:
        return self._global_category_filter_ids is None or category_id in self._global_category_filter_ids

    def _refresh_global_category_sidebar(self) -> None:
        sidebar = getattr(self, "global_category_sidebar", None)
        if not sidebar or not sidebar.winfo_exists():
            return
        for child in sidebar.winfo_children():
            child.destroy()
        theme = self.theme
        dp = self.dpi.px
        sidebar.configure(width=dp(152 if self._global_category_sidebar_open else 38))
        if not self._global_category_sidebar_open:
            ThemeButton(
                sidebar,
                self,
                "›",
                self._toggle_global_category_sidebar,
                width=30,
                height=28,
                font_size=12,
                surface_background=theme.panel_secondary,
                outlined=True,
            ).pack(pady=(dp(8), dp(6)))
            for category in self.store.sorted_categories()[:8]:
                dot = LogicalCanvas(sidebar, dpi=self.dpi, width=26, height=22, bg=theme.panel_secondary, bd=0, highlightthickness=0, cursor="hand2")
                dot.pack(pady=dp(1))
                dot.create_oval(7, 5, 19, 17, fill=category.color if self._category_filter_enabled(category.id) else theme.panel_secondary, outline=category.color, width=2)
                dot.bind("<Button-1>", lambda _event, category_id=category.id: self._toggle_global_category(category_id))
                Tooltip(dot, category.name)
            uncategorized = LogicalCanvas(sidebar, dpi=self.dpi, width=26, height=22, bg=theme.panel_secondary, bd=0, highlightthickness=0, cursor="hand2")
            uncategorized.pack(pady=dp(1))
            uncategorized.create_oval(7, 5, 19, 17, fill=theme.panel_secondary, outline=theme.text_muted, width=2)
            if self._global_include_uncategorized:
                uncategorized.create_text(13, 11, text="✓", fill=theme.accent, font=(FONT, 6, "bold"))
            uncategorized.bind("<Button-1>", lambda _event: self._toggle_global_uncategorized())
            Tooltip(uncategorized, "无分类")
            return

        header = tk.Frame(sidebar, bg=theme.panel_secondary, padx=dp(6), pady=dp(8))
        header.pack(fill="x")
        tk.Label(header, text="事项分类", bg=theme.panel_secondary, fg=theme.text_primary, font=(FONT, 8, "bold")).pack(side="left")
        ThemeButton(header, self, "‹", self._toggle_global_category_sidebar, width=23, height=23, font_size=10, surface_background=theme.panel_secondary).pack(side="right")

        actions = tk.Frame(sidebar, bg=theme.panel_secondary, padx=dp(5))
        actions.pack(fill="x", pady=(0, dp(7)))
        ThemeButton(actions, self, "全部", self._select_all_global_categories, width=58, height=23, font_size=7, surface_background=theme.panel_secondary, outlined=True).pack(side="left")
        ThemeButton(actions, self, "全不选", self._clear_all_global_categories, width=58, height=23, font_size=7, surface_background=theme.panel_secondary, outlined=True).pack(side="right")

        list_shell = tk.Frame(sidebar, bg=theme.panel_secondary)
        list_shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(list_shell, bg=theme.panel_secondary, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            list_shell,
            orient="vertical",
            command=canvas.yview,
            style="Global.Vertical.TScrollbar",
        )
        inner = tk.Frame(canvas, bg=theme.panel_secondary)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        for category in self.store.sorted_categories():
            self._build_global_category_filter_row(
                inner,
                category.name,
                category.color,
                self._category_filter_enabled(category.id),
                lambda category_id=category.id: self._toggle_global_category(category_id),
                canvas,
            )
        self._build_global_category_filter_row(
            inner,
            "无分类",
            theme.text_muted,
            self._global_include_uncategorized,
            self._toggle_global_uncategorized,
            canvas,
            hollow=True,
        )

        footer = tk.Frame(sidebar, bg=theme.panel_secondary, padx=dp(5), pady=dp(8))
        footer.pack(fill="x", side="bottom")
        ThemeButton(footer, self, "管理分类…", self.open_category_manager, width=126, height=26, font_size=7, surface_background=theme.panel_secondary).pack(fill="x")

    def _build_global_category_filter_row(
        self,
        parent: tk.Widget,
        name: str,
        color: str,
        enabled: bool,
        command: Callable[[], None],
        scroll_canvas: tk.Canvas,
        *,
        hollow: bool = False,
    ) -> None:
        theme = self.theme
        dp = self.dpi.px
        background = blend(theme.panel_secondary, theme.accent_soft, 0.28) if enabled else theme.panel_secondary
        hover_background = theme.control_hover
        row = tk.Frame(parent, bg=background, padx=dp(4), pady=dp(6), cursor="hand2")
        row.pack(fill="x", padx=dp(3), pady=(0, dp(2)))
        dot = LogicalCanvas(row, dpi=self.dpi, width=16, height=18, bg=background, bd=0, highlightthickness=0, cursor="hand2")
        dot.pack(side="left", padx=(0, dp(2)))
        dot.create_oval(3, 4, 13, 14, fill=background if hollow or not enabled else color, outline=color, width=2)
        label = tk.Label(row, text=truncate(name, 12), bg=background, fg=theme.text_primary if enabled else theme.text_secondary, font=(FONT, 8), anchor="w", cursor="hand2")
        label.pack(side="left", fill="x", expand=True)
        check = tk.Label(row, text="✓" if enabled else "", bg=background, fg=theme.accent, font=(FONT, 8, "bold"), cursor="hand2", width=1)
        check.pack(side="right")
        widgets = (row, dot, label, check)

        def set_background(value: str) -> None:
            for widget in widgets:
                widget.configure(bg=value)

        for widget in widgets:
            widget.bind("<Button-1>", lambda _event: command())
            widget.bind("<MouseWheel>", lambda event: scroll_canvas.yview_scroll(int(-event.delta / 120), "units"))
            widget.bind("<Enter>", lambda _event: set_background(hover_background))
            widget.bind("<Leave>", lambda _event: set_background(background))

    def _toggle_global_category_sidebar(self) -> None:
        self._global_category_sidebar_open = not self._global_category_sidebar_open
        self._refresh_global_category_sidebar()
        self._schedule_global_render()

    def _toggle_global_category(self, category_id: str) -> None:
        all_ids = {category.id for category in self.store.categories}
        selected = set(all_ids if self._global_category_filter_ids is None else self._global_category_filter_ids)
        if category_id in selected:
            selected.remove(category_id)
        elif category_id in all_ids:
            selected.add(category_id)
        self._global_category_filter_ids = None if selected == all_ids else selected
        self._render_global_timeline()

    def _toggle_global_uncategorized(self) -> None:
        self._global_include_uncategorized = not self._global_include_uncategorized
        self._render_global_timeline()

    def _select_all_global_categories(self) -> None:
        self._global_category_filter_ids = None
        self._global_include_uncategorized = True
        self._render_global_timeline()

    def _clear_all_global_categories(self) -> None:
        self._global_category_filter_ids = set()
        self._global_include_uncategorized = False
        self._render_global_timeline()

    def show_global_category_filter(self) -> None:
        self._global_category_sidebar_open = True
        self._refresh_global_category_sidebar()
        self._schedule_global_render()

    def _reset_global_category_filter(self) -> None:
        self._global_category_filter_ids = None
        self._global_include_uncategorized = True
        self._render_global_timeline()

    def open_ddl_list(self) -> None:
        if self.ddl_list_window and self.ddl_list_window.winfo_exists():
            self.present_overlay(self.ddl_list_window)
            return
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        self.ddl_list_window = DDLListDialog(self)

    def open_routine_editor(self, item: Optional[RoutineItem] = None) -> None:
        if self.routine_editor and self.routine_editor.winfo_exists():
            self.present_overlay(self.routine_editor)
            return
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        self.routine_editor = RoutineEditor(self, item)

    def present_overlay(self, window: tk.Toplevel) -> None:
        if not window.winfo_exists():
            return
        self._register_overlay(window)
        self.attributes("-topmost", False)
        if self.view_mode == "compact":
            send_to_desktop(self)
        make_tool_window(window)
        window.attributes("-topmost", True)
        window.lift()
        bring_to_front(window)

    def present_modal(self, window: tk.Toplevel, parent: tk.Misc) -> None:
        """Present a transient modal without leaving it in the topmost band."""
        if not window.winfo_exists():
            return
        self._register_overlay(window)
        self.attributes("-topmost", False)
        try:
            parent.attributes("-topmost", False)
        except (AttributeError, tk.TclError):
            pass
        window.transient(parent)
        make_tool_window(window)
        window.attributes("-topmost", False)
        window.deiconify()
        window.lift()
        raise_for_interaction(window)
        window.focus_force()

    def restore_window_mode_if_idle(self) -> None:
        """Restore the main Z-order only when no newer modal owns interaction."""
        try:
            active_grab = self.grab_current()
        except (AttributeError, tk.TclError):
            active_grab = None
        if active_grab is not None and active_grab is not self:
            return
        self.apply_window_mode()

    def _register_overlay(self, window: tk.Toplevel) -> None:
        if window in self.overlay_windows:
            return
        self.overlay_windows.append(window)
        window.bind(
            "<Destroy>",
            lambda event, overlay=window: self._overlay_destroyed(event, overlay),
            add="+",
        )

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
        if self.view_mode == "global":
            make_app_window(self)
        else:
            make_tool_window(self)
        overlays = self._active_overlays()
        if overlays:
            self.attributes("-topmost", False)
            if self.view_mode == "compact":
                send_to_desktop(self)
            try:
                active_grab = self.grab_current()
            except (AttributeError, tk.TclError):
                active_grab = None
            if active_grab in overlays:
                # A grabbed overlay is an owned modal.  Keep it in the normal
                # foreground band; promoting every overlay to TOPMOST can make
                # the owner and modal compete during delayed layout restores.
                active_grab.attributes("-topmost", False)
                active_grab.lift()
                raise_for_interaction(active_grab)
                return
            for overlay in overlays:
                overlay.attributes("-topmost", True)
            bring_to_front(overlays[-1])
            return
        if self.view_mode == "global":
            self.attributes("-topmost", self.window_mode == "pinned")
            if self.window_mode == "pinned":
                self.lift()
            self._update_mode_badge()
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
        if self.view_mode == "global":
            label = "取消置顶" if self.window_mode == "pinned" else "窗口置顶"
            foreground = (
                self.theme.accent if self.window_mode == "pinned" else self.theme.text_secondary
            ) if self.theme.style != "aero" else self.theme.control_text
            self.mode_button.set_text(label, foreground)
            return
        if self.window_mode == "pinned":
            self.mode_button.set_text("置顶", self.theme.accent if self.theme.style != "aero" else self.theme.control_text)
        elif self.desktop_session_active:
            self.mode_button.set_text("前台", self.theme.accent if self.theme.style != "aero" else self.theme.control_text)
        else:
            self.mode_button.set_text("桌面", self.theme.text_secondary if self.theme.style != "aero" else self.theme.control_text)

    def _activate_desktop_session(self, _event=None) -> None:
        if self.view_mode != "compact" or not self._window_ready or self.window_mode != "desktop":
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
        if (
            self._startup_foreground_active
            or self.view_mode != "compact"
            or self.window_mode != "desktop"
            or not self.desktop_session_active
        ):
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
        self._lower_job = self.after(700, self._return_to_desktop_if_inactive)

    def _return_to_desktop_if_inactive(self) -> None:
        self._lower_job = None
        if self.view_mode != "compact" or self.window_mode != "desktop" or not self.desktop_session_active:
            return
        if self._active_overlays() or is_foreground_process():
            self._lower_job = self.after(900, self._return_to_desktop_if_inactive)
            return
        self._end_desktop_session()

    def _end_desktop_session(self) -> None:
        if self.view_mode != "compact" or self.window_mode != "desktop":
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
        if self.view_mode == "global":
            if self.state() == "normal":
                self._global_normal_geometry = self._capture_window_geometry()
            if self._global_normal_geometry is not None:
                self.store.settings["global_geometry"] = self._global_normal_geometry.as_dict()
        else:
            compact_geometry = self._capture_window_geometry()
            self.store.settings["compact_geometry"] = compact_geometry.as_dict()
            self.store.settings["x"] = compact_geometry.x
            self.store.settings["y"] = compact_geometry.y
        self.store.settings["view_mode"] = self.view_mode
        self.store.settings["global_display_mode"] = self.global_display_mode
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

    def _set_ddl_list_entry_state(self, state: str) -> None:
        if not hasattr(self, "ddl_list_label") or not self.ddl_list_label.winfo_exists():
            return
        if state == "pressed":
            background = self.theme.control_pressed
            foreground = self.theme.accent
        elif state == "hover":
            background = self.theme.control_hover
            foreground = self.theme.accent
        else:
            background = (
                blend(self.theme.schedule_background, self.theme.environment_haze, 0.28)
                if self.theme.style == "frutiger"
                else self.theme.schedule_background
            )
            foreground = self.theme.text_secondary
        self.ddl_list_label.configure(bg=background, fg=foreground)

    def _activate_ddl_list_entry(self, event: tk.Event) -> None:
        inside = 0 <= event.x < self.ddl_list_label.winfo_width() and 0 <= event.y < self.ddl_list_label.winfo_height()
        self._set_ddl_list_entry_state("hover" if inside else "normal")
        if inside:
            self.open_ddl_list()

    def show_main_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(
            label="切换为桌面模式" if self.window_mode == "pinned" else "始终置顶",
            command=self.toggle_window_mode,
        )
        if self.view_mode == "compact":
            menu.add_command(label="收起日程区" if self.agenda_open else "展开日程区", command=self.toggle_agenda)
        else:
            menu.add_command(label="返回紧凑视图", command=self.return_to_compact_view)
        menu.add_command(label="查看未来 7 天", command=lambda: UpcomingDialog(self))
        menu.add_command(label="管理事项分类…", command=self.open_category_manager)
        menu.add_command(label="管理习惯清单…", command=self.open_routine_manager)
        menu.add_command(label="查看全部 DDL…", command=self.open_ddl_list)
        menu.add_separator()
        theme_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        self.theme_var = tk.StringVar(value=self.theme_name)
        for theme_name, theme in THEMES.items():
            theme_menu.add_radiobutton(
                label=theme.display_name,
                variable=self.theme_var,
                value=theme_name,
                command=lambda name=theme_name: self.set_theme(name),
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
            "Ctrl+G：打开 / 关闭全局视图\n"
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
        tk.Frame(shell, bg=self.store.effective_event_color(event), height=5).pack(fill="x")
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
        if self._startup_foreground_restore_job:
            try:
                self.after_cancel(self._startup_foreground_restore_job)
            except tk.TclError:
                pass
            self._startup_foreground_restore_job = None
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
