"""Global-only bulk adapter. Event history owns all mutations and persistence."""
import tkinter as tk
from tkinter import messagebox

from interaction_visuals import CanvasOverlay
from global_timeline_ui import GLOBAL_TIMELINE_LAYOUT
from timeline_model import build_month_timeline


class BulkActionsMixin:
    def _bulk_context(self):
        categories = self._global_category_filter_ids
        return (self.shown_year, self.shown_month, self.global_display_mode,
                None if categories is None else frozenset(categories), self._global_include_uncategorized,
                self._search_reveal_event_id)

    def _bulk_visible_order(self):
        model = self.timeline_model
        if model is None:
            return ()
        ids = tuple(item.id for item in model.items)
        if self.global_display_mode == "flow":
            shown = {hit.event_id for hit in self._schedule_hits}
            ids = tuple(identifier for identifier in ids if identifier in shown)
        return ids

    def _sync_bulk_selection(self):
        if self.bulk_selection.context != self._bulk_context():
            self.timeline_selection.clear()
        self.bulk_selection.reconcile(self._bulk_visible_order(), self._bulk_context())

    def _bulk_press(self, event, identifier):
        """Return True only for consumed modifier-selection gestures."""
        self._sync_bulk_selection()
        order = self._bulk_visible_order()
        state = getattr(event, "state", 0)
        ctrl, shift = bool(state & 0x4), bool(state & 0x1)
        if ctrl or (shift and self.global_display_mode == "timeline"):
            self._cancel_flow_drag(redraw=False)
            if shift and self.global_display_mode == "timeline":
                self.bulk_selection.range_to(identifier, order, additive=ctrl)
            else:
                self.bulk_selection.toggle(identifier, order)
            primary = identifier if identifier in self.bulk_selection.ids else next(iter(sorted(self.bulk_selection.ids)), None)
            if primary:
                self.timeline_selection.select(primary, self.timeline_model)
            else:
                self.timeline_selection.clear()
            self._update_global_detail(self.timeline_selection.get(self.timeline_model))
            self._refresh_bulk_visuals()
            event.widget.focus_set()
            return True
        # A plain drag always acts on this single item, never the entire group.
        self.bulk_selection.single(identifier, order)
        self.timeline_selection.select(identifier, self.timeline_model)
        self._refresh_bulk_visuals()
        return False

    def _bulk_label_press(self, event, identifier):
        if self._active_modal_window() is None and not self._bulk_press(event, identifier):
            self.select_timeline_item(identifier)
        return "break"

    def _bulk_open_item(self, event, identifier):
        if event.state & 0x5:
            # Timeline has a widget-level Double-Press binding (blank-day
            # creation), replacing its ordinary Press handler. Flow instead
            # handles Double-Release and still dispatches its ordinary Press;
            # do not toggle twice through Canvas tag + widget bindings.
            if self.global_display_mode == "timeline":
                self._bulk_press(event, identifier)
            return "break"
        self.select_timeline_item(identifier)
        self.open_timeline_item(identifier)
        return "break"

    def clear_bulk_selection(self):
        self.bulk_selection.clear()
        self.timeline_selection.clear()
        if self.view_mode == "global":
            self._refresh_bulk_visuals()
            self._update_global_detail(None)

    def _build_bulk_toolbar(self, *, button_factory, tooltip_factory, font_family):
        theme, dp = self.theme, self.dpi.px
        self.bulk_toolbar = tk.Frame(self.shell, bg=theme.panel_secondary, padx=dp(12), pady=dp(4))
        self.bulk_count_label = tk.Label(self.bulk_toolbar, bg=theme.panel_secondary, fg=theme.text_primary,
                                         font=(font_family, 8, "bold"))
        self.bulk_count_label.pack(side="left", padx=(0, dp(8)))
        self.bulk_buttons = {}
        options = (("完成", "全部完成", lambda: self.bulk_complete(True), 58),
                   ("取消完成", "全部取消完成", lambda: self.bulk_complete(False), 80),
                   ("分类", "分类", self.show_bulk_categories, 40),
                   ("日期", "调整日期…", self.show_bulk_dates, 68),
                   ("删除", "删除", self.bulk_delete, 40),
                   ("清空", "×", self.clear_bulk_selection, 24))
        for key, label, command, width in options:
            button = button_factory(self.bulk_toolbar, self, label, command, width=width, height=26,
                                    font_size=8, surface_background=theme.panel_secondary, outlined=True,
                                    foreground=theme.danger if key == "删除" else theme.control_text)
            button.pack(side="left", padx=(0, dp(4)))
            tooltip_factory(button, "取消所有选择（Esc）" if key == "清空" else f"对当前选中事项：{label}")
            self.bulk_buttons[key] = button

    def _refresh_bulk_visuals(self):
        if self.view_mode != "global" or not hasattr(self, "bulk_toolbar") or not self.bulk_toolbar.winfo_exists():
            return
        self._sync_bulk_selection()
        ids, dp = self.bulk_selection.ids, self.dpi.px
        count = len(ids)
        self.bulk_count_label.configure(text=f"已选择 {count} 项")
        if count >= 2:
            if not self.bulk_toolbar.winfo_manager():
                self.bulk_toolbar.pack(fill="x", before=self.global_workspace)
        else:
            self.bulk_toolbar.pack_forget()
        canvas = self.global_flow_canvas if self.global_display_mode == "flow" else self.global_timeline_canvas
        if not hasattr(canvas, "_bulk_overlay"):
            canvas._bulk_overlay = CanvasOverlay(canvas, "bulk-selection-outline")
        pool = canvas._bulk_overlay
        pool.begin()
        for index, hit in enumerate(self._schedule_hits):
            if hit.event_id not in ids:
                continue
            x1, y1, x2, y2 = hit.bounds
            outline = pool.item(index, "rectangle", (x1-dp(1), y1-dp(1), x2+dp(1), y2+dp(1)),
                                fill="", outline=self.theme.date_selected_border, width=dp(2))
            canvas.itemconfigure(outline, tags=(pool.tag, f"timeline-item:{hit.event_id}"))
        pool.end()
        if self.global_display_mode == "timeline":
            labels = self.global_label_canvas
            if not hasattr(labels, "_bulk_overlay"):
                labels._bulk_overlay = CanvasOverlay(labels, "bulk-label-outline")
            label_pool = labels._bulk_overlay
            label_pool.begin()
            row_height = dp(GLOBAL_TIMELINE_LAYOUT.row_height)
            for index, item in enumerate(self.timeline_model.items):
                if item.id in ids:
                    outline = label_pool.item(item.id, "rectangle", (dp(2), index*row_height+dp(2),
                        labels.winfo_width()-dp(2), (index+1)*row_height-dp(2)),
                        fill="", outline=self.theme.date_selected_border, width=dp(2))
                    labels.itemconfigure(outline, tags=(label_pool.tag, f"timeline-item:{item.id}"))
            label_pool.end()
        if count >= 2:
            canvas.delete("schedule-handle")
            canvas.delete("schedule-edge-hover")
            self.global_status_label.configure(text=f"已选择 {count} 项 · Ctrl+单击增减 · Esc 取消 · 仅操作当前可见选择")
        else:
            self.global_status_label.configure(text=("已选择 1 项 · " if count else "") +
                "Ctrl+单击多选 · Esc 取消选择 · 双击事项编辑")

    def _bulk_ids_for_action(self, expected=None):
        old_ids = frozenset(self.bulk_selection.ids)
        old_context = self.bulk_selection.context
        self._sync_bulk_selection()
        ids = frozenset(self.bulk_selection.ids)
        if old_context != self._bulk_context() or ids != old_ids or (expected is not None and ids != frozenset(expected)):
            raise ValueError("选择范围已变化，请重新选择后操作。")
        if self.view_mode != "global" or len(ids) < 2:
            raise ValueError("请至少选择两个可见事项。")
        # Recheck against live facts, not just a stale Canvas/model reference.
        model = build_month_timeline(self.store, self.shown_year, self.shown_month,
            category_ids=self._global_category_filter_ids, include_uncategorized=self._global_include_uncategorized,
            reveal_event_ids=(self._search_reveal_event_id,) if self._search_reveal_event_id else ())
        visible = {item.id for item in model.items}
        if not ids <= visible:
            self.clear_bulk_selection()
            raise ValueError("部分事项已删除或不可见，请重新选择。")
        return tuple(item.id for item in model.items if item.id in ids)

    def _bulk_message(self, text, *, confirm=False, owner=None):
        owner = owner or self._active_modal_window() or self
        function = messagebox.askyesno if confirm else messagebox.showerror
        return self._run_owned_bulk_modal(owner, lambda: function("批量管理", text, parent=owner))

    def _execute_bulk(self, operation, *, expected=None, **options):
        try:
            ids = self._bulk_ids_for_action(expected)
        except ValueError as exc:
            self._bulk_message(str(exc))
            return False
        success = self._run_event_command(lambda: self.event_commands.bulk(ids, operation, **options))
        if success:
            self.clear_bulk_selection()
        return success

    def bulk_complete(self, done):
        return self._execute_bulk("complete", done=done)

    def show_bulk_categories(self):
        try:
            ids = self._bulk_ids_for_action()
        except ValueError as exc:
            self._bulk_message(str(exc))
            return
        menu = tk.Menu(self, tearoff=False, font=(self.search_font, 9))
        menu.add_command(label="无分类", command=lambda: self._execute_bulk("category", expected=ids, category_id=None))
        for category in self.store.sorted_categories():
            menu.add_command(label=category.name, command=lambda value=category.id: self._execute_bulk("category", expected=ids, category_id=value))
        button = self.bulk_buttons["分类"]
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty()+button.winfo_height())
        finally:
            menu.grab_release()

    def show_bulk_dates(self):
        if self._active_modal_window() is not None:
            return
        try:
            ids = self._bulk_ids_for_action()
        except ValueError as exc:
            self._bulk_message(str(exc))
            return
        self._prepare_modal_open()
        BulkDateDialog(self, ids)

    def bulk_delete(self):
        try:
            ids = self._bulk_ids_for_action()
            if not self._bulk_message(f"确定删除选中的 {len(ids)} 个事项吗？", confirm=True):
                return False
            self._bulk_ids_for_action(ids)
            if not self._bulk_message(f"再次确认：删除这 {len(ids)} 个事项？\n可用一次 Ctrl+Z 撤销整批删除。", confirm=True):
                return False
            return self._execute_bulk("delete", expected=ids)
        except ValueError as exc:
            self._bulk_message(str(exc))
            return False


