"""Explicit offline restore; damaged authority is never opened as a Store."""
import hashlib
import json
from pathlib import Path
import uuid

from desktop_calendar.storage.calendar_storage_lifecycle import LifecycleError, durable_new, digest, inspect_database
from desktop_calendar.storage.calendar_sqlite_backup import read_container
from desktop_calendar.storage.calendar_migration import dry_run_migration
from calendar_core import Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository


def candidate_from_backup(manager, backup, token):
    manifest, data, backend = read_container(backup)
    if backend == 'json':
        return candidate_from_legacy_json(manager, data, token)
    candidate = manager._path('candidate-' + token + '.sqlite3')
    durable_new(candidate, data)
    identity = inspect_database(candidate)
    if identity != manifest.get('identity'):
        raise LifecycleError('backup_identity')
    return candidate, identity


def read_reviewed_json(source, expected_source_hash):
    from desktop_calendar.storage.calendar_migration_validation import MAX_SOURCE_BYTES
    with Path(source).open('rb') as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != expected_source_hash:
        raise LifecycleError('json_changed_since_review')
    return data


def candidate_from_legacy_json(manager, data, token):
    source = manager._path('restore-input-' + token + '.json')
    durable_new(source, data)
    report = dry_run_migration(source, manager.root / ('restore-stage-' + token), managed_directory=manager.root)
    if not report['candidate_ready']:
        raise LifecycleError('legacy_backup_invalid')
    candidate = manager._path('candidate-' + token + '.sqlite3')
    store = Store(Path(report['candidate_path']), repository_factory=SqliteCalendarRepository)
    try:
        store.repository.backup_to(candidate)
    finally:
        store.repository.close()
    return candidate, inspect_database(candidate)


def verify_archive(manager, state):
    archive = manager._path(state['archive'])
    for name, expected in state['archived_files'].items():
        source = manager._path(name)
        copy = archive / name
        if copy.is_symlink() or digest(copy) != expected:
            raise LifecycleError('diagnostic_archive_changed')
        if not source.is_file() or digest(source) != expected:
            raise LifecycleError('damaged_source_changed')


def disaster_restore(manager, backup, *, authorize_replacement=False,
                     reminder_replay_reviewed=False, expected_backup_hash=None):
    if not authorize_replacement or not reminder_replay_reviewed:
        raise LifecycleError('disaster_restore_requires_explicit_review')
    with manager.lease():
        if expected_backup_hash is not None and digest(backup) != expected_backup_hash:
            raise LifecycleError('backup_changed_since_review')
        # Never consult _select(): ACTIVE may be genuinely unreadable. Preserve
        # all local database candidates/sidecars, plus raw authority metadata.
        if not manager.witness.exists() and not manager.state_path.exists():
            raise LifecycleError('no_managed_storage_evidence')
        token = uuid.uuid4().hex
        archive = manager._path('disaster-original-' + token)
        archive.mkdir()
        names = {p.name for p in manager.root.iterdir()
                 if p.name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))}
        names.update(p.name for p in (manager.state_path, manager.witness) if p.exists())
        records = {}
        for name in sorted(names):
            source = manager._path(name)
            before = digest(source)
            durable_new(archive / name, source.read_bytes())
            if digest(archive / name) != before or digest(source) != before:
                raise LifecycleError('diagnostic_archive_failed')
            records[name] = before
        durable_new(archive / 'inventory.json', json.dumps(records, sort_keys=True).encode())
        manager.checkpoint('ARCHIVED')
        candidate, identity = candidate_from_backup(manager, backup, token)
        if expected_backup_hash is not None and digest(backup) != expected_backup_hash:
            raise LifecycleError('backup_changed_since_review')
        # Metadata is replaced during activation; its immutable original remains
        # archived, but source comparisons cover only DB/WAL/SHM thereafter.
        db_records = {k: v for k, v in records.items() if k.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))}
        state = dict(format=1,state='STAGED',operation='disaster_restore',epoch=token,
            archive=archive.name,archived_files=db_records,candidate=candidate.name,
            database='calendar-'+token+'.sqlite3',database_hash=digest(candidate),identity=identity,
            reminder_replay_warning=True,reminder_replay_reviewed=True)
        if not manager.witness.exists():
            durable_new(manager.witness, b'Explicit disaster recovery\n')
        manager._write(state)
        return manager._finish(state)
