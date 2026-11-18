"""Isolated lifecycle backup format 2. No changes to production format 1 UI."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import zipfile

from calendar_core import Store
from desktop_calendar.storage.calendar_storage_lifecycle import LifecycleError,inspect_database,durable_new
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository
from version import __version__

LIMIT=64*1024*1024


def _secret_keys(value):
    if isinstance(value,dict):
        for key,item in value.items():
            name=str(key).lower().replace('-','_')
            if any(word in name for word in ('api_key','apikey','bearer','password','secret','token','dpapi')):
                return True
            if _secret_keys(item):return True
    elif isinstance(value,list):return any(_secret_keys(item) for item in value)
    return False


def create_sqlite_backup(path,destination):
    """Includes calendar/preferences/delivery state, excludes external AI files.

    Unexpected credential-bearing preference keys block export rather than being
    silently stripped. Arbitrary user-authored notes remain calendar content.
    """
    destination=Path(destination)
    inspect_database(path)
    with tempfile.TemporaryDirectory() as folder:
        snapshot=Path(folder)/'calendar.sqlite3'
        store=Store(Path(path),repository_factory=SqliteCalendarRepository)
        try:store.repository.backup_to(snapshot)
        finally:store.repository.close()
        with closing(sqlite3.connect(snapshot)) as db:
            settings={k:json.loads(v) for k,v in db.execute('SELECT key,value_json FROM preferences')}
        if _secret_keys(settings):raise LifecycleError('credential_preferences_not_exportable')
        identity=inspect_database(snapshot);data=snapshot.read_bytes()
        if len(data)>LIMIT:raise LifecycleError('backup_too_large')
        manifest=dict(format='DesktopCalendarBackup',format_version=2,backend='sqlite',
            app_version=__version__,created_at=datetime.now(timezone.utc).isoformat(),
            identity=identity,files={'calendar.sqlite3':dict(size=len(data),sha256=hashlib.sha256(data).hexdigest())},
            scope=['calendar','preferences','reminder_delivery'],excluded=['ChatJournal','AI configuration','credentials'])
        # Incomplete files are never published at the requested destination.
        staging=Path(folder)/'package'
        with zipfile.ZipFile(staging,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',json.dumps(manifest))
            archive.writestr('calendar.sqlite3',data)
        read_container(staging)
        durable_new(destination,staging.read_bytes())
    return manifest


def read_container(path):
    """Bounded allowlist reads; never extract paths from an untrusted ZIP."""
    path=Path(path)
    try:
        if path.stat().st_size>LIMIT+1024*1024:raise LifecycleError('backup_too_large')
        with zipfile.ZipFile(path) as archive:
            members=archive.infolist();names=[m.filename for m in members]
            if len(names)!=2 or set(names) not in ({'manifest.json','calendar.sqlite3'},{'manifest.json','calendar_data.json'}):
                raise LifecycleError('backup_members')
            for item in members:
                kind=stat.S_IFMT(item.external_attr>>16)
                if item.is_dir() or item.flag_bits&1 or kind not in (0,stat.S_IFREG) or item.file_size>(65536 if item.filename=='manifest.json' else LIMIT):
                    raise LifecycleError('unsafe_backup_member')
            manifest=json.loads(archive.read('manifest.json'))
            name=next(n for n in names if n!='manifest.json');data=archive.read(name)
        if name=='calendar_data.json':
            # Existing format-1 verification, followed later by strict B3a import.
            from data_backup import read_backup
            return (*read_backup(path),'json')
        if manifest.get('format')!='DesktopCalendarBackup' or type(manifest.get('format_version')) is not int or manifest['format_version']!=2 or manifest.get('backend')!='sqlite':
            raise LifecycleError('backup_format')
        if manifest.get('files')!={name:dict(size=len(data),sha256=hashlib.sha256(data).hexdigest())}:
            raise LifecycleError('backup_hash')
        if datetime.fromisoformat(manifest['created_at']).tzinfo is None:raise LifecycleError('backup_timestamp')
        return manifest,data,'sqlite'
    except LifecycleError:raise
    except Exception as exc:raise LifecycleError('invalid_backup_container') from exc