class BulkDateDialog(tk.Toplevel):
    def __init__(self, app, ids):
        super().__init__(app)
        self.withdraw()
        self.app, self.ids, self._closing = app, ids, False
        self.title("批量调整日期")
        theme, dp = app.theme, app.dpi.px
        self.configure(bg=theme.panel_background)
        frame = tk.Frame(self, bg=theme.panel_background, padx=dp(16), pady=dp(14))
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=f"调整 {len(ids)} 个事项的开始日期", bg=theme.panel_background,
                 fg=theme.text_primary, font=(app.search_font, 10, "bold")).pack(anchor="w")
        tk.Label(frame, text="正数延后，负数提前（自然日）。\n持续天数不变；实际工作日和 DDL 按原规则重算。",
                 bg=theme.panel_background, fg=theme.text_secondary, font=(app.search_font, 8), justify="left").pack(anchor="w", pady=dp(8))
        self.days = tk.StringVar(value="1")
        self.entry = tk.Entry(frame, textvariable=self.days, width=10, font=(app.search_font, 10),
                              bg=theme.input_background, fg=theme.text_primary, insertbackground=theme.text_primary)
        self.entry.pack(anchor="w")
        actions = tk.Frame(frame, bg=theme.panel_background)
        actions.pack(fill="x", pady=(dp(12), 0))
        tk.Button(actions, text="取消", command=self.close, bg=theme.control_background, fg=theme.control_text).pack(side="left")
        tk.Button(actions, text="应用", command=self.apply, bg=theme.accent, fg=theme.text_on_accent).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Return>", lambda _e: self.apply())
        self.update_idletasks()
        area = app.dpi.work_area()
        width, height = min(dp(360), area.width-dp(40)), min(self.winfo_reqheight(), area.height-dp(60))
        x = max(area.left+dp(10), min(area.right-width-dp(10), app.winfo_rootx()+(app.winfo_width()-width)//2))
        y = max(area.top+dp(10), min(area.bottom-height-dp(40), app.winfo_rooty()+dp(70)))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        app.present_modal(self, app)
        self.grab_set()
        self.entry.focus_set()

    def apply(self):
        try:
            days = int(self.days.get())
        except ValueError:
            self.app._bulk_message("请填写带正负号的整数天数。", owner=self)
            self.entry.focus_set()
            return
        if self.app._execute_bulk("shift", expected=self.ids, days=days):
            self.close()

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.grab_current() is self:
            self.grab_release()
        self.app._leave_modal(self)
        self.destroy()
