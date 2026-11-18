"""Opt-in real Tk two-stage deletion smoke; temporary synthetic data only."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store, Event
from desktop_calendar.assistant.calendar_chat_ui import open_chat
from desktop_calendar.assistant.calendar_chat_service import OWNER
from tools.check_calendar_chat import wait
from tools.check_interactions import cleanup, pump


def main():
    with tempfile.TemporaryDirectory() as folder:
        store=Store(Path(folder)/'calendar.json')
        store.events=[Event(i,'WB实验',day+'T23:59',has_time=False) for i,day in
            [('A','2026-11-15'),('B','2026-11-15'),('C','2026-11-16'),('D','2026-11-17')]]
        store.save()
        with patch.object(CalendarApp,'_start_tray_icon'),patch.object(CalendarApp,'check_reminders'):
            app=CalendarApp(store)
            errors=[]
            app.report_callback_exception=lambda *parts: errors.append(str(parts))
            try:
                for theme in ('modern','aero','paper','frutiger'):
                    app.set_theme(theme)
                    for mode in ('compact','global'):
                        (app.enter_global_view if mode=='global' else app.return_to_compact_view)()
                        open_chat(app)
                        chat=app.chat_runtime.window
                        wait(app,lambda:not chat.busy,'chat initialization')
                        service=app.chat_runtime.service
                        session=service.get_session(chat.session_id)
                        session['clarified_query']=dict(start='2026-11-15',end='2026-11-17')
                        service.journal.put('session',chat.session_id,OWNER,session)
                        chat.display(service.send_message(chat.session_id,'把这几天所有的WB实验全删除了','batch-delete-'+theme+'-'+mode))
                        pump(app)
                        assert chat.confirm_button.cget('text')=='删除这4项'
                        assert all(('ID '+i) in chat.transcript.get('1.0','end') for i in 'ABCD')
                        chat.confirm_button.invoke()
                        wait(app,lambda:not chat.busy,'first review')
                        assert len(store.events)==4
                        assert chat.confirm_button.cget('text')=='最终确认删除'
                        chat.confirm_button.invoke()
                        wait(app,lambda:not chat.busy,'final deletion')
                        assert not store.events
                        chat.close()
                        assert app.grab_current() is None
                        app.undo_event()
                        assert [e.id for e in store.events]==list('ABCD')
                        print(theme,mode,'two-stage/delete/undo/modal passed; DPI',app.dpi.dpi,flush=True)
                assert not errors,errors
            finally:
                cleanup(app)


if __name__=='__main__':
    main()
