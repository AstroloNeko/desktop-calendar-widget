"""Reproducible isolated JSON/SQLite service benchmark; never uses user data."""
import argparse
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calendar_core import Store, Event, EventCategory, RoutineItem
from desktop_calendar.assistant.calendar_action_gateway import CalendarActionGateway
from calendar_event_service import CalendarEventService
from calendar_mutation_service import CalendarMutationService
from event_commands import EventCommandHistory
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository


def measure(function, repeats):
    values=[]
    for _ in range(repeats):
        start=time.perf_counter(); function(); values.append((time.perf_counter()-start)*1000)
    return round(statistics.median(values),3)


def run(count, backend, repeats):
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/('calendar.db' if backend=='sqlite' else 'calendar.json')
        factory=SqliteCalendarRepository if backend=='sqlite' else None
        store=Store(path,repository_factory=factory)
        history=EventCommandHistory(store)
        events=CalendarEventService(history); mutations=CalendarMutationService(history)
        # Only fixture population uses the prepared repository port. Measurements
        # below exercise Application Services and the real query projection.
        categories=[EventCategory(str(i),'Category '+str(i),created_at='2026-01-01T00:00') for i in range(10)]
        routines=[RoutineItem(str(i),'Habit '+str(i),created_on='2026-01-01',
            workdays_only=False,habit_done=['2026-01-01','2026-02-01']) for i in range(20)]
        rows=[Event(str(i),'Synthetic '+str(i),'2026-11-20T23:59',has_time=False,
                    category_id=str(i%10),created_at='2026-01-01T00:00') for i in range(count)]
        store.repository.apply_changes(dict(events=rows,categories=categories,routines=routines,
            date_states={'2026-11-23':'leave','2026-11-24':'holiday'}))
        store.save()
        gateway=CalendarActionGateway(store,history)
        report={'backend':backend,'events':count,'repeats':repeats}
        def load():
            loaded=Store(path,repository_factory=factory)
            assert loaded.load_error is None
            loaded.repository.close()
        report['load_ms']=measure(load,repeats)
        report['range_query_ms']=measure(lambda:gateway.query({'start':'2026-11-19','end':'2026-11-25'}),repeats)
        if count:
            index=0
            def edit():
                nonlocal index
                index+=1; events.patch('0',{'title':'Changed '+str(index)})
            report['single_patch_ms']=measure(edit,repeats)
            report['batch50_ms']=measure(lambda:events.bulk([str(i) for i in range(min(count,50))],'shift',days=1),repeats)
            report['undo_redo_ms']=measure(lambda:(history.undo(),history.redo()),repeats)
        status=False
        def commit():
            nonlocal status
            status=not status
            mutations.set_date_status(date(2026,12,1),'leave' if status else 'normal')
        report['date_commit_ms']=measure(commit,repeats)
        preference=0
        def save():
            nonlocal preference
            preference+=1; store.settings['benchmark_device_setting']=preference; store.save()
        report['auxiliary_save_ms']=measure(save,repeats)
        report['close_ms']=measure(store.repository.close,1)
        report['file_bytes']=path.stat().st_size
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--counts',type=int,nargs='+',default=[0,1000,10000])
    options=parser.parse_args()
    print(json.dumps({'python':sys.version,'executable':sys.executable}))
    for count in options.counts:
        for backend in ('json','sqlite'):
            print(json.dumps(run(count,backend,options.repeats)),flush=True)
