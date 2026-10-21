"""Tk scheduling adapter. Renderers provide hit boxes; commands own mutations."""

import tkinter as tk

from schedule_drag import ScheduleDrag, hit_at


class GlobalDragMixin:
    def _schedule_pointer_day(self, event):
        if self.global_display_mode == "flow":
            return self._global_flow_day_from_event(event)
        return self._global_day_from_event(event)

    def _schedule_press(self, event):
        if self._active_modal_window() is not None:
            return None
        self._cancel_schedule_drag()
        canvas = event.widget
        hit = hit_at(self._schedule_hits, canvas.canvasx(event.x), canvas.canvasy(event.y))
        if hit is None:
            return None
        day = self._schedule_pointer_day(event)
        if day is None:
            return None
        self._cancel_flow_drag(redraw=False)
        operation = hit.operation_at(canvas.canvasx(event.x), self.dpi.px(6))
        try:
            self._schedule_drag = ScheduleDrag(self.event_commands, hit.event_id, operation, day,
                                                (event.x_root, event.y_root), threshold=self.dpi.px(5))
        except ValueError as exc:
            self.global_status_label.configure(text=str(exc))
            return "break"
        self._schedule_canvas = canvas
        self._schedule_origin_y = (hit.bounds[1] + hit.bounds[3]) / 2
        self._schedule_pointer = event
        self._schedule_render_job = None
        self._schedule_month_job = None
        self._schedule_month_direction = 0
        canvas.focus_set()
        canvas.grab_set()
        self._hide_global_tooltip()
        return "break"

    def _schedule_hover(self, event):
        if getattr(self, "_schedule_drag", None):
            return
        canvas = event.widget
        hit = hit_at(self._schedule_hits, canvas.canvasx(event.x), canvas.canvasy(event.y))
        operation = hit.operation_at(canvas.canvasx(event.x), self.dpi.px(6)) if hit else None
        canvas.configure(cursor="sb_h_double_arrow" if operation in ("start", "end") else "fleur" if hit else "")

    def _schedule_motion(self, event):
        drag = getattr(self, "_schedule_drag", None)
        if drag is None:
            return None
        self._schedule_pointer = event
        canvas = self._schedule_canvas
        inside = 0 <= event.x < canvas.winfo_width() and 0 <= event.y < canvas.winfo_height()
        drag.update(self._schedule_pointer_day(event) if inside else None, (event.x_root, event.y_root))
        # Coalesce motion into one small overlay update per display frame. No
        # model rebuild, Store write, or full Canvas deletion occurs here.
        if self._schedule_render_job is None:
            self._schedule_render_job = self.after(16, self._draw_schedule_preview)
        direction = (-1 if event.x < self.dpi.px(18) else 1 if event.x > canvas.winfo_width() - self.dpi.px(18) else 0) if inside and drag.active else 0
        if direction != self._schedule_month_direction:
            if self._schedule_month_job:
                self.after_cancel(self._schedule_month_job)
            self._schedule_month_direction = direction
            self._schedule_month_job = self.after(750, self._schedule_turn_month) if direction else None
        # Keep a long Timeline / Flow vertically navigable without relocating
        # the source item during preview.
        if inside and event.y > canvas.winfo_height() - self.dpi.px(16):
            canvas.yview_scroll(1, "units")
        elif inside and event.y < self.dpi.px(16):
            canvas.yview_scroll(-1, "units")
        return "break"

    def _schedule_turn_month(self):
        self._schedule_month_job = None
        if not getattr(self, "_schedule_drag", None):
            return
        if self.global_display_mode == "timeline":
            first, last = self._schedule_canvas.xview()
            direction = self._schedule_month_direction
            if (direction < 0 and first > 0) or (direction > 0 and last < 1):
                self._global_xview("scroll", direction * 4, "units")
                self._schedule_motion(self._schedule_pointer)
                self._schedule_month_job = self.after(150, self._schedule_turn_month)
                return
        self._schedule_navigate_month(self._schedule_month_direction)

    def _decorate_schedule_handles(self, canvas):
        dp = self.dpi.px
        for hit in self._schedule_hits:
            x1, y1, x2, y2 = hit.bounds
            middle = (y1 + y2) / 2
            for edge, enabled in ((x1 + dp(3), hit.true_start), (x2 - dp(3), hit.true_end)):
                if enabled:
                    canvas.create_line(edge, middle - dp(4), edge, middle + dp(4),
                                       fill=self.theme.text_primary, width=dp(2),
                                       tags=(f"timeline-item:{hit.event_id}", "schedule-handle"))

    def _schedule_navigate_month(self, delta):
        """A deliberate month transition is the only full redraw during drag."""
        if not delta:
            return
        index = self.shown_year * 12 + self.shown_month - 1 + delta
        year, month_index = divmod(index, 12)
        if not 1 <= year <= 9999:
            return
        self.shown_year, self.shown_month = year, month_index + 1
        self._render_global_timeline(refresh_sidebar=False)
        event = self._schedule_pointer
        self._schedule_drag.last_day = None
        self._schedule_motion(event)

    def _schedule_page_month(self, delta):
        if getattr(self, "_schedule_drag", None):
            self._schedule_navigate_month(delta)
        else:
            self.change_month(delta)
        return "break"

    def _draw_schedule_preview(self):
        self._schedule_render_job = None
        drag = getattr(self, "_schedule_drag", None)
        if drag is None:
            return
        canvas = self._schedule_canvas
        canvas.delete("schedule-preview")
        preview = drag.preview
        if not drag.active:
            return
        if preview is None:
            canvas.configure(cursor="X_cursor")
            self.global_status_label.configure(text=f"无法排期：{drag.error} · Esc 取消")
            return
        canvas.configure(cursor="fleur" if drag.operation == "move" else "sb_h_double_arrow")
        days = preview.effective_dates
        self.global_status_label.configure(text=f"预览 {days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d} · {len(days)}个有效日 · 松开提交 · Esc 取消 · 边缘停留或 PgUp/PgDn 切月")
        theme, dp = self.theme, self.dpi.px
        color = preview.effective_color or theme.accent
        if self.global_display_mode == "flow":
            locations = {day: (week.index, column) for week in self.calendar_flow_layout.weeks for column, day in enumerate(week.dates)}
            for day in days:
                if day not in locations:
                    continue
                week, column = locations[day]
                x1 = column * self._global_flow_column_width + dp(2)
                y1 = week * self._global_flow_week_height + dp(2)
                canvas.create_rectangle(x1, y1, x1 + self._global_flow_column_width - dp(4),
                                        y1 + self._global_flow_week_height - dp(4),
                                        outline=theme.accent, width=dp(2), dash=(dp(4), dp(2)), tags="schedule-preview")
                canvas.create_rectangle(x1 + dp(3), y1 + dp(29), x1 + self._global_flow_column_width - dp(8), y1 + dp(40),
                                        fill=color, outline=theme.accent, stipple="gray50", tags="schedule-preview")
        else:
            model = self.timeline_model
            y = self._schedule_origin_y
            for day in days:
                if model.period_start <= day <= model.period_end:
                    x1 = (day - model.period_start).days * self._global_day_width + dp(2)
                    canvas.create_rectangle(x1, y - dp(15), x1 + self._global_day_width - dp(4), y + dp(15),
                                            fill=color, outline=theme.accent, width=dp(2), stipple="gray50", tags="schedule-preview")
        canvas.tag_raise("schedule-preview")

    def _schedule_release(self, event):
        drag = getattr(self, "_schedule_drag", None)
        if drag is None:
            return None
        self._schedule_motion(event)
        event_id = drag.source.event_id
        was_active = drag.active
        preview = drag.finish()
        self._cancel_schedule_drag()
        if preview is not None:
            self.commit_event_preview(preview, focus_result=True)
        elif not was_active:
            self.select_timeline_item(event_id)
        else:
            self.global_status_label.configure(text="未更改排期")
        return "break"

    def _cancel_schedule_drag(self):
        drag = getattr(self, "_schedule_drag", None)
        if drag is None:
            return False
        drag.cancel()
        self._schedule_drag = None
        for job_name in ("_schedule_render_job", "_schedule_month_job"):
            job = getattr(self, job_name, None)
            if job:
                self.after_cancel(job)
            setattr(self, job_name, None)
        canvas = self._schedule_canvas
        try:
            if canvas.grab_current() is canvas:
                canvas.grab_release()
            canvas.delete("schedule-preview")
            canvas.configure(cursor="")
        except tk.TclError:
            pass  # The renderer can already have been destroyed on application exit.
        if hasattr(self, "global_status_label"):
            self.global_status_label.configure(text="已取消拖拽排期")
        return True

    def _schedule_check_focus(self):
        if getattr(self, "_schedule_drag", None) and self.focus_displayof() is None:
            self._cancel_schedule_drag()
