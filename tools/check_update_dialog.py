"""Opt-in Windows/Tk smoke using synthetic release notes and isolated user data.

No network, downloads or installation. Native monitor DPI is reported separately
from simulated 96..192 DPI layouts. Run: py -3 tools/check_update_dialog.py
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import CalendarApp
from calendar_core import Store
from dpi_utils import WorkArea, dpi_for_window
from tools.check_interactions import cleanup, pump
from tools.check_frameless_global import capture
from update_dialog import UpdateAvailableDialog
from update_manager import UpdateInfo
from version import __version__
from win_integration import is_window_topmost


def visible(widget, window):
    return (widget.winfo_ismapped() and widget.winfo_height() > 10
            and widget.winfo_rooty() >= window.winfo_rooty()
            and widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-only", action="store_true", help="Only the current physical monitor DPI")
    parser.add_argument("--screenshots", action="store_true", help="Save native-DPI samples in ignored build/")
    options = parser.parse_args()
    errors = []
    cases = []
    with tempfile.TemporaryDirectory(prefix="calendar-update-dialog-") as temporary:
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(Store(Path(temporary) / "data.json"))
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .2)
                native_dpi = dpi_for_window(app)
                long_notes = ("## 本轮更新\n\n- **中文** English 123 🙂\n1. `Ctrl+K` 搜索\n\n" * 100
                              + "最后一段 FINAL RELEASE NOTE END")
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for mode in ("compact", "global"):
                        if mode == "global":
                            app.enter_global_view()
                        else:
                            app.return_to_compact_view()
                        for dpi in ((native_dpi,) if options.native_only else (96, 120, 144, 168, 192)):
                            with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                                app._apply_dpi_change(dpi)
                                for pinned in (False, True):
                                    app.window_mode = "pinned" if pinned else "desktop"
                                    app.desktop_session_active = True
                                    app.apply_window_mode()
                                    info = UpdateInfo("v99.0.0", "99.0.0", "模拟更新 / Synthetic release", long_notes, "", "", "")
                                    dialog = UpdateAvailableDialog(app, info, __version__, "Microsoft YaHei UI")
                                    pump(app)
                                    assert dialog.winfo_ismapped()
                                    assert str(dialog.transient()) == str(app), (dialog.transient(), str(app))
                                    assert app.grab_current() is dialog
                                    assert app.focus_get().winfo_toplevel() is dialog
                                    assert long_notes.splitlines()[-1] in dialog.notes.get("1.0", "end-1c")
                                    assert "## 本轮" not in dialog.notes.get("1.0", "end-1c")
                                    assert visible(dialog.later_button, dialog) and visible(dialog.update_button, dialog)
                                    area = app.dpi.work_area()
                                    assert dialog.winfo_height() <= area.height and dialog.winfo_width() <= area.width
                                    dialog.notes.yview_moveto(0)
                                    dialog.notes.event_generate("<MouseWheel>", delta=-120)
                                    pump(app)
                                    assert dialog.notes.yview()[0] > 0, "wheel did not scroll"
                                    dialog.notes.yview_moveto(0)
                                    pump(app)
                                    bar = dialog.scrollbar
                                    x = bar.winfo_width() // 2
                                    thumb = [y for y in range(bar.winfo_height())
                                             if bar.identify(x, y).split(".")[-1] == "thumb"]
                                    assert thumb, ("no draggable scrollbar thumb", bar.winfo_width(), bar.winfo_height(),
                                                   bar.get(), {bar.identify(x, y) for y in range(bar.winfo_height())})
                                    y = thumb[len(thumb) // 2]
                                    bar.event_generate("<ButtonPress-1>", x=x, y=y)
                                    bar.event_generate("<B1-Motion>", x=x, y=bar.winfo_height() - 20)
                                    bar.event_generate("<ButtonRelease-1>", x=x, y=bar.winfo_height() - 20)
                                    pump(app)
                                    assert dialog.notes.yview()[0] > 0, "scrollbar drag did not scroll"
                                    dialog.notes.yview_moveto(1)
                                    pump(app)
                                    assert dialog.notes.yview()[1] == 1.0
                                    last_line = dialog.notes.index("end-1c linestart")
                                    assert dialog.notes.bbox(last_line) is not None, "last paragraph not visible"
                                    assert visible(dialog.later_button, dialog) and visible(dialog.update_button, dialog)
                                    if options.screenshots and dpi == native_dpi and not pinned and mode == "global":
                                        output = Path(__file__).resolve().parents[1] / "build" / "update-dialog-smoke"
                                        output.mkdir(parents=True, exist_ok=True)
                                        dialog.notes.yview_moveto(0)
                                        pump(app)
                                        capture(dialog, output / f"{theme}.png")
                                    dialog.later_button.invoke()
                                    pump(app, .15)
                                    assert not dialog.winfo_exists() and not dialog.accepted
                                    assert app.grab_current() is None and app.modal_depth == 0
                                    assert is_window_topmost(app) == pinned, "saved topmost mode not restored"
                                    # Actual Tk event dispatch, not a callback invocation.
                                    probe = tk.Entry(app)
                                    probe.place(x=15, y=20, width=160)
                                    probe.focus_force()
                                    pump(app)
                                    probe.event_generate("<KeyPress-a>")
                                    assert probe.get() == "a", "input after closing modal failed"
                                    if dpi == native_dpi:
                                        probe.event_generate("<Control-k>")
                                        pump(app)
                                        assert app.search_window is not None and app.search_window.winfo_ismapped(), "Ctrl+K after close failed"
                                        app.search_window.close()
                                        pump(app, .15)
                                        assert app.grab_current() is None
                                    probe.destroy()
                                    cases.append({"theme": theme, "view": mode, "dpi": dpi, "pinned": pinned})
                    print(f"PASS {theme}: Compact/Global, DPI {'native' if options.native_only else '96..192'}, topmost on/off", flush=True)
                # Empty, short and medium documents; closing and accepting use the
                # same modal path without ever starting a real update.
                for notes in ("", "字" * 50, "字" * 500, long_notes):
                    dialog = UpdateAvailableDialog(app, UpdateInfo("v99", "99", "", notes, "", "", ""), __version__, "Microsoft YaHei UI")
                    pump(app)
                    assert visible(dialog.later_button, dialog) and visible(dialog.update_button, dialog)
                    dialog.update_button.invoke()
                    pump(app, .15)
                    assert dialog.accepted and app.grab_current() is None
                # Small available work area at native DPI: actions retain space
                # even though the reading viewport has to shrink.
                for area in (WorkArea(0, 0, 800, 560), WorkArea(0, 0, 1024, 700)):
                    with patch.object(app.dpi, "work_area", return_value=area):
                        dialog = UpdateAvailableDialog(app, info, __version__, "Microsoft YaHei UI")
                        pump(app)
                        assert visible(dialog.later_button, dialog) and visible(dialog.update_button, dialog)
                        assert dialog.notes.winfo_height() > 40
                        dialog.close()
                        pump(app, .15)
                assert not errors, errors
                print(json.dumps({"native_monitor_dpi": native_dpi, "layout_cases": len(cases),
                                  "content_cases": 4, "callback_errors": errors}, ensure_ascii=False), flush=True)
            finally:
                cleanup(app)


if __name__ == "__main__":
    main()
