"""Synthetic B3a performance rehearsal. Explicit temp data only; no real migration."""
import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from calendar_core import Event,RoutineItem,EventCategory
from desktop_calendar.storage.calendar_migration import dry_run_migration
from habit_schedule import SCHEDULE_FIELDS


def sample(count):
    category=EventCategory('category','Synthetic',created_at='2025-01-01T10:00')
    habits=[]
    for i in range(10):
        habit=RoutineItem(str(i),'Synthetic habit',created_on='2024-02-29',frequency='weekly',
            weekdays=[0,2,4],interval=2,workdays_only=False,habit_done=['2024-03-01','2024-03-02'])
        rule={k:deepcopy(getattr(habit,k)) for k in SCHEDULE_FIELDS}
        habit.schedule_history=[dict(until=f'2025-{m:02d}-01',rule=deepcopy(rule)) for m in range(1,13)]
        habits.append(asdict(habit))
    return dict(version=9,categories=[asdict(category)],routines=habits,
        events=[asdict(Event(str(i),'Synthetic','2025-12-30T23:59',has_time=False,
            created_at='2025-01-01T10:00',duration_days=5,skip_non_working_days=True,end_as_ddl=True)) for i in range(count)],
        date_states={'2026-01-01':'leave'},settings={'unknown_legacy':{'keep':True}},notified=['old:key'])


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--counts',type=int,nargs='+',default=[100,1000,10000])
    parser.add_argument('--memory',action='store_true')
    args=parser.parse_args()
    print(json.dumps(dict(python=sys.version,executable=sys.executable,memory_tracking=args.memory)))
    for count in args.counts:
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.json'
            source.write_text(json.dumps(sample(count),ensure_ascii=False),encoding='utf8')
            if args.memory:tracemalloc.start()
            result=dry_run_migration(source,root/'stage')
            peak=tracemalloc.get_traced_memory()[1] if args.memory else None
            if args.memory:tracemalloc.stop()
            if not result['candidate_ready']:raise RuntimeError(result)
            print(json.dumps(dict(events=count,timings_ms=result['timings_ms'],
                peak_python_bytes=peak,sqlite_bytes=result['sqlite_bytes'],counts=result['imported_counts'],
                source_unchanged=result['source_unchanged'],comparison=result['comparison'])),flush=True)
