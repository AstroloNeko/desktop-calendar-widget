"""Isolated v0.9 visual acceptance captures; PNG/data never enter user storage.

Run with --phase before/after. Non-native DPI values exercise the real Tk
layout path, without changing the user's Windows display settings.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import struct
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Event, EventCategory, Store
from dpi_utils import scaled_geometry
from global_timeline_ui import GLOBAL_TIMELINE_LAYOUT
from tools.check_frameless_global import assert_header
from tools.check_interactions import cleanup, position_hit, pump
from tools.check_window_drag import native_signatures, save_png
from win_integration import window_handle


def fixture(store):
    today = date.today()
    store.categories = [EventCategory("art", "设计创作", "#8B70D6"),
                        EventCategory("work", "工作项目", "#6687F2"),
                        EventCategory("life", "生活计划", "#52B788")]
    specs = [("design", "秋季主视觉 · 设计与交付", -2, 6, "art", "general", False),
             ("urgent", "项目评审与客户反馈", -1, 3, "work", "urgent", False),
             ("deadline", "提交最终方案", 0, 1, "work", "ddl", False),
             ("skip", "工作日创作计划", -3, 7, "art", "general", False),
             ("life", "周末出行计划", 2, 2, "life", "general", False),
             ("done", "已完成 · 整理项目资料", -2, 1, "work", "general", True)]
    store.events = [Event(key, title, f"{today + timedelta(days=offset)}T18:00",
                          duration_days=duration, category_id=category, color_mode="inherit",
                          event_type=kind, done=done, skip_non_working_days=key == "skip",
                          end_as_ddl=key == "design", notes="复核细节，留出收尾时间。")
                    for key, title, offset, duration, category, kind, done in specs]
    store.settings.update(theme="modern", view_mode="compact", window_mode="desktop", opacity=1.0)
    store.save()
    return today


def capture(window, target):
    window.lift()
    window.update_idletasks()
    pump(window, .12)
    # Print the actual mapped HWND; also works when desktop capture is unavailable.
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = window_handle(window)
    bounds = wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(bounds))
    width, height = bounds.right - bounds.left, bounds.bottom - bounds.top
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user.PrintWindow.restype = wintypes.BOOL
    screen = user.GetDC(hwnd)
    memory = gdi.CreateCompatibleDC(screen)
    bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
    previous = gdi.SelectObject(memory, bitmap)
    try:
        assert user.PrintWindow(hwnd, memory, 2), "Unable to print QA window"
        gdi.SelectObject(memory, previous)
        info = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        data = ctypes.create_string_buffer(width * height * 4)
        assert gdi.GetDIBits(memory, bitmap, 0, height, data, info, 0) == height
        save_png(target, (0, 0, width, height), data.raw)
    finally:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, screen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="after")
    parser.add_argument("--themes", nargs="+", default=["modern", "win7_aero", "paper", "frutiger"])
    parser.add_argument("--dpi", nargs="+", type=int, default=[96, 192])
    options = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "build" / "final-ui-qa" / options.phase
    output.mkdir(parents=True, exist_ok=True)
    native_signatures()
    results = []
    with tempfile.TemporaryDirectory(prefix="calendar-final-ui-") as directory:
        store = Store(Path(directory) / "data.json")
        today = fixture(store)
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            actual = app.dpi.dpi
            try:
                for dpi in options.dpi:
                    with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                        app._apply_dpi_change(dpi)
                        for theme in options.themes:
                            app.set_theme(theme)
                            app.select_day(today)
                            app.enter_global_view()
                            area = app.dpi.work_area()
                            width = min(1260, app.dpi.logical(area.width) - 40)
                            height = min(760, app.dpi.logical(area.height) - 50)
                            app.geometry(scaled_geometry(width, height, area.left + 20, area.top + 20, dpi))
                            pump(app, .25)
                            assert_header(app)
                            for mode in ("flow", "timeline"):
                                app.set_global_display_mode(mode)
                                app.select_timeline_item("design")
                                app._scroll_search_target_into_view("design")
                                pump(app, .15)
                                capture(app, output / f"{theme}-{dpi}-{mode}.png")
                                if mode == "flow":
                                    assert_flow_text(app)
                                if dpi == actual:
                                    capture_drag_preview(app, output / f"{theme}-{dpi}-{mode}-drag.png")
                            app.open_search()
                            palette = app.search_window
                            palette.query.set("项目")
                            deadline = time.monotonic() + 5
                            while palette._pending and time.monotonic() < deadline:
                                pump(app, .02)
                            assert palette._results
                            capture(palette, output / f"{theme}-{dpi}-search.png")
                            palette.move(1)
                            palette.move(-1)
                            palette.close()
                            pump(app)
                            app.return_to_compact_view()
                            pump(app, .15)
                            capture(app, output / f"{theme}-{dpi}-compact.png")
                            app.open_day_detail(today)
                            pump(app)
                            assert app.grab_current() is app.day_detail_window
                            app.day_detail_window.close()
                            pump(app)
                            app.enter_global_view()
                            for width_check in (720, 1000, width):
                                app.geometry(scaled_geometry(width_check, height, area.left + 20, area.top + 20, dpi))
                                pump(app, .06)
                                assert_header(app)
                                expected = GLOBAL_TIMELINE_LAYOUT.show_detail_panel(app.dpi.logical(app.global_workspace.winfo_width()))
                                assert bool(app.global_detail_frame.winfo_manager()) == expected, (theme, dpi, width_check)
                            app.open_new_event(today)
                            pump(app)
                            assert app.grab_current() is app.editor_window
                            assert str(app.editor_window.transient()) == str(app)
                            app.editor_window.close()
                            pump(app)
                            for cycle in range(10 if dpi == actual else 1):
                                app.open_category_editor()
                                pump(app)
                                editor = app.category_editor
                                assert app.grab_current() is editor
                                editor.name_entry.insert(0, f"输入检查 {cycle}")
                                assert editor.name_var.get() == f"输入检查 {cycle}"
                                editor.close()
                                pump(app)
                            assert app.grab_current() is None
                            # Leave a hidden inspector behind before the next theme rebuild.
                            app.geometry(scaled_geometry(720, height, area.left + 20, area.top + 20, dpi))
                            pump(app, .06)
                            results.append({"theme": theme, "dpi": dpi, "native_dpi": actual, "status": "passed"})
                            print(theme, dpi, "Flow / Timeline / Search / Compact captured", flush=True)
                assert not errors, errors
            finally:
                cleanup(app)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(output, flush=True)


def assert_flow_text(app):
    """Check rendered glyph extents, not estimates of font point sizes."""
    canvas = app.global_flow_canvas
    for week in app.calendar_flow_layout.weeks:
        for block in week.blocks:
            if not block.visible:
                continue
            tag = f"flow-card-style:{block.item.id}:{week.index}:{block.start_column}:{block.lane}"
            identifiers = canvas.find_withtag(tag)
            if not identifiers:
                continue
            x1, y1, x2, y2 = canvas.bbox(identifiers[0])
            texts = []
            for identifier in canvas.find_withtag(f"timeline-item:{block.item.id}"):
                if canvas.type(identifier) != "text":
                    continue
                x, y = canvas.coords(identifier)
                if x1 <= x <= x2 and y1 <= y <= y2:
                    texts.append(canvas.bbox(identifier))
            texts.sort(key=lambda box: box[1])
            if len(texts) == 2:
                assert texts[0][3] <= texts[1][1] + 1, (app.theme_name, app.dpi.dpi, texts)


def capture_drag_preview(app, target):
    canvas = app.global_flow_canvas if app.global_display_mode == "flow" else app.global_timeline_canvas
    hit = next(hit for hit in app._schedule_hits if hit.event_id == "design")
    x, y = position_hit(app, canvas, hit)
    canvas.event_generate("<ButtonPress-1>", x=x, y=y)
    app.update()
    width = app._global_flow_column_width if app.global_display_mode == "flow" else app._global_day_width
    canvas.event_generate("<B1-Motion>", x=x + width, y=y)
    pump(app)
    assert app._schedule_drag is not None and app._schedule_drag.active
    capture(app, target)
    app._cancel_schedule_drag()
    assert app.grab_current() is None


if __name__ == "__main__":
    main()
