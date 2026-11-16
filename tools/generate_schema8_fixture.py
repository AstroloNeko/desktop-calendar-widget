"""Generate synthetic data through the actual tagged v0.10.2 Store.save.

No checkout/worktree or real user data. Git archive is extracted to TemporaryDirectory.
The output includes an independent old-version semantic oracle.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
REV='cb0c62b204c553f61cb31b83955e1191f675c2e2'
SCRIPT=r'''
import json
from pathlib import Path
from datetime import date,timedelta
from calendar_core import Store,Event,EventCategory,RoutineItem
s=Store(Path('isolated.json'))
s.categories=[EventCategory('old-cat','Synthetic','#52B788',created_at='2026-01-01T10:00')]
s.events=[Event('old-'+str(i),'Synthetic duplicate','2026-01-02T23:59',created_at='2025-01-01T10:00',
    has_time=False,duration_days=5,skip_non_working_days=True,end_as_ddl=True) for i in range(2)]
s.events += [Event('timed','Synthetic timed','2026-01-05T09:00',created_at='2025-01-01T10:00',reminder=30),
 Event('ddl','Synthetic DDL','2026-01-07T10:00',created_at='2025-01-01T10:00',event_type='ddl'),
 Event('done','Synthetic done','2026-01-08T10:00',created_at='2025-01-01T10:00',done=True),
 Event('inherit','Synthetic inherit','2026-01-09T10:00',created_at='2025-01-01T10:00',category_id='old-cat',color_mode='inherit',color='#52B788')]
s.routines=[RoutineItem('habit','Synthetic habit',created_on='2025-01-01',frequency='weekly',weekdays=[0,2,4],
 workdays_only=False,habit_done=['2025-01-03','2025-02-03'],reminder_enabled=True,reminder_time='09:00'),
 RoutineItem('todo','Synthetic todo',kind='todo',created_on='2025-01-01',completed_on='2025-02-01')]
s.date_states={'2026-01-05':'leave','2026-01-06':'holiday'}
s.settings.update(theme='paper',unknown_old={'retain':True},global_geometry='1200x800+10+10')
s.notified={'old-0:delivery'}
s.save()
days=[date(2024,12,30)+timedelta(days=i) for i in range(800)]
oracle=dict(events=[dict(id=e.id,dates=[d.isoformat() for d in s.event_dates(e)],
 end=s.event_ends_at(e).isoformat(),color=s.effective_event_color(e)) for e in s.events],
 habits=[dict(id=r.id,planned=[d.isoformat() for d in days if s.routine_planned_on(r,d)]) for r in s.routines])
print(json.dumps(dict(document=json.loads(Path('isolated.json').read_text(encoding='utf8')),oracle=oracle)))
'''

DEFAULT_SCRIPT=r'''
from dataclasses import asdict
raw=dict(version=8,events=[dict(id='old',title='Synthetic',due='2026-01-01T10:00',created_at='2025-01-01T10:00',priority='normal')],
 categories=[dict(id='cat',name='Synthetic',created_at='2025-01-01T10:00')],
 routines=[dict(id='r'+str(i),title='Synthetic',created_on='2025-01-01',**extra) for i,extra in enumerate([
 {},{'frequency':'weekly'},{'workdays_only':False},{'frequency':'weekly','workdays_only':True}])],
 settings={},date_states={},notified=[])
Path('partial.json').write_text(json.dumps(raw),encoding='utf8')
partial=Store(Path('partial.json'))
assert partial.load_error is None
print(json.dumps(dict(input=raw,document=dict(events=[asdict(e) for e in partial.events],
 categories=[asdict(c) for c in partial.categories],routines=[asdict(r) for r in partial.routines]))))
'''

def generate(defaults=False):
    archive=subprocess.check_output(['git','archive','--format=zip',REV],cwd=ROOT)
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:z.extractall(root)
        env=dict(os.environ,APPDATA=str(root/'private-appdata'),PYTHONIOENCODING='utf-8')
        script=SCRIPT if not defaults else SCRIPT.split('s=Store(')[0]+DEFAULT_SCRIPT
        result=json.loads(subprocess.check_output([sys.executable,'-c',script],cwd=root,env=env,text=True,encoding='utf8'))
    result['provenance']=dict(tag='v0.10.2',commit=REV,generated_by='tagged Store.save; synthetic data',source_app_version='0.10.2')
    return result

if __name__=='__main__':
    output=ROOT/'tests'/'fixtures'/'schema8_v0102.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(generate(),ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(output)
