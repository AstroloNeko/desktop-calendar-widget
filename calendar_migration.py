"""Explicit JSON9 -> SQLite1 rehearsal. Never activates or discovers user data."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import time

from calendar_core import Store, DATA_DIR
from calendar_migration_validation import validate_source, MigrationIssue, MODELS
from sqlite_calendar_repository import SqliteCalendarRepository, encoded
from sqlite_calendar_schema import SCHEMA_VERSION


def _digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


class _ReadOnlyDocument:
    def __init__(self,store,document): self.document=document
    def read_document(self): return deepcopy(self.document)
    def write_current(self): raise RuntimeError('Migration source snapshot is read-only')
    def close(self): pass


def _source_store(document, source):
    # Strict validation has already proved every row survives the model boundary.
    # This reader never reads the path or exposes a source writer.
    return Store(source,repository_factory=lambda store:_ReadOnlyDocument(store,document))


def _raw_snapshot(store):
    return dict(version=9,events=[asdict(e) for e in store.events],
        routines=[asdict(r) for r in store.routines],categories=[asdict(c) for c in store.categories],
        date_states=dict(store.date_states))


def _semantic_snapshot(store, clock):
    """All Event occurrences; bounded deterministic query/recurrence probes."""
    from desktop_calendar.reminders.reminder_engine import normalize_config, plans_for_store, CONFIG_KEY
    event_rows=[]
    all_days=set()
    for event in store.events:
        days=store.event_dates(event)
        all_days.update(days)
        event_rows.append((event.id,[d.isoformat() for d in days],
            store.event_ends_at(event).isoformat() if store.event_has_deadline(event) else None,
            store.effective_event_color(event)))
    ordered=sorted(all_days)
    probes=set(ordered[:16]+ordered[-16:])
    probes.update(date.fromisoformat(day) for day in store.date_states)
    probes.update(clock.date()+timedelta(days=d) for d in range(-7,8))
    probes.update([date(clock.year,1,1),date(clock.year,12,31)])
    queries=[(d.isoformat(),[e.id for e in store.events_on(d)]) for d in sorted(probes)]
    habits=[]
    for routine in store.routines:
        anchor=date.fromisoformat(routine.start_on)
        days={anchor+timedelta(days=i) for i in range(732) if anchor.toordinal()+i<=date.max.toordinal()}
        days.update(probes)
        for text in [*routine.habit_done,*[h['until'] for h in routine.schedule_history],routine.end_on]:
            if not text: continue
            point=date.fromisoformat(text)
            days.update(date.fromordinal(point.toordinal()+i) for i in (-1,0,1) if 1<=point.toordinal()+i<=date.max.toordinal())
        habits.append((routine.id,[(d.isoformat(),store.routine_planned_on(routine,d),routine.is_done_on(d)) for d in sorted(days)],
            store.routine_progress(routine,clock.date()-timedelta(days=31),clock.date()+timedelta(days=31))))
    config=normalize_config(store.settings.get(CONFIG_KEY,{}))
    plans=[(p.event_id,p.key,p.when.isoformat(),p.anchor,p.ddl) for p in plans_for_store(store,config)]
    groups=store.complete_ddl_groups(clock)
    return dict(events=event_rows,queries=queries,habits=habits,reminder_plans=plans,
        ddl_groups={key:[e.id for e in getattr(groups,key)] for key in ('overdue','due_soon','future','completed')},
        effective_preferences=store.settings,notified=sorted(store.notified))


def _first_difference(left,right,path='$'):
    if type(left) is not type(right): return path
    if isinstance(left,dict):
        if left.keys()!=right.keys(): return path
        for key in left:
            found=_first_difference(left[key],right[key],path+'['+json.dumps(key,ensure_ascii=True)+']')
            if found:return found
    elif isinstance(left,(tuple,list)):
        if len(left)!=len(right):return path
        for i,(a,b) in enumerate(zip(left,right)):
            found=_first_difference(a,b,f'{path}[{i}]')
            if found:return found
    elif left!=right:return path
    return None


def _reopen_target(path):
    store=Store(path,repository_factory=SqliteCalendarRepository)
    if store.load_error:
        store.repository.close()
        raise ValueError('Target reload failed')
    return store


def _compare_candidate(document, target, source, clock):
    with closing(target.repository._connect()) as db:
        raw=target.repository._document(db)
    raw['notified']=sorted(raw['notified'])
    mismatch=_first_difference(document,raw)
    if mismatch: return dict(equal=False,path=mismatch,phase='raw_fields')
    before=_source_store(document,source)
    if before.load_error: return dict(equal=False,path='$',phase='source_projection')
    # Detect even a hypothetical tolerant-loader loss independently of both backends.
    expected={key:document[key] for key in ('version','events','routines','categories','date_states')}
    for label,store in (('source_model',before),('target_model',target)):
        mismatch=_first_difference(expected,_raw_snapshot(store))
        if mismatch:return dict(equal=False,path=mismatch,phase=label)
    left=_semantic_snapshot(before,clock)
    right=_semantic_snapshot(target,clock)
    mismatch=_first_difference(left,right)
    return dict(equal=mismatch is None,path=mismatch,phase='semantics',
        semantic_sha256=hashlib.sha256(encoded(left).encode()).hexdigest(),
        event_occurrences=sum(len(row[1]) for row in left['events']),
        query_probe_days=len(left['queries']),habit_probe_days=sum(len(row[1]) for row in left['habits']))


def _integrity(repo):
    with closing(repo._connect()) as db:
        quick=[r[0] for r in db.execute('PRAGMA quick_check')]
        foreign=list(db.execute('PRAGMA foreign_key_check'))
        if quick!=['ok'] or foreign:raise ValueError('SQLite integrity failure')
        return dict(quick_check='ok',foreign_key_violations=0,sqlite_schema=SCHEMA_VERSION)


def report_text(report):
    lines=[f"迁移预演：{report['status']}",f"候选可供验收：{report['candidate_ready']}",
           '正式存储切换：禁止（B3a 仅预演）',f"源 SHA-256：{report['validation']['source'].get('sha256')}",
           '源记录数：'+json.dumps(report['validation']['counts'],ensure_ascii=False)]
    for issue in report['validation']['issues']+report['issues']:
        lines.append(f"{issue['code']} @ {issue['source_path']}: {issue['message']}")
    return '\n'.join(lines)+'\n'


def _write_reports(stage,report):
    # Caller reserved these paths exclusively for this invocation.
    for name,content in (('migration-report.json',json.dumps(report,ensure_ascii=False,indent=2)),
                         ('migration-report.txt',report_text(report))):
        temporary=stage/(name+'.tmp')
        with temporary.open('x',encoding='utf8') as stream: stream.write(content)
        temporary.replace(stage/name)


def dry_run_migration(source, staging_directory, *, clock=datetime(2026,1,15,12), managed_directory=None):
    """Stable explicit interface for B3b. Returns a redacted, JSON-serializable report.

    A successful candidate is NOT permission to activate. No implicit input/output
    paths, source recovery, user-directory lock or update/backup work is performed.
    """
    source=Path(source).resolve()
    stage=Path(staging_directory).resolve()
    validation=validate_source(source)
    report=dict(status='rejected',candidate_ready=False,switch_allowed=False,
        validation=validation.report(),issues=[],timings_ms={'validation':validation.elapsed_ms},
        candidate_path=None,source_unchanged=None,clock=clock.isoformat() if isinstance(clock,datetime) else None,comparison=None)
    report.update(validation.compatibility)
    report.update(source_schema_version=validation.source.get('json_schema'),
                  source_sha256=validation.source.get('sha256'),original_counts=validation.counts,
                  target_sqlite_schema_version=SCHEMA_VERSION)
    target=stage/'calendar.candidate.sqlite3'
    owned=False; reserved=[]; store=None; phase='path_validation'
    def issue(code,message,path='$'):
        report['issues'].append(asdict(MigrationIssue(code,path,message)))
    def cleanup_candidate():
        if not owned:return
        for path in (target,Path(str(target)+'-wal'),Path(str(target)+'-shm')):
            try:path.unlink(missing_ok=True)
            except OSError:issue('candidate_cleanup_failed','未完成候选文件未能移除，不能用于切换。')
    try:
        if not isinstance(clock,datetime) or clock.tzinfo is not None or not 2<=clock.year<=9998:
            raise ValueError('Clock must be explicit naive datetime away from date bounds')
        managed = (managed_directory is not None and stage.parent == Path(managed_directory).resolve()
                   and stage.name.startswith(('stage-', 'restore-stage-')))
        if stage==source.parent or (not managed and (stage==DATA_DIR.resolve() or DATA_DIR.resolve() in stage.parents)):
            issue('unsafe_staging_directory','必须使用正式用户目录及源目录以外的显式隔离暂存目录。')
            return report
        if target.exists() or any((stage/name).exists() for name in ('migration-report.json','migration-report.txt')):
            issue('target_exists','候选或报告已存在，不覆盖；请指定新的暂存目录。')
            return report
        stage.mkdir(parents=True,exist_ok=True)
        for name in ('migration-report.json','migration-report.txt'):
            path=stage/name
            with path.open('x',encoding='utf8'):pass
            reserved.append(path)
        report['status']='validating' if validation.valid else 'rejected'
        if not validation.valid and validation.source.get('sha256'):
            report['source_unchanged']=_digest(source)==validation.source['sha256']
        _write_reports(stage,report)
        if not validation.valid: return report
        phase='sqlite_import'
        with target.open('xb'):pass
        owned=True
        started=time.perf_counter()
        store=Store(target,repository_factory=SqliteCalendarRepository)
        if store.load_error:raise ValueError('Candidate initialization failed')
        document=validation._document
        changes={key:[model.from_dict(deepcopy(row)) for row in document[key]] for key,model in MODELS.items()}
        changes.update(date_states=document['date_states'],notified=set(document['notified']))
        result=store.repository.import_snapshot(changes,document['settings'],source_hash=validation.source['sha256'])
        report['import_revision']=asdict(result.revision)
        report['operation_id']=result.operation_id
        report['timings_ms']['import']=round((time.perf_counter()-started)*1000,3)
        phase='sqlite_integrity_failure';started=time.perf_counter()
        report['integrity']=_integrity(store.repository)
        report['timings_ms']['integrity']=round((time.perf_counter()-started)*1000,3)
        store.repository.close();store=None
        phase='target_reopen_failed'
        store=_reopen_target(target)
        phase='inconsistent_snapshot';started=time.perf_counter()
        comparison=_compare_candidate(document,store,source,clock)
        report['comparison']=comparison
        report['semantic_comparison']=comparison
        report['imported_counts']=dict(events=len(store.events),routines=len(store.routines),
            categories=len(store.categories),date_states=len(store.date_states),
            habit_completions=sum(len(r.habit_done) for r in store.routines),
            habit_schedule_history=sum(len(r.schedule_history) for r in store.routines),
            preference_keys=len(document['settings']),reminder_delivery=len(store.notified))
        report['timings_ms']['comparison']=round((time.perf_counter()-started)*1000,3)
        if not comparison['equal']:
            issue('inconsistent_snapshot','逐字段或业务语义比对不一致，候选不可切换。',comparison['path'] or '$')
            raise ValueError('Comparison failed')
        phase='source_changed'
        report['source_unchanged']=_digest(source)==validation.source['sha256']
        if not report['source_unchanged']:raise ValueError('Source changed')
        report.update(status='validated_candidate',candidate_ready=True,candidate_path=str(target),
                      sqlite_bytes=target.stat().st_size)
        phase='report_write_failed'
        _write_reports(stage,report)
    except (OSError,ValueError,TypeError,KeyError,OverflowError,RuntimeError) as exc:
        # SQLite errors are separately included below by the common Exception branch;
        # do not echo exception text, which can contain business field values.
        if not report['issues']:issue(phase,'预演未通过该阶段；源文件未被导入器修改，请检查问题码。')
        report['candidate_ready']=False
    except Exception:
        issue(phase,'预演发生存储或校验异常；未批准候选。')
        report['candidate_ready']=False
    finally:
        if store is not None:store.repository.close()
        if validation.source.get('sha256'):
            try:report['source_unchanged']=_digest(source)==validation.source['sha256']
            except OSError:report['source_unchanged']=False
            if report['candidate_ready'] and not report['source_unchanged']:
                report['candidate_ready']=False
                issue('source_changed','最终校验时源文件发生变化，候选不可用于切换。')
        if not report['candidate_ready'] and owned:
            # Exact files belonging to this invocation; never recursive cleanup.
            cleanup_candidate()
    if not report['candidate_ready']:
        report.update(status='failed',candidate_path=None)
    if len(reserved)==2:
        try:_write_reports(stage,report)
        except OSError:
            issue('report_write_failed','报告未能完整写入，请使用调用返回的结构化报告。')
            report.update(status='failed',candidate_ready=False,candidate_path=None)
            cleanup_candidate()
    return report
