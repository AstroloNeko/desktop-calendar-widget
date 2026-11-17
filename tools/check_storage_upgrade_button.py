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
    else:
        run(True);run(False)
