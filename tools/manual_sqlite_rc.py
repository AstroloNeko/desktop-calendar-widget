"""Prepare/inspect one persistent, synthetic packaged-RC manual acceptance.

No clicks, keys, retries, timers or automatic window closing. Only the launched
RC inherits isolated APPDATA. Files stay under the explicit project build path.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import sqlite3
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from calendar_storage_lifecycle import StorageLifecycle
from calendar_sqlite_backup import read_container
from tools.generate_schema8_fixture import generate

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','inspect','launch']);parser.add_argument('candidate',type=Path);parser.add_argument('--session',type=Path)
    args=parser.parse_args();candidate=args.candidate.resolve()
    if args.action=='prepare':
        session=ROOT/'build'/('rc-manual-'+uuid.uuid4().hex[:8]);session.mkdir()
        profile=session/'roaming/DesktopCalendar';profile.mkdir(parents=True)
        document=generate()['document'];document['settings']['theme']='modern'
        (profile/'calendar_data.json').write_text(json.dumps(document),encoding='utf8')
        manager=StorageLifecycle(profile,isolated=True);state=manager.migrate()
        backup=session/'recovery.dcalbackup';manager.backup(backup)
        damaged=profile/state['database'];damaged.write_bytes(b'Isolated RC manual recovery damage')
        Path(str(damaged)+'-wal').write_bytes(b'Isolated WAL evidence')
        Path(str(damaged)+'-shm').write_bytes(b'Isolated SHM evidence')
        executable=candidate/'src/dist/DesktopCalendar/DesktopCalendar.exe'
        metadata=dict(session=str(session),profile=str(profile),backup=str(backup),executable=str(executable),damaged=str(damaged),original_counts={k:len(document[k]) for k in ('events','routines','categories')})
        (session/'session.json').write_text(json.dumps(metadata,indent=2),encoding='utf8')
        process=subprocess.Popen([str(executable),'--recover-storage'],env={**os.environ,'APPDATA':str(profile.parent)},cwd=executable.parent)
        print(json.dumps({**metadata,'pid':process.pid},ensure_ascii=True),flush=True)
    else:
        metadata=json.loads((args.session/'session.json').read_text(encoding='utf8'));profile=Path(metadata['profile'])
        if args.action=='launch':
            exe=Path(metadata['executable'])
            process=subprocess.Popen([str(exe)],env={**os.environ,'APPDATA':str(profile.parent)},cwd=exe.parent)
            print(process.pid);return
        manager=StorageLifecycle(profile,isolated=True);state=manager._read()
        output=dict(state=state['state'],operation=state.get('operation'),damaged_original_retained=Path(metadata['damaged']).read_bytes()==b'Isolated RC manual recovery damage')
        if state['state']=='ACTIVE':
            with closing(sqlite3.connect((profile/state['database']).as_uri()+'?mode=ro',uri=True)) as db:
                output['counts']={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('events','routines','categories')}
                output['manual_events']=[dict(id=r[0],title=r[1],done=r[2]) for r in db.execute("SELECT id,title,done FROM events WHERE title LIKE 'RC验收%'")]
            output['backups']=[dict(path=str(p),format=read_container(p)[0]['format_version']) for p in args.session.glob('*.dcalbackup')]
        print(json.dumps(output,ensure_ascii=True),flush=True)

if __name__=='__main__':main()
