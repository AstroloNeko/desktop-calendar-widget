from __future__ import annotations

import calendar
import queue
import shutil
import sys
import threading
import traceback
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from calendar_core import (
    APP_DIR,
    APP_NAME,
    COLORS,
    DATA_DIR,
    PRIORITIES,
    REMINDERS,
    WEEKDAYS,
    Event,
    Store,
)
from holiday_data import HolidayInfo, holiday_for
from tray_icon import TrayIcon
from win_integration import (
    SingleInstance,
    bring_to_front,
    clamp_to_work_area,
    is_autostart_enabled,
    make_tool_window,
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


WINDOW_WIDTH = 372
OPEN_HEIGHT = 548
CLOSED_HEIGHT = 338

SURFACE = "#F7F6F2"
CARD = "#FFFFFF"
INK = "#25262B"
SUBTLE = "#777A83"
FAINT = "#A9ABB2"
BORDER = "#D8D7D2"
HOVER = "#ECECF1"
ACCENT = "#6273D9"
ACCENT_SOFT = "#E8EAF8"
WEEKEND = "#BC6B6B"
DANGER = "#D9515D"
FONT = "Microsoft YaHei UI"


def geometry_at(width: int, height: int, x: int, y: int) -> str:
    return f"{width}x{height}{x:+d}{y:+d}"


def position_at(x: int, y: int) -> str:
    return f"{x:+d}{y:+d}"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


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
    bg: str = SURFACE,
    fg: str = SUBTLE,
    hover: str = HOVER,
    font_size: int = 10,
) -> tk.Label:
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
        tk.Label(
            self.window,
            text=self.text,
            bg="#303136",
            fg="white",
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


class DayCell(tk.Canvas):
    def __init__(self, parent: tk.Widget, app: "CalendarApp", column: int) -> None:
        super().__init__(parent, width=46, height=36, bg=SURFACE, bd=0, highlightthickness=0, cursor="hand2")
        self.app = app
        self.column = column
        self.day = date.today()
        self.in_month = True
        self.selected = False
        self.today = False
        self.hovered = False
        self.event_colors: list[str] = []
        self.holiday: Optional[HolidayInfo] = None
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
    ) -> None:
        self.day = day
        self.in_month = in_month
        self.selected = selected
        self.today = today
        self.event_colors = colors[:3]
        self.holiday = holiday
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
        self.app.open_editor(selected=self.day)
        return "break"

    def _right_click(self, event: tk.Event) -> None:
        self.app.select_day(self.day)
        self.app.show_day_menu(self.day, event.x_root, event.y_root)

    def draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 38)
        center_x = width / 2
        if self.hovered and not self.selected:
            self.create_oval(center_x - 14, 1, center_x + 14, 25, fill=HOVER, outline="")
        if self.selected:
            self.create_oval(center_x - 14, 1, center_x + 14, 25, fill=ACCENT, outline="")
        elif self.today:
            self.create_oval(center_x - 13, 2, center_x + 13, 24, outline=ACCENT, width=1.5)

        if self.selected:
            color = "white"
        elif not self.in_month:
            color = "#C2C3C7"
        elif self.column >= 5:
            color = WEEKEND
        else:
            color = INK
        weight = "bold" if self.today or self.selected else "normal"
        self.create_text(center_x, 13, text=str(self.day.day), fill=color, font=(FONT, 9, weight))

        if self.holiday and self.in_month:
            holiday_color = "white" if self.selected else {
                "day_off": DANGER,
                "workday": "#C47B28",
            }.get(self.holiday.kind, "#8B70A8")
            self.create_text(
                center_x,
                27,
                text=truncate(self.holiday.short_name, 3),
                fill=holiday_color,
                font=(FONT, 6, "bold" if self.holiday.kind != "festival" else "normal"),
            )

        if self.event_colors:
            gap = 7
            start = center_x - (len(self.event_colors) - 1) * gap / 2
            for index, event_color in enumerate(self.event_colors):
                x = start + index * gap
                dot_color = "white" if self.selected else event_color
                self.create_oval(x - 2, 32, x + 2, 36, fill=dot_color, outline="")


