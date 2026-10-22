"""Tk scheduling adapter. Renderers provide hit boxes; commands own mutations."""

import tkinter as tk
from tkinter import font as tkfont

from schedule_drag import ScheduleDrag, hit_at
from interaction_visuals import CanvasOverlay, elide, preview_bounds
from ui_draw import blend, draw_drag_bubble, pooled_rounded_rectangle, rounded_rectangle


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
        if hasattr(self, "_bulk_press") and self._bulk_press(event, hit.event_id):
            return "break"
        if hasattr(self, "_clear_feedback"):
            self._clear_feedback()
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
        self._schedule_source_style = {}
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
        key = (hit, operation) if operation in ("start", "end") else None
        if key == getattr(self, "_schedule_hover_key", None):
            return
        self._schedule_hover_key = key
        canvas.delete("schedule-edge-hover")
        if key:
            x1, y1, x2, y2 = hit.bounds
            x = x1 if operation == "start" else x2
            item = self.timeline_model.item_by_id(hit.event_id)
            rounded_rectangle(canvas, round(x - self.dpi.px(2)), round(y1 + self.dpi.px(4)),
                              round(x + self.dpi.px(2)), round(y2 - self.dpi.px(4)), self.dpi.px(2),
                              fill=self.theme.control_highlight, outline=item.color,
                              tags=("schedule-edge-hover", f"timeline-item:{hit.event_id}"))

    def _hide_schedule_edge(self, event):
        self._schedule_hover_key = None
        event.widget.delete("schedule-edge-hover")

    def _schedule_scroll_changed(self):
        if not getattr(self, "_schedule_drag", None):
            return
        event = self._schedule_pointer
        drag = self._schedule_drag
        drag.last_day = None
        drag.update(self._schedule_pointer_day(event), (event.x_root, event.y_root))
        if self._schedule_render_job is None:
            self._schedule_render_job = self.after(16, self._draw_schedule_preview)

    def _draw_flow_range_preview(self):
        bounds = self._flow_selected_range()
        if not bounds:
            return
        first, last, _duration = bounds
        canvas, dp = self.global_flow_canvas, self.dpi.px
        if not hasattr(canvas, "_range_overlay"):
            canvas._range_overlay = CanvasOverlay(canvas, "flow-range-preview")
        pool = canvas._range_overlay
        pool.begin()
        slot = 0
        for week in self.calendar_flow_layout.weeks:
            for column, day in enumerate(week.dates):
                if first <= day <= last:
                    x = column * self._global_flow_column_width
                    y = week.index * self._global_flow_week_height
                    pool.item(slot, "rectangle", (x + dp(4), y + dp(4), x + self._global_flow_column_width - dp(4),
                                                  y + self._global_flow_week_height - dp(4)),
                              outline=self.theme.date_selected_border, width=dp(2))
                    slot += 1
        pool.end()
        self.global_status_label.configure(text=f"已选择日期范围：{first:%Y-%m-%d} 至 {last:%Y-%m-%d} · 右键进行操作")

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
        if inside and drag.active and event.y > canvas.winfo_height() - self.dpi.px(16):
            canvas.yview_scroll(1, "units")
            self._schedule_scroll_changed()
        elif inside and drag.active and event.y < self.dpi.px(16):
            canvas.yview_scroll(-1, "units")
            self._schedule_scroll_changed()
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
        if hasattr(self, "bulk_selection") and len(self.bulk_selection.ids) > 1:
            return
        dp = self.dpi.px
        selected = self.timeline_selection.get(self.timeline_model)
        for hit in self._schedule_hits:
            if selected is None or selected.id != hit.event_id:
                continue
            x1, y1, x2, y2 = hit.bounds
            middle = round((y1 + y2) / 2)
            for edge, enabled in ((x1 + dp(3), hit.true_start), (x2 - dp(3), hit.true_end)):
                if enabled:
                    canvas.create_line(edge, middle - dp(4), edge, middle + dp(4),
                                       fill=blend(selected.color, self.theme.text_primary, .24), width=dp(1),
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
        if not hasattr(canvas, "_schedule_overlay"):
            canvas._schedule_overlay = CanvasOverlay(canvas, "schedule-preview")
        pool = canvas._schedule_overlay
        pool.begin()
        preview = drag.preview
        if not drag.active:
            pool.end()
            return
        theme, dp = self.theme, self.dpi.px
        for identifier in canvas.find_withtag(f"timeline-item:{drag.source.event_id}"):
            if identifier not in self._schedule_source_style:
                options = {"fill": canvas.itemcget(identifier, "fill")}
                if canvas.type(identifier) in ("rectangle", "polygon", "oval"):
                    options["outline"] = canvas.itemcget(identifier, "outline")
                self._schedule_source_style[identifier] = options
                canvas.itemconfigure(identifier, **{key: blend(value, theme.schedule_background, .72) for key, value in options.items() if value})
        if not hasattr(self, "_schedule_fonts") or self._schedule_fonts[0] != self.dpi.dpi:
            self._schedule_fonts = (self.dpi.dpi, tkfont.Font(self, family=self.search_font, size=8, weight="bold"),
                                    tkfont.Font(self, family=self.search_font, size=8))
        _, font, small = self._schedule_fonts
        event = self._schedule_pointer
        viewport = (canvas.canvasx(0), canvas.canvasy(0), canvas.canvasx(canvas.winfo_width()), canvas.canvasy(canvas.winfo_height()))
        box = preview_bounds(canvas.canvasx(event.x) + dp(16), canvas.canvasy(event.y) + dp(18), dp(260), dp(54), viewport, dp(6))
        if preview is None:
            header = getattr(self, "global_date_canvas", None)
            if header is not None and hasattr(header, "_schedule_overlay"):
                header._schedule_overlay.clear()
            canvas.configure(cursor="X_cursor")
            self.global_status_label.configure(text=f"无法排期：{drag.error} · Esc 取消")
            draw_drag_bubble(pool, box, theme=theme, color=theme.danger, title="无法放置 · Esc 取消",
                             subtitle=elide(drag.error, small, box[2] - box[0] - dp(26)), badge="",
                             font=font, small_font=small, scale=dp)
            pool.end()
            return
        canvas.configure(cursor="fleur" if drag.operation == "move" else "sb_h_double_arrow")
        days = preview.effective_dates
        self.global_status_label.configure(text=f"预览：{preview.event().title} · {days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d} · {len(days)}个有效日 · 松开提交 · Esc 取消 · 边缘停留或 PgUp/PgDn 切月")
        color = preview.effective_color or theme.accent
        if self.global_display_mode == "flow":
            locations = {day: (week.index, column) for week in self.calendar_flow_layout.weeks for column, day in enumerate(week.dates)}
            slot = 0
            for day in days:
                if day not in locations:
                    continue
                week, column = locations[day]
                x1 = column * self._global_flow_column_width + dp(2)
                y1 = week * self._global_flow_week_height + dp(2)
                pooled_rounded_rectangle(pool, ("snap", slot), x1, y1,
                                         x1 + self._global_flow_column_width - dp(4), y1 + dp(27),
                                         radius=dp(4), fill=blend(theme.schedule_background, color, .12),
                                         outline=color, width=dp(1), dash=(dp(3), dp(2)))
                slot += 1
                if day == preview.deadline:
                    pool.item("deadline", "text", (x1 + self._global_flow_column_width - dp(12), y1 + dp(14)),
                              text="◆", anchor="e", fill=theme.event_type_ddl, font=small)
        else:
            model = self.timeline_model
            y = self._schedule_origin_y
            slot = 0
            for day in days:
                if model.period_start <= day <= model.period_end:
                    x1 = (day - model.period_start).days * self._global_day_width + dp(2)
                    pooled_rounded_rectangle(pool, ("snap", slot), round(x1), round(y - dp(11)),
                                             round(x1 + self._global_day_width - dp(4)), round(y + dp(11)),
                                             radius=dp(4), fill=blend(theme.schedule_background, color, .12),
                                             outline=color, width=dp(1), dash=(dp(3), dp(2)))
                    slot += 1
            header = self.global_date_canvas
            if not hasattr(header, "_schedule_overlay"):
                header._schedule_overlay = CanvasOverlay(header, "schedule-preview")
            header_pool = header._schedule_overlay
            header_pool.begin()
            slot = 0
            for day in days:
                if model.period_start <= day <= model.period_end:
                    x = (day - model.period_start).days * self._global_day_width
                    header_pool.item(slot, "rectangle", (x + dp(2), dp(2), x + self._global_day_width - dp(2), header.winfo_height() - dp(2)),
                                     outline=color, width=dp(2))
                    slot += 1
            header_pool.end()
        draw_drag_bubble(pool, box, theme=theme, color=color,
                         title=elide(preview.event().title, font, box[2] - box[0] - dp(65)),
                         subtitle=elide(f"{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}", small, box[2] - box[0] - dp(26)),
                         badge="◆ DDL" if preview.deadline else "", font=font, small_font=small, scale=dp)
        pool.end()

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
            if self.commit_event_preview(preview, focus_result=True) and hasattr(self, "_show_feedback"):
                self._show_feedback(f"已调整至 {preview.effective_dates[0]:%m月%d日} → {preview.effective_dates[-1]:%m月%d日}")
                self._flash_event(event_id)
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
        self._schedule_hover_key = None
        for job_name in ("_schedule_render_job", "_schedule_month_job"):
            job = getattr(self, job_name, None)
            if job:
                self.after_cancel(job)
            setattr(self, job_name, None)
        canvas = self._schedule_canvas
        try:
            if canvas.grab_current() is canvas:
                canvas.grab_release()
            if hasattr(canvas, "_schedule_overlay"):
                canvas._schedule_overlay.clear()
            canvas.delete("schedule-edge-hover")
            for identifier, options in getattr(self, "_schedule_source_style", {}).items():
                if canvas.type(identifier):
                    canvas.itemconfigure(identifier, **options)
            self._schedule_source_style = {}
            header = getattr(self, "global_date_canvas", None)
            if header is not None and hasattr(header, "_schedule_overlay"):
                header._schedule_overlay.clear()
            canvas.configure(cursor="")
        except tk.TclError:
            pass  # The renderer can already have been destroyed on application exit.
        if hasattr(self, "global_status_label"):
            self.global_status_label.configure(text="已取消拖拽排期")
        return True

    def _schedule_check_focus(self):
        if getattr(self, "_schedule_drag", None) and self.focus_displayof() is None:
            self._cancel_schedule_drag()
