"""Small schedule form embedded in the existing habit editor."""
from datetime import date
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

from habit_schedule import SCHEDULE_FIELDS, schedule_label, validate_schedule_input

CHOICES = ("每天", "每隔 N 天", "每周", "每月", "每年", "自定义…")
UNITS = {"天": "daily", "周": "weekly", "月": "monthly", "年": "yearly"}


def rule_summary(values):
    """Use the same normalized rule and label as saved habits, never a second matcher."""
    return schedule_label(SimpleNamespace(kind="habit", **values))


class HabitScheduleForm(tk.Frame):
    def __init__(self, master, app, item, font_family):
        theme = app.theme
        super().__init__(master, bg=theme.panel_background)
        self.theme = theme
        self.dp = app.dpi.px
        self.font = (font_family, 9)
        self.original = {key: getattr(item, key) for key in SCHEDULE_FIELDS} if item else {}
        anchor = date.fromisoformat(item.start_on) if item else app.selected
        frequency = item.frequency if item else "daily"
        interval = item.interval if item else 1
        choice = {"daily": "每天" if interval == 1 else "每隔 N 天", "weekly": "每周", "monthly": "每月", "yearly": "每年"}[frequency]
        self.choice = tk.StringVar(value=choice)
        self.unit = tk.StringVar(value=next(key for key, value in UNITS.items() if value == frequency))
        self.interval = tk.StringVar(value=str(interval))
        self.start = tk.StringVar(value=anchor.isoformat())
        self.end = tk.StringVar(value=item.end_on if item and item.end_on else "")
        self.ending = tk.StringVar(value="指定日期" if self.end.get() else "永不结束")
        self.month_day = tk.StringVar(value=str(item.month_day if item else anchor.day))
        self.year_month = tk.StringVar(value=str(item.year_month if item else anchor.month))
        self.workdays = tk.BooleanVar(value=item.workdays_only if item else False)
        self.weekdays = [tk.BooleanVar(value=(index in item.weekdays if item else index == anchor.weekday())) for index in range(7)]

        row = self.row()
        self.label(row, "重复周期").pack(side="left")
        selector = ttk.Combobox(row, textvariable=self.choice, values=CHOICES, state="readonly", width=13, font=self.font)
        selector.pack(side="right")
        selector.bind("<<ComboboxSelected>>", self.refresh)
        self.options = self.row()
        self.label(self.options, "每隔").pack(side="left")
        self.entry(self.options, self.interval, 5).pack(side="left", padx=self.dp(5))
        self.unit_label = self.label(self.options, "天")
        self.custom_unit = ttk.Combobox(self.options, textvariable=self.unit, values=tuple(UNITS), state="readonly", width=4, font=self.font)
        self.custom_unit.bind("<<ComboboxSelected>>", self.refresh)
        self.week_row = self.row()
        for text, variable in zip("一二三四五六日", self.weekdays):
            self.check(self.week_row, "周" + text, variable, compact=True).pack(side="left", expand=True, fill="x", padx=self.dp(1))
        self.month_row = self.row()
        self.month_label = self.label(self.month_row, "月份")
        self.month_entry = self.entry(self.month_row, self.year_month, 4)
        self.day_label = self.label(self.month_row, "日期")
        self.day_label.pack(side="left")
        self.entry(self.month_row, self.month_day, 4).pack(side="left", padx=self.dp(5))
        self.label(self.month_row, "日").pack(side="left")
        row = self.row()
        self.start_row = row
        self.label(row, "开始日期").pack(side="left")
        self.entry(row, self.start, 12).pack(side="right")
        row = self.row()
        self.end_entry = self.entry(row, self.end, 12)
        self.end_entry.pack(side="right")
        ending = ttk.Combobox(row, textvariable=self.ending, values=("永不结束", "指定日期"), width=10, state="readonly", font=self.font)
        ending.pack(side="left")
        ending.bind("<<ComboboxSelected>>", self.refresh)
        row = self.row()
        self.check(row, "仅工作日（含调休；跳过假期和请假日）", self.workdays).pack(anchor="w")
        self.summary = tk.StringVar()
        tk.Label(self, textvariable=self.summary, bg=theme.accent_soft, fg=theme.text_primary,
                 font=(font_family, 8), anchor="w", justify="left", wraplength=self.dp(330),
                 padx=self.dp(8), pady=self.dp(6)).pack(fill="x", pady=self.dp(5))
        self.label(self, "间隔以开始日期为锚点；不存在的月末日期、非闰年的\n2 月 29 日直接跳过，不顺延。", muted=True).pack(anchor="w", pady=self.dp(4))
        if item and item.habit_done:
            self.label(self, f"历史打卡（{len(item.habit_done)} 次，修改周期不会删除）").pack(anchor="w", pady=(self.dp(6), 0))
            history_row = self.row()
            history = tk.Text(history_row, height=3, width=1, wrap="word", font=self.font,
                              bg=theme.input_background, fg=theme.text_secondary, relief="flat")
            bar = ttk.Scrollbar(history_row, command=history.yview, style="Global.Vertical.TScrollbar")
            bar.pack(side="right", fill="y")
            history.pack(side="left", fill="both", expand=True)
            history.configure(yscrollcommand=bar.set)
            history.insert("1.0", "\n".join(reversed(item.habit_done)))
            history.configure(state="disabled")
        for variable in (self.choice, self.unit, self.interval, self.start, self.end, self.ending,
                         self.month_day, self.year_month, self.workdays, *self.weekdays):
            variable.trace_add("write", self._update_summary)
        self.refresh()

    def row(self):
        row = tk.Frame(self, bg=self.theme.panel_background)
        row.pack(fill="x", pady=self.dp(3))
        return row

    def label(self, parent, text, muted=False):
        return tk.Label(parent, text=text, bg=self.theme.panel_background,
                        fg=self.theme.text_muted if muted else self.theme.text_secondary,
                        font=self.font, anchor="w", justify="left", wraplength=self.dp(355))

    def entry(self, parent, variable, width):
        return tk.Entry(parent, textvariable=variable, width=width, font=self.font,
                        bg=self.theme.input_background, fg=self.theme.text_primary,
                        insertbackground=self.theme.text_primary, relief="flat",
                        disabledbackground=self.theme.control_background, disabledforeground=self.theme.text_muted,
                        highlightthickness=self.dp(1), highlightbackground=self.theme.input_border,
                        highlightcolor=self.theme.input_focus)

    def check(self, parent, text, variable, compact=False):
        return tk.Checkbutton(parent, text=text, variable=variable, font=self.font,
                              bg=self.theme.control_background if compact else self.theme.panel_background,
                              fg=self.theme.text_primary, indicatoron=not compact,
                              activebackground=self.theme.control_hover,
                              activeforeground=self.theme.text_primary,
                              selectcolor=self.theme.accent_soft if compact else self.theme.input_background,
                              relief="flat", offrelief="flat", borderwidth=0,
                              padx=self.dp(2), pady=self.dp(4 if compact else 0),
                              cursor="hand2", highlightthickness=0)

    def _update_summary(self, *_args):
        try:
            text = rule_summary(self.value())
        except ValueError as exc:
            text = str(exc)
        self.summary.set(text)

    def frequency(self):
        return {"每天": "daily", "每隔 N 天": "daily", "每周": "weekly", "每月": "monthly", "每年": "yearly"}.get(
            self.choice.get(), UNITS[self.unit.get()])

    def refresh(self, _event=None):
        frequency = self.frequency()
        self.options.pack_forget()
        self.week_row.pack_forget()
        self.month_row.pack_forget()
        # Restore dynamic rows before the always-present start-date row.
        before = self.start_row
        if self.choice.get() != "每天":
            self.options.pack(fill="x", pady=self.dp(3), before=before)
            self.unit_label.pack_forget()
            self.custom_unit.pack_forget()
            if self.choice.get() == "自定义…":
                self.custom_unit.pack(side="left")
            else:
                self.unit_label.configure(text=next(key for key, value in UNITS.items() if value == frequency))
                self.unit_label.pack(side="left")
        if frequency == "weekly":
            self.week_row.pack(fill="x", pady=self.dp(3), before=before)
        if frequency in ("monthly", "yearly"):
            self.month_row.pack(fill="x", pady=self.dp(3), before=before)
            self.month_label.pack_forget()
            self.month_entry.pack_forget()
            if frequency == "yearly":
                self.month_label.pack(side="left", before=self.day_label)
                self.month_entry.pack(side="left", padx=self.dp(5), before=self.day_label)
        self.end_entry.configure(state="normal" if self.ending.get() == "指定日期" else "disabled")
        self._update_summary()

    def value(self):
        return validate_schedule_input(
            frequency=self.frequency(), interval=1 if self.choice.get() == "每天" else self.interval.get(),
            weekdays=[index for index, value in enumerate(self.weekdays) if value.get()],
            month_day=self.month_day.get(), year_month=self.year_month.get(), start_on=self.start.get(),
            end_on=self.end.get() if self.ending.get() == "指定日期" else None, workdays_only=self.workdays.get())
