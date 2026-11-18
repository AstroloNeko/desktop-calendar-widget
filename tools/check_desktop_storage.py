"""Real Tk C1 business smoke, synthetic v0.10.2 profile only.

Uses the same factory as app.main. Native migration consent is programmatically
answered in this test; this is not a claim of manual user acceptance.
"""
import json
from datetime import date
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from desktop_storage_startup import StartupOptions, create_desktop_app
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from tools.check_interactions import pump, cleanup


def check_wizard():
    """Exercise app.main's first migration, auto-answering owned native dialogs."""
    import app as entry
    fixture = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
    with tempfile.TemporaryDirectory(prefix='calendar-c1-wizard-') as folder:
        root = Path(folder)
        (root/'calendar_data.json').write_text(json.dumps(fixture), encoding='utf8')
        seen = []
        def consent(*args, parent, **kwargs):
            assert parent.winfo_exists()
            seen.append('consent')
            return True
        def activated(*args, parent, **kwargs):
            assert parent.winfo_exists() and parent.winfo_ismapped()
            seen.append('activated')
        def loop(app):
            try:
                assert isinstance(app.store.repository, SqliteCalendarRepository)
                pump(app)
                seen.append('desktop')
            finally:
                cleanup(app)
        with patch('storage_startup_ui.messagebox.askyesno', side_effect=consent), \
                patch('storage_startup_ui.messagebox.showinfo', side_effect=activated), \
                patch.object(CalendarApp, 'mainloop', loop), \
                patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            entry.main(['--isolated-data-dir', str(root), '--offer-storage-migration'])
        assert seen == ['consent', 'activated', 'desktop'], seen
        print('app.main migration wizard ownership/progress/SQLite desktop passed (automated consent)', flush=True)


def main():
    check_wizard()
    fixture = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
    with tempfile.TemporaryDirectory(prefix='calendar-c1-gui-') as folder:
        root = Path(folder)
        source = root/'calendar_data.json'
        source.write_text(json.dumps(fixture), encoding='utf8')
        original = source.read_bytes()
        options = StartupOptions(root, True)
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = create_desktop_app(options, CalendarApp, confirm=lambda: True)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            ids = []
            try:
                assert isinstance(app.store.repository, SqliteCalendarRepository)
                assert len(app.store.events) == len(fixture['events'])
                for theme in ('modern', 'aero', 'paper', 'frutiger'):
                    app.set_theme(theme)
                    for mode in ('compact', 'global'):
                        (app.enter_global_view if mode == 'global' else app.return_to_compact_view)()
                        app.select_day(date.today())
                        app.open_new_event()
                        pump(app)
                        editor = app.editor_window
                        assert app.grab_current() is editor
                        editor.title_var.set('C1 temporary '+theme+mode)
                        editor.save()
                        pump(app)
                        item = app.store.events[-1]
                        ids.append(item.id)
                        app.open_editor(item)
                        pump(app)
                        app.editor_window.title_var.set('C1 edited')
                        app.editor_window.save()
                        app.toggle_done(next(event for event in app.store.events if event.id == item.id))
                        app.undo_event()
                        app.redo_event()
                        app.open_routine_editor()
                        pump(app)
                        editor = app.routine_editor
                        editor.title_var.set('C1 habit')
                        editor.save_button.invoke()
                        pump(app)
                        habit = app.store.routines[-1]
                        app.toggle_routine(habit, date.today())
                        assert app.store.routines[-1].is_done_on(date.today())
                        app.open_category_editor()
                        pump(app)
                        app.category_editor.name_var.set('C1 '+theme+mode)
                        app.category_editor.save()
                        app._set_flow_range_status(date(2026,11,16), date(2026,11,20), 'leave')
                        pump(app)
                        assert app.grab_current() is None
                        assert source.read_bytes() == original
                        assert not errors, errors
                        print(theme, mode, 'SQLite Event/Habit/Category/date-state/Undo passed; DPI', app.dpi.dpi, flush=True)
                snapshot = app.store.repository.revision()
            finally:
                cleanup(app)
            assert app.storage_session.closed
            import app as entry
            checked = []
            def restart_loop(restarted):
                try:
                    pump(restarted)
                    assert isinstance(restarted.store.repository, SqliteCalendarRepository)
                    assert set(ids) <= {event.id for event in restarted.store.events}
                    revision = restarted.store.repository.revision()
                    assert revision.session == snapshot.session and revision.generation >= snapshot.generation
                    assert source.read_bytes() == original
                    checked.append(True)
                    print('app.main SQLite restart, source preservation and lease release passed', flush=True)
                finally:
                    cleanup(restarted)
            with patch.object(CalendarApp, 'mainloop', restart_loop):
                entry.main(['--isolated-data-dir', str(root)])
            assert checked == [True]


if __name__ == '__main__': main()
