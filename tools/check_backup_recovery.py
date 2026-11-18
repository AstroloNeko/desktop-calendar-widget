"""C2 single-theme real Tk backup/restart/recovery; temporary data only.

Native file/confirmation dialogs are auto-answered. Not manual acceptance.
No model requests, production profile, notifications or EXE installation.
"""
import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import socket
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as entry
from desktop_calendar.storage.calendar_storage_lifecycle import StorageLifecycle
from calendar_event_service import CalendarEventService
from desktop_calendar.storage.calendar_sqlite_backup import read_container
from tools.check_interactions import pump, cleanup


def main():
    fixture = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/schema8_v0102.json').read_text(encoding='utf8'))['document']
    with tempfile.TemporaryDirectory(prefix='calendar-c2-gui-') as folder:
        root = Path(folder)
        source = root/'calendar_data.json'
        source.write_text(json.dumps(fixture), encoding='utf8')
        original = source.read_bytes()
        manager = StorageLifecycle(root, isolated=True)
        manager.migrate()
        from desktop_calendar.assistant.llm_provider import AIConfiguration
        config = AIConfiguration(root/'ai-assistant')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        config.save({**config.load(), 'http_enabled': True, 'port': port})
        original_token = config.load()['token']
        target = root/'gui.dcalbackup'
        seen, errors, added = [], [], []
        old_owners = []
        def loop(app):
            app.set_theme('modern')
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            pump(app, .7)
            assert app.dpi.dpi == 192, app.dpi.dpi
            assert app.chat_runtime.core.repository is app.store.repository
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/health', timeout=2) as response:
                assert response.status == 200
            if not seen:
                app.export_backup()
                assert read_container(target)[2] == 'sqlite'
                added.extend(CalendarEventService(app.event_commands).create_quick('C2 after backup', date.today()).event_ids)
                old_owners.append(app)
                seen.append('backup')
                app.restore_data_backup()
                assert app.lifecycle.stopped and app.storage_session.closed
                assert app.chat_runtime.service.stopped and app.chat_runtime.http is None
            else:
                assert not set(added) & {event.id for event in app.store.events}
                assert app.event_commands.undo_count == 0
                assert app.reminder_engine.store is app.store
                assert old_owners[0].store is not app.store
                assert old_owners[0].chat_runtime.journal.path != app.chat_runtime.journal.path
                assert config.load()['token'] == original_token
                assert app.grab_current() is None
                seen.append('restored')
                cleanup(app)
        with patch.object(entry.CalendarApp, '_start_tray_icon'), \
                patch.object(entry.CalendarApp, 'check_reminders'), \
                patch('tkinter.filedialog.asksaveasfilename', return_value=str(target)), \
                patch('tkinter.filedialog.askopenfilename', return_value=str(target)), \
                patch('tkinter.messagebox.askyesno', return_value=True), \
                patch('tkinter.messagebox.showinfo'), \
                patch('tkinter.messagebox.showerror', side_effect=lambda *a, **k: errors.append(str(a))):
            with patch.object(entry.CalendarApp, 'mainloop', loop):
                entry.main(['--isolated-data-dir', str(root)])
            assert seen == ['backup','restored'], (seen,errors)
            assert not errors, errors
            print('Modern 192 DPI: Format 2 UI backup, restore, fresh Store/history/reminder owner, modal/close; HTTP shutdown/rebind and journal epoch passed (no model requests)', flush=True)
            state = manager._read()
            damaged = root/state['database']
            damaged.write_bytes(b'isolated damaged database')
            def recovered(app):
                pump(app, .1)
                assert app.store.events
                assert app.storage_session.state['operation'] == 'disaster_restore'
                assert app.event_commands.undo_count == 0
                cleanup(app)
                seen.append('offline')
            with patch.object(entry.CalendarApp, 'mainloop', recovered):
                entry.main(['--isolated-data-dir', str(root), '--recover-storage'])
            assert seen[-1] == 'offline' and not errors, errors
            assert damaged.read_bytes() == b'isolated damaged database'
            assert source.read_bytes() == original
            print('Offline recovery UI: damaged authority preserved, valid backup activated, desktop reopened; source JSON unchanged', flush=True)


if __name__ == '__main__': main()
