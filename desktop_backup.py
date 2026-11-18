"""Authority-aware desktop backup helpers; no live WAL file copying."""
from datetime import datetime, timezone
from pathlib import Path
import os
import uuid
from calendar_sqlite_backup import create_sqlite_backup
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository


def sqlite_store(store):
    return isinstance(store.repository, SqliteCalendarRepository)


def export_sqlite(store, destination, *, overwrite=False):
    destination = Path(destination)
    if destination.is_symlink() or destination.suffix.lower() != '.dcalbackup':
        raise ValueError('备份必须写入独立的 .dcalbackup 文件，不能覆盖存储元数据。')
    if destination.resolve() == store.data_file.resolve():
        raise ValueError('不能用备份覆盖当前数据库。')
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    staged = destination.with_name('.backup-' + uuid.uuid4().hex)
    try:
        manifest = create_sqlite_backup(store.data_file, staged)
        if overwrite:
            os.replace(staged, destination)
        else:
            # Exclusive publication; a racing existing file is never overwritten.
            os.link(staged, destination)
        return manifest
    finally:
        staged.unlink(missing_ok=True)


def automatic_sqlite_backup(store):
    folder = store.data_file.parent / 'backups'
    folder.mkdir(exist_ok=True)
    name = 'sqlite-auto-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '.dcalbackup'
    target = folder / name
    if target.exists():
        from calendar_sqlite_backup import read_container
        if read_container(target)[2] != 'sqlite':
            raise ValueError('自动备份路径存在非 SQLite 容器，请保留并检查。')
    else:
        export_sqlite(store, target)
    return target
