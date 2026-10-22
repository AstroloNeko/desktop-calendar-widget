"""Opt-in real Windows/Tk checks; synthetic notes/data, no installation or network.

Physical monitor DPI is reported separately from simulated layout DPI. Tk events
and control invocations validate widgets; this is not a hardware-mouse test.
"""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from dpi_utils import dpi_for_window, enable_dpi_awareness
from repair_launcher import RepairWindow, RepairNotesDialog
from tools.check_interactions import cleanup, pump
from tools.check_update_dialog import visible
from update_dialog import ReleaseHistoryDialog
from update_manager import UpdateInfo
from version import __version__
from win_integration import is_window_topmost


def document_checks(dialog, text, actions):
    assert dialog.winfo_ismapped()
    assert all(visible(action, dialog) for action in actions)
    assert text.splitlines()[-1] in dialog.notes.get("1.0", "end-1c")
    dialog.notes.yview_moveto(0)
    dialog.notes.event_generate("<MouseWheel>", delta=-120)
    pump(dialog)
    assert dialog.notes.yview()[0] > 0
    dialog.document.scrollbar.set(*dialog.notes.yview())
    dialog.notes.yview_moveto(1)
    pump(dialog)
    assert dialog.notes.yview()[1] == 1
    assert dialog.notes.bbox("end-1c linestart") is not None
    assert all(visible(action, dialog) for action in actions)


def main():
    enable_dpi_awareness()
    errors = []
    long_notes = "### 测试标题\n\n- **中文** English 123🙂\n1. `Ctrl+K`\n" * 120 + "FINAL END 最后一段"
    with tempfile.TemporaryDirectory(prefix="calendar-history-") as temporary:
        root = Path(temporary)
        changelog = root / "CHANGELOG.md"
        changelog.write_text(f"## v{__version__}\n{long_notes}\n## v0.1.0\n旧版本 OLD END", encoding="utf-8")
        with patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
            app = CalendarApp(Store(root / "calendar_data.json"))
            app.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
            try:
                pump(app, .2)
                native = dpi_for_window(app)
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.set_theme(theme)
                    for view in ("compact", "global"):
                        (app.enter_global_view if view == "global" else app.return_to_compact_view)()
                        for pinned in (False, True):
                            app.window_mode = "pinned" if pinned else "desktop"
                            app.desktop_session_active = True
                            app.apply_window_mode()
                            dialog = ReleaseHistoryDialog(app, __version__, changelog, "Microsoft YaHei UI")
                            pump(app)
                            assert app.grab_current() is dialog and str(dialog.transient()) == str(app)
                            document_checks(dialog, long_notes, [dialog.later_button])
                            dialog.version_selector.current(1)
                            dialog.version_selector.event_generate("<<ComboboxSelected>>")
                            pump(app)
                            assert "OLD END" in dialog.notes.get("1.0", "end-1c")
                            assert "FINAL END" not in dialog.notes.get("1.0", "end-1c")
                            dialog.close()
                            pump(app, .15)
                            assert app.grab_current() is None and app.modal_depth == 0
                            assert is_window_topmost(app) == pinned
                            probe = tk.Entry(app)
                            probe.place(x=10, y=10, width=180)
                            probe.focus_force()
                            pump(app)
                            probe.event_generate("<KeyPress-a>")
                            assert probe.get() == "a"
                            probe.event_generate("<Control-k>")
                            pump(app)
                            assert app.search_window.winfo_ismapped()
                            app.search_window.close()
                            pump(app, .15)
                            probe.destroy()
                    print(f"PASS {theme}: history Compact/Global, topmost on/off, switch/scroll/close/input/Ctrl+K", flush=True)
                for dpi in (96, 120, 144, 168, 192):
                    with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                        app._apply_dpi_change(dpi)
                        dialog = ReleaseHistoryDialog(app, __version__, changelog, "Microsoft YaHei UI")
                        pump(app)
                        document_checks(dialog, long_notes, [dialog.later_button])
                        dialog.close()
                        pump(app, .15)
                # Exercise the actual More-menu command including deferred unposting.
                def popup(menu, *_args):
                    for index in range(menu.index("end") + 1):
                        if menu.type(index) == "command" and menu.entrycget(index, "label") == "版本与更新日志…":
                            menu.invoke(index)
                            return
                    raise AssertionError("history menu entry missing")
                with patch.object(tk.Menu, "tk_popup", popup):
                    app.show_main_menu()
                pump(app)
                dialogs = [w for w in app.winfo_children() if isinstance(w, ReleaseHistoryDialog)]
                assert len(dialogs) == 1
                dialogs[0].close()
                pump(app)
                assert not errors, errors
                print(f"Native monitor DPI {native}; 96/120/144/168/192 simulated layouts PASS", flush=True)
            finally:
                cleanup(app)
        # A fresh, independent Tk root: importing/using Repair does not load Store.
        repair = RepairWindow()
        repair.report_callback_exception = lambda *parts: errors.append("".join(traceback.format_exception(*parts)))
        try:
            pump(repair)
            for local in ("0.9.1", "99.0.0", None):
                for notes in ("", long_notes):
                    target = UpdateInfo("v99.0.0", "99.0.0", "TARGET RELEASE", notes, "", "", "")
                    dialog = RepairNotesDialog(repair, target, local)
                    pump(repair)
                    assert repair.grab_current() is dialog and str(dialog.transient()) == str(repair)
                    if notes:
                        document_checks(dialog, notes, [dialog.cancel_button, dialog.confirm_button])
                    else:
                        assert "没有附加说明" in dialog.notes.get("1.0", "end-1c")
                    dialog.cancel_button.invoke()
                    pump(repair)
                    assert not dialog.accepted and repair.grab_current() is None
            assert not errors, errors
            print("Independent Repair: newer/equal/unknown, empty/long notes, fixed buttons, cancel/grab PASS", flush=True)
        finally:
            repair.destroy()


if __name__ == "__main__":
    main()
