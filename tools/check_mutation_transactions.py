"""Opt-in real Tk A3 regression. Only synthetic, temporary user data."""
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from tools.check_interactions import cleanup, pump


def main():
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'calendar.json')
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                for theme in ('modern', 'aero', 'paper', 'frutiger'):
                    app.set_theme(theme)
                    for mode in ('compact', 'global'):
                        (app.enter_global_view if mode == 'global' else app.return_to_compact_view)()
                        day = date.today()
                        app.select_day(day)
                        app.open_routine_editor()
                        pump(app)
                        editor = app.routine_editor
                        assert app.grab_current() is editor
                        editor.title_var.set('A3 temporary habit')
                        editor.save_button.invoke()
                        pump(app)
                        habit = store.routines[-1]
                        app.toggle_routine(habit, day)
                        assert store.routines[-1].is_done_on(day)
                        app.open_routine_editor(store.routines[-1])
                        pump(app)
                        editor = app.routine_editor
                        editor.title_var.set('A3 renamed')
                        editor.save_button.invoke()
                        app.undo_event()
                        assert store.routines[-1].title == 'A3 temporary habit'
                        assert store.routines[-1].is_done_on(day)
                        app.redo_event()
                        app.open_routine_editor(store.routines[-1])
                        pump(app)
                        with patch('app.messagebox.askyesno', return_value=True):
                            app.routine_editor.delete()
                        assert not store.routines
                        app.open_category_editor()
                        pump(app)
                        editor = app.category_editor
                        editor.name_var.set('A3 category')
                        editor.save()
                        category = store.categories[-1]
                        app.open_category_editor(category)
                        pump(app)
                        app.category_editor.name_var.set('A3 renamed category')
                        app.category_editor.save()
                        app.open_category_manager()
                        pump(app)
                        with patch('app.messagebox.askyesno', return_value=True):
                            app.category_manager._delete(store.categories[-1])
                        assert not store.categories
                        app.category_manager.close()
                        with patch.object(store, 'save', wraps=store.save) as save:
                            app._set_flow_range_status(date(2026,11,16), date(2026,11,20), 'leave')
                            assert save.call_count == 1
                        app._set_flow_range_status(date(2026,11,16), date(2026,11,20), 'normal')
                        pump(app)
                        assert app.grab_current() is None
                        assert not errors, errors
                        print(theme, mode, 'habit/category/date range/undo/modal passed; DPI', app.dpi.dpi, flush=True)
            finally:
                cleanup(app)


if __name__ == '__main__':
    main()
