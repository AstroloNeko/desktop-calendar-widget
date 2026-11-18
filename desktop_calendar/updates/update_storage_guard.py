"""Fail closed before launching a payload updater on managed SQLite profiles."""
import json
import os
import re
from pathlib import Path
import zipfile
from desktop_calendar.storage.calendar_storage_lifecycle import StorageLifecycle, LifecycleError


def profile_root():
    return Path(os.getenv('APPDATA', str(Path.home()))) / 'DesktopCalendar'


def capabilities(version):
    return dict(format=1, application_version=version, sqlite_schemas=[1], storage_lifecycle=1, restore_epochs=True)


def validate_capabilities(data, version):
    try:
        value = json.loads(data)
        if not re.fullmatch(r'\d+\.\d+\.\d+', version.strip()) or not isinstance(value, dict) or type(value.get('format')) is not int or type(value.get('storage_lifecycle')) is not int or value != capabilities(version.strip()):
            raise ValueError()
    except (ValueError, TypeError):
        raise LifecycleError('更新包不具备当前 SQLite 生命周期兼容能力，已拒绝安装旧版。') from None


def managed_state(root):
    root = Path(root)
    if not (root / 'storage-state.json').exists() and not (root / '.storage-managed').exists():
        return False
    state = StorageLifecycle(root, existing_only=True)._read()
    if state['state'] == 'NOT_STARTED': return False
    if state['state'] != 'ACTIVE':
        raise LifecycleError('存储迁移或恢复尚未完成，已拒绝更新。')
    return True


def check_archive(archive, root=None):
    if not managed_state(root if root is not None else profile_root()): return
    try:
        with zipfile.ZipFile(archive) as package:
            cap = 'DesktopCalendar/storage-capabilities.json'
            ver = 'DesktopCalendar/app-version.txt'
            if any(package.namelist().count(name) != 1 or package.getinfo(name).file_size > 4096 for name in (cap, ver)):
                raise ValueError()
            validate_capabilities(package.read(cap), package.read(ver).decode('ascii'))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise LifecycleError('无法确认更新包支持 SQLite，未启动更新器；请勿回退旧版。') from exc


def check_directory(source):
    try:
        validate_capabilities((source/'storage-capabilities.json').read_bytes(),
                              (source/'app-version.txt').read_text(encoding='ascii'))
    except OSError as exc:
        raise LifecycleError('更新包缺少 SQLite 兼容声明，已拒绝安装。') from exc
