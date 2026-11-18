"""Bounded real RC Schema8 startup; known consent dialogs only, no file picker."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.request
import sqlite3
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.generate_schema8_fixture import generate
from tools.check_sqlite_rc_exe import wait,close
from desktop_calendar.assistant.llm_provider import AIConfiguration

def main():
    parser=argparse.ArgumentParser();parser.add_argument('exe',type=Path);parser.add_argument('report',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='calendar-schema8-exe-') as folder:
        profile=Path(folder)/'DesktopCalendar';profile.mkdir()
        raw=generate()['document'];raw['settings']['theme']='modern'
        source=profile/'calendar_data.json';source.write_text(json.dumps(raw),encoding='utf8');original=source.read_bytes()
        config=AIConfiguration(profile/'ai-assistant')
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        config.save({**config.load(),'http_enabled':True,'port':port})
        def healthy():
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/health',timeout=.5) as r:return r.status==200
            except OSError:return False
        exe=args.exe.resolve();child=subprocess.Popen([str(exe),'--upgrade-storage'],cwd=exe.parent,env={**os.environ,'APPDATA':folder})
        try:
            wait(child,healthy)
            state=json.loads((profile/'storage-state.json').read_text(encoding='utf8'))
            assert state['source_schema']==8 and state['state']=='ACTIVE'
            assert source.read_bytes()==original==(profile/state['protection']).read_bytes()
            with closing(sqlite3.connect((profile/state['database']).as_uri()+'?mode=ro',uri=True)) as db:
                counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('events','routines','categories')}
                assert counts==dict(events=6,routines=2,categories=1)
            close(child)
            result=dict(source_schema=8,sqlite_schema=1,state='ACTIVE',counts=counts,source_and_protection_identical=True,normal_tray_exit=True)
            args.report.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result),flush=True)
        finally:
            if child.poll() is None:
                try:close(child)
                except Exception:child.terminate();child.wait(5)

if __name__=='__main__':main()
