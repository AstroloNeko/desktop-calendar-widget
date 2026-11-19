"""One Modern real-Tk upgrade-button/cancel smoke; isolated profiles only."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as entry
from tools.check_interactions import pump, cleanup

def run(approve):
    with tempfile.TemporaryDirectory(prefix='calendar-upgrade-button-') as folder:
        root=Path(folder)
        fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
        fixture['settings']['theme']='modern'
        (root/'calendar_data.json').write_text(json.dumps(fixture),encoding='utf8')
        owners=[];saved=[];errors=[]
        def loop(app):
            app.report_callback_exception=lambda *a:errors.append(str(a))
            pump(app,.1)
            if not owners:
                owners.append(app)
                app.storage_upgrade_button.invoke()
                assert app.lifecycle.stopped and app.storage_session.closed
            else:
                assert app.storage_session.sqlite == approve
                assert owners[0].store.repository is not app.store.repository
                if approve:
                    app.open_new_event();pump(app)
                    app.editor_window.title_var.set('Isolated button upgrade persisted')
                    app.editor_window.save();pump(app)
                    saved.append(app.store.events[-1].id)
                else:
                    assert {e.id for e in app.store.events}=={e['id'] for e in fixture['events']}
                cleanup(app)
        with patch.object(entry.CalendarApp,'mainloop',loop),patch.object(entry.CalendarApp,'_start_tray_icon'),patch.object(entry.CalendarApp,'check_reminders'),patch('storage_startup_ui.messagebox.askyesno',return_value=approve),patch('storage_startup_ui.messagebox.showinfo'),patch('app.messagebox.showerror',side_effect=lambda *a,**k: (_ for _ in ()).throw(AssertionError(str(a)))):
            entry.main(['--isolated-data-dir',str(root)])
        assert not errors,errors
        def restart(app):
            if approve: assert saved[0] in {e.id for e in app.store.events}
            else: assert not app.storage_session.sqlite
            cleanup(app)
        with patch.object(entry.CalendarApp,'mainloop',restart),patch.object(entry.CalendarApp,'_start_tray_icon'),patch.object(entry.CalendarApp,'check_reminders'):
            entry.main(['--isolated-data-dir',str(root)])
        print('Modern desktop button, consent='+str(approve)+', persistence/restart passed',flush=True)


def policy_smoke():
    """Test the production Tk entry with synthetic profiles and no remote calls."""
    from contextlib import ExitStack
    from desktop_preferences import sqlite_upgrade_declined
    fixture = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json')
                         .read_text(encoding='utf8'))['document']
    fixture['settings']['theme'] = 'modern'
    with tempfile.TemporaryDirectory(prefix='calendar-storage-policy-') as folder, ExitStack() as stack:
        root = Path(folder)
        legacy, fresh = root/'legacy', root/'fresh'
        legacy.mkdir()
        source = legacy/'calendar_data.json'
        source.write_text(json.dumps(fixture), encoding='utf8')
        external = root/'import.json'
        original = source.read_bytes()
        external.write_bytes(original)
        stack.enter_context(patch.object(entry.CalendarApp, '_start_tray_icon'))
        stack.enter_context(patch.object(entry.CalendarApp, 'check_reminders'))
        stack.enter_context(patch('storage_startup_ui.messagebox.askyesno', return_value=True))
        stack.enter_context(patch('storage_startup_ui.messagebox.showinfo'))
        stack.enter_context(patch('backup_ui.messagebox.showerror', side_effect=AssertionError('unexpected storage error')))

        def launch(profile, loop):
            def run_loop(app):
                app.report_callback_exception = lambda *_: (_ for _ in ()).throw(AssertionError('Tk callback failed'))
                pump(app, .15)
                assert app.theme.name == 'modern'
                loop(app)
            with patch.object(entry.CalendarApp, 'mainloop', run_loop):
                entry.main(['--isolated-data-dir', str(profile)])

        opened = []
        def fresh_import(app):
            assert app.storage_session.sqlite and not app.storage_banner_visible()
            assert not (fresh/'calendar_data.json').exists()
            opened.append(app.storage_session.state['operation'])
            if len(opened) == 1:
                assert not app.store.events
                with (patch('backup_ui.filedialog.askopenfilename', return_value=str(external)),
                      patch('backup_ui.messagebox.askyesno', side_effect=[False, True])):
                    app.import_legacy_calendar()
                    assert not app.lifecycle.stopped
                    assert app.storage_session.state['operation'] == 'initialize'
                    app.import_legacy_calendar()
                    assert app.storage_session.closed
            else:
                assert {e.id for e in app.store.events} == {e['id'] for e in fixture['events']}
                cleanup(app)
        launch(fresh, fresh_import)
        assert opened == ['initialize', 'import_json'] and external.read_bytes() == original

        def decline(app):
            assert not app.storage_session.sqlite and app.storage_banner_visible()
            assert app.storage_decline_button.winfo_viewable()
            app.storage_decline_button.invoke()
            assert not app.storage_banner_visible() and sqlite_upgrade_declined(legacy)
            cleanup(app)
        launch(legacy, decline)

        modes = []
        def manual_upgrade(app):
            modes.append(app.storage_session.sqlite)
            assert not app.storage_banner_visible()
            if not app.storage_session.sqlite:
                # Same callback as the permanent Data Management entry.
                app.request_storage_upgrade()
                assert app.storage_session.closed
            else:
                state = app.storage_session.state
                assert source.read_bytes() == (legacy/state['protection']).read_bytes()
                cleanup(app)
        launch(legacy, manual_upgrade)
        assert modes == [False, True]
        print('Modern: native SQLite, cancelled/confirmed legacy import, permanent decline and manual upgrade passed',
              flush=True)

if __name__=='__main__':
    if '--manual-exe' in sys.argv:
        import os, subprocess, uuid
        folder=Path(__file__).resolve().parents[1]/'build'/('final-button-'+uuid.uuid4().hex[:8])
        profile=folder/'DesktopCalendar';profile.mkdir(parents=True)
        fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
        fixture['settings']['theme']='modern'
        (profile/'calendar_data.json').write_text(json.dumps(fixture),encoding='utf8')
        exe=Path(__file__).resolve().parents[1]/'dist/DesktopCalendar/DesktopCalendar.exe'
        p=subprocess.Popen([str(exe)],cwd=exe.parent,env={**os.environ,'APPDATA':str(folder)})
        print(str(profile),p.pid,flush=True)
    elif '--policy' in sys.argv:
        policy_smoke()
    else:
        run(True);run(False)
