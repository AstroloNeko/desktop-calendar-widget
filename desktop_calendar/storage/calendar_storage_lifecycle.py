"""Storage authority and recovery; migration remains isolated-directory only.

Cooperating runtimes hold an exclusive OS lease for their entire lifetime.
Old executables do not honor this lease: deployment coordination belongs to B3c.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

# Keep lease handles alive if a slow model prevents complete runtime shutdown.
_QUARANTINED = {}

from calendar_core import DATA_DIR, Store
from desktop_calendar.storage.authority_state import LifecycleError, read_authority_state
from desktop_calendar.storage.calendar_migration import dry_run_migration
from desktop_calendar.storage.calendar_migration_validation import validate_source, ValidationResult, _validate_document
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from desktop_calendar.storage.sqlite_calendar_schema import APPLICATION_ID, SCHEMA_VERSION


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def durable_new(path, data):
    with Path(path).open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


def inspect_database(path):
    """Read-only, never initializes a missing database or changes journal mode."""
    path=Path(path)
    if not path.is_file():raise LifecycleError('database_missing')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID or db.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:
                raise LifecycleError('database_schema')
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
                raise LifecycleError('database_integrity')
            rows=db.execute('SELECT id,revision FROM calendars').fetchall()
            if len(rows)!=1:raise LifecycleError('database_identity')
            expected={'schema_migrations','calendars','events','categories','routines','routine_weekdays',
                'habit_completions','habit_schedule_history','date_states','preferences','reminder_delivery_state','operation_receipts'}
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables!=expected:raise LifecycleError('unexpected_database_tables')
            db.row_factory=sqlite3.Row
            raw=SqliteCalendarRepository._document(None,db)
            validation=ValidationResult();_validate_document(raw,validation)
            if not validation.valid:raise LifecycleError('invalid_database_business_snapshot')
            return dict(calendar_id=rows[0][0],revision=rows[0][1],schema=SCHEMA_VERSION)
    except sqlite3.Error as exc:raise LifecycleError('database_unreadable') from exc


class DirectoryLease:
    def __init__(self, root):self.path=Path(root)/'.calendar-lease';self.stream=None
    def __enter__(self):
        if self.path.is_symlink():raise LifecycleError('unsafe_lease_path')
        self.stream=self.path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                self.stream.seek(0);msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close();self.stream=None
            raise LifecycleError('directory_busy') from exc
        return self
    def __exit__(self,*args):
        if self.stream:
            # OS releases locks on close and on process termination.
            self.stream.close();self.stream=None


class StorageLifecycle:
    def __init__(self, directory, *, isolated=False, existing_only=False, migration_authorized=False, checkpoint=lambda name:None):
        self.root=Path(directory).resolve()
        actual=DATA_DIR.resolve()
        if not existing_only and not migration_authorized and (not isolated or self.root==actual or actual in self.root.parents):
            raise LifecycleError('explicit_isolated_directory_required')
        self.existing_only = existing_only
        self.root.mkdir(parents=True,exist_ok=True)
        self.state_path=self.root/'storage-state.json'
        self.witness=self.root/'.storage-managed'
        self.checkpoint=checkpoint

    def lease(self):return DirectoryLease(self.root)

    def _path(self,name):
        if not isinstance(name,str) or Path(name).name!=name or name in ('','.','..'):
            raise LifecycleError('unsafe_state_path')
        path=self.root/name
        if path.is_symlink() or path.resolve().parent!=self.root:raise LifecycleError('unsafe_state_path')
        return path

    def _read(self):
        return read_authority_state(self.root)

    def _write(self,state):
        temporary=self.root/('.state-'+uuid.uuid4().hex)
        durable_new(temporary,json.dumps(state,sort_keys=True).encode('utf8'))
        try:os.replace(temporary,self.state_path)
        finally:temporary.unlink(missing_ok=True)
        # File fsync + same-volume replace; directory fsync not portable on Windows.
        self.checkpoint(state['state'])

    def _verify_candidate(self,state,path):
        if digest(path)!=state['database_hash']:raise LifecycleError('candidate_changed')
        if inspect_database(path)!=state['identity']:raise LifecycleError('candidate_identity')
        if state['operation']=='migration':
            stat=self._path(state['source']).stat()
            if [stat.st_dev,stat.st_ino]!=state['source_identity']:raise LifecycleError('source_identity_changed')
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute('SELECT committed_revision FROM operation_receipts WHERE operation_id=?',('migration:'+state['source_hash'],)).fetchone()
                if row!=(state['identity']['revision'],):raise LifecycleError('migration_receipt_missing')

    def _verify_source(self,state):
        if state['operation']=='disaster_restore':
            from desktop_calendar.storage.calendar_disaster_recovery import verify_archive
            verify_archive(self, state)
        elif state['operation']=='migration':
            if digest(self._path(state['source']))!=state['source_hash']:raise LifecycleError('source_changed')
            if digest(self._path(state['protection']))!=state['source_hash']:raise LifecycleError('protection_changed')
            if 'report_hash' in state:
                report_path=self._path(state['report'])/'migration-report.json'
                if digest(report_path)!=state['report_hash']:raise LifecycleError('migration_report_changed')
        else:
            if inspect_database(self._path(state['previous_database']))!=state['previous_identity']:
                raise LifecycleError('restore_source_changed')
            if digest(self._path(state['protection']))!=state['protection_hash']:
                raise LifecycleError('restore_protection_changed')

    def _finish(self,state):
        self._verify_source(state)
        candidate=self._path(state['candidate']);final=self._path(state['database'])
        if state['state']=='STAGED':
            self._verify_candidate(state,candidate)
            state=dict(state,state='ACTIVATING');self._write(state)
        if final.exists():self._verify_candidate(state,final)
        else:
            self._verify_candidate(state,candidate)
            # Hard-link publication is same-volume and refuses an existing target.
            os.link(candidate,final)
            self.checkpoint('PLACED')
            self._verify_candidate(state,final)
        state=dict(state,state='ACTIVE');self._write(state)
        # The final file is now the only live link. Candidate is no longer mutable.
        candidate.unlink(missing_ok=True)
        return state

    def migrate(self, *, expected_source_hash=None):
        if self.existing_only:raise LifecycleError('production_migration_disabled')
        with self.lease():
            state=self._read()
            if state['state']=='ACTIVE':raise LifecycleError('already_active')
            if expected_source_hash is not None and state['state'] in ('STAGED','ACTIVATING') and state.get('source_hash') != expected_source_hash:
                raise LifecycleError('source_changed')
            if state['state'] in ('STAGED','ACTIVATING'):return self._finish(state)
            if state['state']!='NOT_STARTED':raise LifecycleError('preparation_requires_review')
            source=self._path('calendar_data.json');validation=validate_source(source)
            if not validation.valid:raise LifecycleError('invalid_source')
            if expected_source_hash is not None and validation.source['sha256'] != expected_source_hash:
                raise LifecycleError('source_changed')
            token=uuid.uuid4().hex
            if not self.witness.exists():durable_new(self.witness,b'Explicit isolated storage lifecycle v1\n')
            state=dict(format=1,state='PREPARING',operation='migration',epoch=token,source=source.name,
                source_identity=[source.stat().st_dev,source.stat().st_ino],
                source_hash=validation.source['sha256'],source_schema=validation.source['json_schema'],
                protection='original-'+token+'.json',database='calendar-'+token+'.sqlite3',
                created_at=datetime.now(timezone.utc).isoformat())
            self._write(state)
            durable_new(self._path(state['protection']),source.read_bytes());self._verify_source(state)
            self.checkpoint('PROTECTED')
            report=dry_run_migration(source,self.root/('stage-'+token),managed_directory=self.root)
            if not report['candidate_ready']:raise LifecycleError('staging_failed')
            # Use backup API to produce a closed, standalone, WAL-independent candidate.
            store=Store(Path(report['candidate_path']),repository_factory=SqliteCalendarRepository)
            candidate=self._path('candidate-'+token+'.sqlite3')
            try:store.repository.backup_to(candidate)
            finally:store.repository.close()
            state.update(candidate=candidate.name,database_hash=digest(candidate),identity=inspect_database(candidate),report='stage-'+token,
                report_hash=digest(self.root/('stage-'+token)/'migration-report.json'))
            state['state']='STAGED';self._write(state)
            return self._finish(state)

    def _select(self):
        state=self._read()
        if state['state']=='NOT_STARTED':return state,self._path('calendar_data.json'),None
        if state['state'] in ('STAGED','ACTIVATING'):state=self._finish(state)
        if state['state']!='ACTIVE':raise LifecycleError('recovery_required')
        path=self._path(state['database']);identity=inspect_database(path)
        if identity['calendar_id']!=state['identity']['calendar_id'] or identity['revision']<state['identity']['revision']:
            raise LifecycleError('active_database_identity')
        return state,path,SqliteCalendarRepository

    def backup(self,destination):
        from desktop_calendar.storage.calendar_sqlite_backup import create_sqlite_backup
        with self.lease():
            state,path,factory=self._select()
            if factory is None:raise LifecycleError('sqlite_not_active')
            return create_sqlite_backup(path,destination)

    def abandon_preparation(self):
        """Explicit retry gate before activation only; retain all forensic files."""
        if self.existing_only:raise LifecycleError('production_migration_disabled')
        with self.lease():
            state=self._read()
            if state['state']!='PREPARING' or state['operation']!='migration':
                raise LifecycleError('cannot_abandon_activation')
            source=self._path(state['source'])
            if digest(source)!=state['source_hash']:raise LifecycleError('source_changed')
            if self._path(state['database']).exists():raise LifecycleError('unexpected_final_database')
            durable_new(self.root/('abandoned-'+uuid.uuid4().hex+'.json'),json.dumps(state).encode())
            self._write(dict(format=1,state='NOT_STARTED'))

    def restore(self,backup,*,authorize_replacement=False,reminder_replay_reviewed=False,expected_backup_hash=None):
        """Offline only. Explicit consent required even for a same-revision backup.

        Every restore changes the persistent epoch and starts a fresh journal/Undo
        context. Prior database and original AI history remain retained.
        """
        with self.lease():
            if expected_backup_hash is not None and digest(backup) != expected_backup_hash:
                raise LifecycleError('backup_changed_since_review')
            previous,path,factory=self._select()
            if factory is None:raise LifecycleError('sqlite_not_active')
            if not authorize_replacement:raise LifecycleError('restore_requires_explicit_authorization')
            from desktop_calendar.storage.calendar_disaster_recovery import candidate_from_backup
            token=uuid.uuid4().hex
            candidate,identity=candidate_from_backup(self,backup,token)
            if expected_backup_hash is not None and digest(backup) != expected_backup_hash:
                raise LifecycleError('backup_changed_since_review')
            protection=self._path('before-restore-'+token+'.sqlite3')
            store=Store(path,repository_factory=SqliteCalendarRepository)
            try:store.repository.backup_to(protection)
            finally:store.repository.close()
            old_identity=inspect_database(path)
            if inspect_database(protection)!=old_identity:raise LifecycleError('restore_protection_identity')
            state=dict(format=1,state='STAGED',operation='restore',epoch=token,
                previous_epoch=previous['epoch'],previous_database=path.name,previous_identity=old_identity,
                protection=protection.name,protection_hash=digest(protection),candidate=candidate.name,
                database='calendar-'+token+'.sqlite3',database_hash=digest(candidate),identity=identity,
                reminder_replay_warning=True,reminder_replay_reviewed=reminder_replay_reviewed)
            self._write(state)
            return self._finish(state)

    @contextmanager
    def runtime(self, *, acknowledge_reminder_replay=False, **options):
        """Lifetime lease prevents restoration while Store/Undo/HTTP are alive.

        Epoch-specific configuration/journal directories invalidate all previous
        pending requests without modifying the ChatJournal schema or old history.
        """
        from calendar_runtime import HeadlessRuntime
        from desktop_calendar.assistant.llm_provider import AIConfiguration
        if any(key in options for key in ('journal','configuration','repository_factory')):
            raise LifecycleError('lifecycle_owned_runtime_options')
        lease=self.lease();lease.__enter__()
        runtime=None
        try:
            state,path,factory=self._select()
            if state.get('operation')=='restore' and options.get('notification_backend') is not None and not acknowledge_reminder_replay:
                raise LifecycleError('restored_reminder_ledger_requires_review')
            config=AIConfiguration(self.root/('runtime-'+state.get('epoch','json')),options.get('protector'))
            runtime=HeadlessRuntime(path,configuration=config,repository_factory=factory,**options)
            yield runtime
        finally:
            try:
                if runtime is not None:runtime.stop()
            except BaseException:
                _QUARANTINED[str(self.root)]=(runtime,lease)
                raise
            else:lease.__exit__(None,None,None)

    def retry_shutdown(self):
        item=_QUARANTINED.get(str(self.root))
        if item:
            runtime,lease=item;runtime.stop();lease.__exit__(None,None,None)
            del _QUARANTINED[str(self.root)]