class EventEditor(tk.Toplevel):
    WIDTH = 400
    HEIGHT = 560

    def __init__(self, master: "CalendarApp", selected: date, event: Optional[Event] = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.event = event
        self.title("编辑日程" if event else "新建日程")
        self.configure(bg=BORDER)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")

        due = event.due_at if event else self._suggest_due(selected)
        self.title_var = tk.StringVar(value=event.title if event else "")
        self.date_var = tk.StringVar(value=due.strftime("%Y-%m-%d"))
        self.time_var = tk.StringVar(value=due.strftime("%H:%M"))
        self.priority_var = tk.StringVar(value=event.priority if event else "普通")
        self.color_var = tk.StringVar(value=event.color if event else COLORS["海盐蓝"])
        reminder_value = event.reminder if event else master.store.settings.get("default_reminder", 60)
        reminder_label = next((label for label, value in REMINDERS.items() if value == reminder_value), "提前 1 小时")
        self.reminder_var = tk.StringVar(value=reminder_label)
        self._drag_origin: Optional[tuple[int, int, int, int]] = None
        self.color_canvases: list[tuple[tk.Canvas, str]] = []

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
            bg="#FBFBFA",
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
        time_col = tk.Frame(date_row, bg=CARD, width=115)
        time_col.pack(side="right", padx=(6, 0))
        self._field_label(date_col, "日期")
        self._flat_entry(date_col, self.date_var).pack(fill="x", ipady=6)
        self._field_label(time_col, "时间")
        self._flat_entry(time_col, self.time_var, width=10).pack(fill="x", ipady=6)

        shortcuts = tk.Frame(shell, bg=CARD)
        shortcuts.pack(fill="x", pady=(7, 10))
        for label, offset in (("今天", 0), ("明天", 1), ("一周后", 7)):
            chip = tk.Label(shortcuts, text=label, bg="#F0F1F4", fg=SUBTLE, font=(FONT, 8), padx=8, pady=3, cursor="hand2")
            chip.pack(side="left", padx=(0, 6))
            chip.bind("<Button-1>", lambda _event, days=offset: self.date_var.set((date.today() + timedelta(days=days)).isoformat()))

        self._field_label(shell, "优先级")
        priority_row = tk.Frame(shell, bg=CARD)
        priority_row.pack(fill="x", pady=(4, 10))
        for priority in PRIORITIES:
            radio = tk.Radiobutton(
                priority_row,
                text=priority,
                variable=self.priority_var,
                value=priority,
                indicatoron=False,
                bg="#F0F1F4",
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
            )
            radio.pack(side="left", padx=(0, 6))

        reminder_row = tk.Frame(shell, bg=CARD)
        reminder_row.pack(fill="x", pady=(0, 10))
        reminder_col = tk.Frame(reminder_row, bg=CARD)
        reminder_col.pack(side="left", fill="x", expand=True)
        self._field_label(reminder_col, "提醒")
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
            swatch = tk.Canvas(color_row, width=28, height=28, bg=CARD, bd=0, highlightthickness=0, cursor="hand2")
            swatch.pack(side="left", padx=(0, 8))
            swatch.bind("<Button-1>", lambda _event, value=color: self._choose_color(value))
            self.color_canvases.append((swatch, color))
        self._draw_colors()

        self._field_label(shell, "备注（可选）")
        self.notes = tk.Text(
            shell,
            width=38,
            height=3,
            bg="#FBFBFA",
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
            delete = tk.Button(actions, text="删除", command=self.delete, bg="#FCEDEF", fg=DANGER, relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
            delete.pack(side="left")
        save = tk.Button(actions, text="保存", command=self.save, bg=ACCENT, fg="white", activebackground="#5263C6", activeforeground="white", relief="flat", bd=0, padx=20, pady=7, font=(FONT, 9, "bold"), cursor="hand2")
        save.pack(side="right")
        cancel = tk.Button(actions, text="取消", command=self.close, bg="#F0F1F4", fg=SUBTLE, relief="flat", bd=0, padx=14, pady=7, cursor="hand2")
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
    def _suggest_due(day: date) -> datetime:
        now = datetime.now()
        if day == now.date():
            candidate = now + timedelta(minutes=30)
            minute = 30 if candidate.minute <= 30 else 0
            if minute == 0:
                candidate += timedelta(hours=1)
            return candidate.replace(minute=minute, second=0, microsecond=0)
        return datetime.combine(day, datetime.min.time()).replace(hour=18)

    @staticmethod
    def _field_label(parent: tk.Widget, text: str) -> None:
        tk.Label(parent, text=text, bg=CARD, fg=SUBTLE, font=(FONT, 8)).pack(anchor="w")

    @staticmethod
    def _flat_entry(parent: tk.Widget, variable: tk.StringVar, width: int = 18) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            bg="#FBFBFA",
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
        x = self.master_app.winfo_rootx() + (self.master_app.winfo_width() - self.WIDTH) // 2
        y = self.master_app.winfo_rooty() + 10
        x, y = clamp_to_work_area(x, y, self.WIDTH, self.HEIGHT)
        self.geometry(geometry_at(self.WIDTH, self.HEIGHT, x, y))

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
        try:
            due = datetime.strptime(f"{self.date_var.get().strip()} {self.time_var.get().strip()}", "%Y-%m-%d %H:%M")
        except ValueError:
            messagebox.showinfo(APP_NAME, "日期或时间格式不正确。\n请使用 YYYY-MM-DD 和 HH:MM。", parent=self)
            return
        item = Event(
            id=self.event.id if self.event else str(uuid.uuid4()),
            title=title,
            due=due.isoformat(timespec="minutes"),
            color=self.color_var.get(),
            priority=self.priority_var.get(),
            reminder=REMINDERS[self.reminder_var.get()],
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
        self.master_app.after(120, self.master_app.apply_window_mode)


class UpcomingDialog(tk.Toplevel):
    def __init__(self, master: "CalendarApp") -> None:
        super().__init__(master)
        self.master_app = master
        self.title("未来 7 天")
        self.configure(bg=BORDER)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry("360x450")

        shell = tk.Frame(self, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(shell, bg=CARD, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="未来 7 天", bg=CARD, fg=INK, font=(FONT, 13, "bold")).pack(side="left")
        close = button_label(header, "×", self.close, width=2, bg=CARD, font_size=13)
        close.pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")

        canvas = tk.Canvas(shell, bg=CARD, bd=0, highlightthickness=0, width=336)
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
            if item.due_date != last_day:
                last_day = item.due_date
                day_text = "今天" if last_day == date.today() else f"{last_day.month}月{last_day.day}日 · {WEEKDAYS[last_day.weekday()]}"
                tk.Label(inner, text=day_text, bg=CARD, fg=SUBTLE, font=(FONT, 8, "bold"), anchor="w").pack(fill="x", pady=(8, 4))
            row = tk.Frame(inner, bg="#F6F6F4", cursor="hand2", padx=8, pady=7)
            row.pack(fill="x", pady=2)
            tk.Frame(row, bg=item.color, width=4).pack(side="left", fill="y", padx=(0, 8))
            title = tk.Label(row, text=truncate(item.title, 22), bg="#F6F6F4", fg=INK, font=(FONT, 9), anchor="w")
            title.pack(side="left", fill="x", expand=True)
            when = "逾期" if item.is_overdue else item.due_at.strftime("%H:%M")
            meta = tk.Label(row, text=when, bg="#F6F6F4", fg=DANGER if item.is_overdue else SUBTLE, font=(FONT, 8))
            meta.pack(side="right")
            for widget in (row, title, meta):
                widget.bind("<Button-1>", lambda _event, event=item: self._edit(event))

        self.bind("<Escape>", lambda _event: self.close())
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - 360) // 2
        y = master.winfo_rooty() + 40
        x, y = clamp_to_work_area(x, y, 360, 450)
        self.geometry(geometry_at(360, 450, x, y))
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
        self.geometry("340x138")
        shell = tk.Frame(self, bg=CARD, padx=18, pady=14)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(shell, text="桌面月历更新", bg=CARD, fg=INK, font=(FONT, 11, "bold"), anchor="w").pack(fill="x")
        self.status_label = tk.Label(shell, text=status, bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w")
        self.status_label.pack(fill="x", pady=(7, 8))
        self.progress = ttk.Progressbar(shell, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x")
        self.progress.start(12)
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - 340) // 2
        y = master.winfo_rooty() + 90
        x, y = clamp_to_work_area(x, y, 340, 138)
        self.geometry(geometry_at(340, 138, x, y))
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
        self.store = store or Store()
        self.instance_guard = instance
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
        self.update_dialog: Optional[UpdateProgressDialog] = None
        self.update_busy = False
        self.show_holidays = bool(self.store.settings.get("show_holidays", True))
        self.tray_icon: Optional[TrayIcon] = None
        self.tray_actions: queue.Queue[str] = queue.Queue()

        self.title(APP_NAME)
        icon_path = resource_path("assets/calendar.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.configure(bg=BORDER)
        self.overrideredirect(True)
        try:
            opacity = min(1.0, max(0.82, float(self.store.settings.get("opacity", 0.97))))
        except (TypeError, ValueError):
            opacity = 0.97
        self.attributes("-alpha", opacity)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._configure_style()
        self._build_ui()
        self._set_initial_geometry()
        self._bind_shortcuts()
        self.render()
        self.after(80, self._finish_window_setup)
        self.after(220, self._start_tray_icon)
        self.after(250, self._poll_tray_actions)
        self.after(1200, self.check_reminders)
        if self.store.load_error:
            self.after(250, lambda: messagebox.showwarning(APP_NAME, f"日历数据读取失败，已先打开空日历。\n\n{self.store.load_error}", parent=self))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TCombobox", padding=4, font=(FONT, 9))

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        log_exception(exc_type, exc_value, exc_traceback)
        try:
            messagebox.showerror(APP_NAME, f"操作没有完成，错误已经记录。\n\n{exc_value}", parent=self)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        self.shell = tk.Frame(self, bg=SURFACE)
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)

        self.header = tk.Frame(self.shell, bg=SURFACE, height=56, padx=12)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        month_box = tk.Frame(self.header, bg=SURFACE)
        month_box.pack(side="left", fill="y")
        self.month_label = tk.Label(month_box, text="", bg=SURFACE, fg=INK, font=(FONT, 13, "bold"), cursor="hand2")
        self.month_label.pack(anchor="w", pady=(8, 0))
        self.month_hint = tk.Label(month_box, text="", bg=SURFACE, fg=SUBTLE, font=(FONT, 8))
        self.month_hint.pack(anchor="w")
        self.month_label.bind("<Button-1>", lambda _event: self.go_today())

        controls = tk.Frame(self.header, bg=SURFACE)
        controls.pack(side="right", fill="y", pady=10)
        previous = button_label(controls, "‹", lambda: self.change_month(-1), width=2, font_size=14)
        previous.pack(side="left")
        today = button_label(controls, "今", self.go_today, width=2, font_size=9)
        today.pack(side="left")
        following = button_label(controls, "›", lambda: self.change_month(1), width=2, font_size=14)
        following.pack(side="left")
        self.mode_button = button_label(controls, "桌面", self.toggle_window_mode, width=4, font_size=8)
        self.mode_button.pack(side="left", padx=(3, 0))
        self.menu_button = button_label(controls, "···", self.show_main_menu, width=3, font_size=10)
        self.menu_button.pack(side="left")
        close = button_label(controls, "×", self.on_close, width=2, fg=SUBTLE, hover="#F6DFE2", font_size=12)
        close.pack(side="left")
        Tooltip(previous, "上个月（滚轮向上 / PgUp）")
        Tooltip(today, "回到今天（Ctrl+T）")
        Tooltip(following, "下个月（滚轮向下 / PgDn）")
        Tooltip(self.mode_button, "桌面模式不遮挡应用；点击可切换置顶")
        Tooltip(close, "退出，提醒也会停止")

        for widget in (self.header, month_box, self.month_hint):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        weekdays = tk.Frame(self.shell, bg=SURFACE, padx=12)
        weekdays.pack(fill="x")
        for column, name in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            weekdays.grid_columnconfigure(column, weight=1, uniform="weekday")
            tk.Label(
                weekdays,
                text=name,
                bg=SURFACE,
                fg=WEEKEND if column >= 5 else FAINT,
                font=(FONT, 8),
                pady=2,
            ).grid(row=0, column=column, sticky="ew")

        self.calendar_frame = tk.Frame(self.shell, bg=SURFACE, padx=12, pady=2)
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

        tk.Frame(self.shell, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(3, 0))

        self.agenda_bar = tk.Frame(self.shell, bg=SURFACE, height=39, padx=12, cursor="hand2")
        self.agenda_bar.pack(fill="x")
        self.agenda_bar.pack_propagate(False)
        self.agenda_toggle = tk.Label(self.agenda_bar, text="⌃", bg=SURFACE, fg=SUBTLE, font=(FONT, 10), cursor="hand2")
        self.agenda_toggle.pack(side="left", padx=(0, 6))
        self.agenda_title = tk.Label(self.agenda_bar, text="", bg=SURFACE, fg=INK, font=(FONT, 10, "bold"), cursor="hand2")
        self.agenda_title.pack(side="left")
        self.agenda_count = tk.Label(self.agenda_bar, text="", bg=SURFACE, fg=SUBTLE, font=(FONT, 8), cursor="hand2")
        self.agenda_count.pack(side="left", padx=(7, 0))
        add = button_label(self.agenda_bar, "+", lambda: self.open_editor(), width=2, bg=SURFACE, fg=ACCENT, hover=ACCENT_SOFT, font_size=13)
        add.pack(side="right", pady=4)
        Tooltip(add, "添加这一天的日程（Ctrl+N 或双击日期）")
        for widget in (self.agenda_bar, self.agenda_toggle, self.agenda_title, self.agenda_count):
            widget.bind("<Button-1>", lambda _event: self.toggle_agenda())

        self.agenda_body = tk.Frame(self.shell, bg=SURFACE)
        self._build_agenda_body()
        if self.agenda_open:
            self.agenda_body.pack(fill="both", expand=True)

    def _build_agenda_body(self) -> None:
        list_shell = tk.Frame(self.agenda_body, bg=SURFACE, padx=10)
        list_shell.pack(fill="both", expand=True)
        self.agenda_canvas = tk.Canvas(list_shell, bg=SURFACE, bd=0, highlightthickness=0, width=340, height=126)
        self.agenda_scrollbar = ttk.Scrollbar(list_shell, orient="vertical", command=self.agenda_canvas.yview)
        self.agenda_inner = tk.Frame(self.agenda_canvas, bg=SURFACE)
        self.agenda_window = self.agenda_canvas.create_window((0, 0), window=self.agenda_inner, anchor="nw")
        self.agenda_canvas.configure(yscrollcommand=self.agenda_scrollbar.set)
        self.agenda_canvas.pack(side="left", fill="both", expand=True)
        self.agenda_inner.bind("<Configure>", lambda _event: self.agenda_canvas.configure(scrollregion=self.agenda_canvas.bbox("all")))
        self.agenda_canvas.bind("<Configure>", lambda event: self.agenda_canvas.itemconfigure(self.agenda_window, width=event.width))
        self.agenda_canvas.bind("<MouseWheel>", self._agenda_wheel)
        self.agenda_inner.bind("<MouseWheel>", self._agenda_wheel)

        quick = tk.Frame(self.agenda_body, bg=SURFACE, padx=12, pady=5)
        quick.pack(fill="x")
        self.quick_var = tk.StringVar(value="")
        self.quick_entry = tk.Entry(
            quick,
            textvariable=self.quick_var,
            bg="#EEEDE9",
            fg=FAINT,
            insertbackground=INK,
            relief="flat",
            font=(FONT, 9),
        )
        self.quick_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.quick_entry.bind("<FocusIn>", self._quick_focus_in)
        self.quick_entry.bind("<FocusOut>", self._quick_focus_out)
        self.quick_entry.bind("<Return>", self.quick_add)
        detail = button_label(quick, "详细", lambda: self.open_editor(), width=4, bg=SURFACE, fg=SUBTLE, hover=HOVER, font_size=8)
        detail.pack(side="right", padx=(6, 0))

        footer = tk.Frame(self.agenda_body, bg=SURFACE, padx=13, height=21)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        self.upcoming_label = tk.Label(footer, text="", bg=SURFACE, fg=SUBTLE, font=(FONT, 8), cursor="hand2")
        self.upcoming_label.pack(side="left")
        self.upcoming_label.bind("<Button-1>", lambda _event: UpcomingDialog(self))
        Tooltip(self.upcoming_label, "查看未来 7 天和已逾期日程")
        tk.Label(footer, text="双击日期可快速新建", bg=SURFACE, fg=FAINT, font=(FONT, 8)).pack(side="right")

    def _set_initial_geometry(self) -> None:
        height = OPEN_HEIGHT if self.agenda_open else CLOSED_HEIGHT
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        saved_x = self.store.settings.get("x")
        saved_y = self.store.settings.get("y")
        try:
            x = int(saved_x) if saved_x is not None else screen_w - WINDOW_WIDTH - 26
            y = int(saved_y) if saved_y is not None else 44
        except (TypeError, ValueError):
            x, y = screen_w - WINDOW_WIDTH - 26, 44
        x, y = clamp_to_work_area(x, y, WINDOW_WIDTH, height)
        self.geometry(geometry_at(WINDOW_WIDTH, height, x, y))

    def _finish_window_setup(self) -> None:
        make_tool_window(self)
        self.apply_window_mode()

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-n>", lambda _event: self.open_editor())
        self.bind("<Control-t>", lambda _event: self.go_today())
        self.bind("<Home>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-t>", lambda event: self._keyboard_command(event, self.go_today))
        self.bind("<Key-n>", lambda event: self._keyboard_command(event, self.open_editor))
        self.bind("<Return>", lambda event: self._keyboard_command(event, self.open_editor))
        self.bind("<Prior>", lambda _event: self.change_month(-1))
        self.bind("<Next>", lambda _event: self.change_month(1))
        self.bind("<Left>", lambda event: self._move_selection(event, -1))
        self.bind("<Right>", lambda event: self._move_selection(event, 1))
        self.bind("<Up>", lambda event: self._move_selection(event, -7))
        self.bind("<Down>", lambda event: self._move_selection(event, 7))
        self.bind("<FocusOut>", self._on_focus_out)

    def render(self) -> None:
        self.month_label.configure(text=f"{self.shown_year}年 {self.shown_month}月")
        today = date.today()
        self.month_hint.configure(text=f"今天 {today.month}月{today.day}日 · {WEEKDAYS[today.weekday()]}")
        self.mode_button.configure(text="置顶" if self.window_mode == "pinned" else "桌面")

        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self.shown_year, self.shown_month)
        while len(weeks) < 6:
            start = weeks[-1][-1] + timedelta(days=1)
            weeks.append([start + timedelta(days=index) for index in range(7)])
        events_by_day: dict[date, list[Event]] = {}
        for item in self.store.events:
            events_by_day.setdefault(item.due_date, []).append(item)

        for index, cell in enumerate(self.day_cells):
            row, column = divmod(index, 7)
            day = weeks[row][column]
            day_events = sorted(events_by_day.get(day, []), key=lambda event: (event.done, event.due_at))
            colors = [event.color if not event.done else "#C5C6CA" for event in day_events]
            cell.update_day(
                day=day,
                in_month=day.month == self.shown_month,
                selected=day == self.selected,
                today=day == today,
                colors=colors,
                holiday=holiday_for(day) if self.show_holidays else None,
            )
        self.render_agenda()

    def render_agenda(self) -> None:
        for child in self.agenda_inner.winfo_children():
            child.destroy()
        events = self.store.events_on(self.selected)
        holiday = holiday_for(self.selected) if self.show_holidays else None
        holiday_text = f" · {holiday.name}" if holiday else ""
        self.agenda_title.configure(text=f"{self.selected.month}月{self.selected.day}日 · {WEEKDAYS[self.selected.weekday()]}{holiday_text}")
        self.agenda_count.configure(text=f"{len(events)} 项" if events else "无安排")
        self.agenda_toggle.configure(text="⌃" if self.agenda_open else "⌄")

        if not events:
            empty = tk.Frame(self.agenda_inner, bg=SURFACE, height=114)
            empty.pack(fill="both", expand=True)
            empty.pack_propagate(False)
            tk.Label(empty, text="这一天很清静", bg=SURFACE, fg=SUBTLE, font=(FONT, 9)).pack(pady=(27, 2))
            tk.Label(empty, text="双击日期，或在下方直接输入", bg=SURFACE, fg=FAINT, font=(FONT, 8)).pack()
        else:
            for item in events:
                self._build_event_card(item)

        upcoming_count = len(self.store.upcoming(7, include_overdue=True))
        self.upcoming_label.configure(text=f"未来 7 天 · {upcoming_count} 项  ›")
        self._set_quick_placeholder()
        self.after_idle(self._update_scrollbar)

    def _build_event_card(self, item: Event) -> None:
        card_bg = "#F0F0ED" if item.done else CARD
        card = tk.Frame(self.agenda_inner, bg=card_bg, highlightthickness=1, highlightbackground="#E4E3DF", cursor="hand2")
        card.pack(fill="x", pady=(0, 5), padx=1)
        stripe = tk.Frame(card, bg="#C5C6CA" if item.done else item.color, width=4)
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        check = tk.Label(
            card,
            text="✓" if item.done else "○",
            bg=card_bg,
            fg="#999BA2" if item.done else item.color,
            font=(FONT, 11, "bold"),
            width=3,
            cursor="hand2",
        )
        check.pack(side="left", fill="y", padx=(3, 0))
        check.bind("<Button-1>", lambda _event, event=item: self.toggle_done(event))
        content = tk.Frame(card, bg=card_bg, padx=1, pady=5)
        content.pack(side="left", fill="both", expand=True)
        title = tk.Label(
            content,
            text=truncate(item.title, 24),
            bg=card_bg,
            fg="#96989E" if item.done else INK,
            font=(FONT, 9, "overstrike" if item.done else "normal"),
            anchor="w",
        )
        title.pack(fill="x")
        if item.is_overdue:
            timing = "已逾期 · " + item.due_at.strftime("%H:%M")
            timing_color = DANGER
        else:
            timing = item.due_at.strftime("%H:%M") + f" · {item.priority}优先级"
            timing_color = SUBTLE
        meta = tk.Label(content, text=timing, bg=card_bg, fg=timing_color, font=(FONT, 8), anchor="w")
        meta.pack(fill="x", pady=(1, 0))
        more = tk.Label(card, text="›", bg=card_bg, fg=FAINT, font=(FONT, 12), width=2, cursor="hand2")
        more.pack(side="right", fill="y")
        for widget in (card, content, title, meta, more, stripe):
            widget.bind("<Button-1>", lambda _event, event=item: self.open_editor(event))
            widget.bind("<Button-3>", lambda event, item=item: self.show_event_menu(item, event.x_root, event.y_root))
            widget.bind("<MouseWheel>", self._agenda_wheel)

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
        self.selected = event.due_date
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

    def quick_add(self, _event=None) -> str:
        title = self.quick_var.get().strip()
        if not title or self.quick_placeholder_active:
            return "break"
        self.store.create_quick(title, self.selected)
        self.quick_var.set("")
        self.quick_placeholder_active = False
        self.render()
        self.quick_entry.focus_set()
        return "break"

    def _set_quick_placeholder(self) -> None:
        if self.focus_get() == self.quick_entry and not self.quick_placeholder_active:
            return
        if not self.quick_placeholder_active and self.quick_var.get().strip():
            return
        self.quick_placeholder_active = True
        self.quick_var.set(f"快速添加到 {self.selected.month}月{self.selected.day}日，回车保存")
        self.quick_entry.configure(fg=FAINT)

    def _quick_focus_in(self, _event=None) -> None:
        if self.quick_placeholder_active:
            self.quick_var.set("")
            self.quick_placeholder_active = False
            self.quick_entry.configure(fg=INK)

    def _quick_focus_out(self, _event=None) -> None:
        if not self.quick_var.get().strip():
            self._set_quick_placeholder()

    def toggle_agenda(self) -> None:
        self.agenda_open = not self.agenda_open
        if self.agenda_open:
            self.agenda_body.pack(fill="both", expand=True)
        else:
            self.agenda_body.pack_forget()
        height = OPEN_HEIGHT if self.agenda_open else CLOSED_HEIGHT
        self.geometry(geometry_at(WINDOW_WIDTH, height, self.winfo_x(), self.winfo_y()))
        self.agenda_toggle.configure(text="⌃" if self.agenda_open else "⌄")
        self.store.settings["agenda_open"] = self.agenda_open
        self._save_window_settings()
        self.after(80, self.apply_window_mode)

    def toggle_window_mode(self) -> None:
        self.window_mode = "pinned" if self.window_mode == "desktop" else "desktop"
        self.store.settings["window_mode"] = self.window_mode
        self.apply_window_mode()
        self.store.save()

    def apply_window_mode(self) -> None:
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
            send_to_desktop(self)
        self.mode_button.configure(text="置顶" if self.window_mode == "pinned" else "桌面")

    def _on_focus_out(self, _event=None) -> None:
        if self.window_mode != "desktop":
            return
        if self._lower_job:
            try:
                self.after_cancel(self._lower_job)
            except tk.TclError:
                pass
        self._lower_job = self.after(120, self.apply_window_mode)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if not self._drag_origin:
            return
        start_x, start_y, win_x, win_y = self._drag_origin
        self.geometry(position_at(win_x + event.x_root - start_x, win_y + event.y_root - start_y))

    def _end_drag(self, _event=None) -> None:
        self._drag_origin = None
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
        self.store.save()

    def show_day_menu(self, day: date, x: int, y: int) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
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

    def _confirm_delete(self, event: Event) -> None:
        if messagebox.askyesno(APP_NAME, f"确定删除“{event.title}”？", parent=self):
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
        if not self.agenda_open:
            self.toggle_agenda()
        self.quick_entry.focus_set()

    def show_main_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False, font=(FONT, 9))
        menu.add_command(
            label="切换为桌面模式" if self.window_mode == "pinned" else "临时置顶",
            command=self.toggle_window_mode,
        )
        menu.add_command(label="收起日程区" if self.agenda_open else "展开日程区", command=self.toggle_agenda)
        menu.add_command(label="查看未来 7 天", command=lambda: UpcomingDialog(self))
        menu.add_separator()
        opacity_menu = tk.Menu(menu, tearoff=False, font=(FONT, 9))
        current_opacity = round(float(self.attributes("-alpha")), 2)
        self.opacity_var = tk.DoubleVar(value=current_opacity)
        for label, value in (("100%", 1.0), ("97%", 0.97), ("92%", 0.92), ("85%", 0.85)):
            opacity_menu.add_radiobutton(label=label, variable=self.opacity_var, value=value, command=lambda v=value: self.set_opacity(v))
        menu.add_cascade(label="透明度", menu=opacity_menu)
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

    def set_opacity(self, value: float) -> None:
        self.attributes("-alpha", value)
        self.store.settings["opacity"] = value
        self.store.save()

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
        message = "启动完成。双击此图标可显示月历，右键可新建日程或检查更新。" if first_for_version else None
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
            "双击日期：新建当天日程\n"
            "右键日期：打开快捷菜单\n"
            "单击日程：编辑；圆圈：完成\n"
            "滚轮 / PgUp / PgDn：切换月份\n"
            "方向键：移动所选日期\n"
            "Ctrl+N：新建日程\n"
            "Ctrl+T：回到今天\n"
            "拖动顶部：移动挂件位置\n\n"
            "桌面模式会待在普通应用窗口后面；需要临时覆盖其他窗口时，点击顶部“桌面”切换为置顶。",
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
                trigger = event.due_at - timedelta(minutes=event.reminder)
            if trigger is None:
                continue
            key = f"{event.id}:{kind}:{trigger.isoformat(timespec='minutes')}"
            latest = event.due_at + timedelta(hours=2) if kind == "reminder" else trigger + timedelta(minutes=10)
            if trigger <= now <= latest and key not in self.store.notified:
                self.store.notified.add(key)
                if kind == "snooze":
                    event.snooze_until = None
                self.show_notification(event)
                changed = True
        if changed:
            self.store.save()
            self.render()
        self.after(15000, self.check_reminders)

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
        popup.geometry("340x168")
        shell = tk.Frame(popup, bg=CARD)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(shell, bg=event.color, height=5).pack(fill="x")
        tk.Label(shell, text="DDL 提醒", bg=CARD, fg=SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(10, 1))
        tk.Label(shell, text=truncate(event.title, 26), bg=CARD, fg=INK, font=(FONT, 12, "bold"), anchor="w").pack(fill="x", padx=15)
        due_text = event.due_at.strftime("%m月%d日 %H:%M")
        if event.is_overdue:
            due_text += " · 已逾期"
        tk.Label(shell, text=due_text, bg=CARD, fg=DANGER if event.is_overdue else SUBTLE, font=(FONT, 8), anchor="w").pack(fill="x", padx=15, pady=(3, 8))
        actions = tk.Frame(shell, bg=CARD, padx=12)
        actions.pack(fill="x")
        tk.Button(actions, text="稍后 10 分钟", command=lambda: self.snooze_event(event, popup), bg="#F0F1F4", fg=SUBTLE, relief="flat", bd=0, padx=8, pady=5, cursor="hand2").pack(side="left")
        tk.Button(actions, text="完成", command=lambda: self.complete_from_notification(event, popup), bg=ACCENT, fg="white", relief="flat", bd=0, padx=12, pady=5, cursor="hand2").pack(side="right")
        tk.Button(actions, text="知道了", command=popup.destroy, bg="#F0F1F4", fg=SUBTLE, relief="flat", bd=0, padx=10, pady=5, cursor="hand2").pack(side="right", padx=(0, 6))
        popup.update_idletasks()
        offset = min(len(self.notification_windows), 3) * 178
        x = popup.winfo_screenwidth() - 358
        y = popup.winfo_screenheight() - 225 - offset
        popup.geometry(geometry_at(340, 168, x, max(8, y)))
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
