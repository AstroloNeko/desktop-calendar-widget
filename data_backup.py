"""Local, checksummed single-file snapshots; no GUI, network or ZIP extraction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import zipfile

from version import __version__
from data_compat import DATA_SCHEMA_VERSION, decode_calendar_data

FORMAT_VERSION = 1
SCHEMA_VERSION = DATA_SCHEMA_VERSION
MAX_DATA_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
AUTO_KEEP = 10
DATA_MEMBER = "calendar_data.json"


class BackupError(ValueError):
    pass


def validate_data(data: bytes) -> dict:
    if len(data) > MAX_DATA_BYTES:
        raise BackupError("数据超过安全大小限制（32 MB）。")
    try:
        raw = decode_calendar_data(data, supported=SCHEMA_VERSION, app_version=__version__)
        # Use the real legacy readers, but reject any record they would discard.
        from calendar_core import Event, EventCategory, RoutineItem
        for key, model in (("events", Event), ("categories", EventCategory), ("routines", RoutineItem)):
            records = raw.get(key, [])
            if not isinstance(records, list):
                raise ValueError(f"{key} 结构无效")
            ids = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"{key} 记录无效")
                item = model.from_dict(record)
                if item.id in ids:
                    raise ValueError(f"{key} 存在重复 ID")
                ids.add(item.id)
        for key in ("settings", "date_states"):
            if not isinstance(raw.get(key, {}), dict):
                raise ValueError(f"{key} 结构无效")
        # Legacy readers intentionally ignore invalid date-state entries. Keep
        # their original bytes in backups rather than breaking that migration.
        if not isinstance(raw.get("notified", []), list):
            raise ValueError("提醒历史结构无效")
        return raw
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, AttributeError, ValueError) as exc:
        raise BackupError(f"无法安全读取日历数据：{exc}") from exc


def read_backup(path: Path) -> tuple[dict, bytes]:
    """Read only two allowlisted members, with bounds before decompression."""
    try:
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BackupError("备份文件过大。")
        with zipfile.ZipFile(path) as package:
            members = package.infolist()
            if len(members) != 2 or {m.filename for m in members} != {"manifest.json", DATA_MEMBER}:
                raise BackupError("备份文件清单无效（不能包含额外文件或路径）。")
            for member in members:
                limit = 64 * 1024 if member.filename == "manifest.json" else MAX_DATA_BYTES
                if (member.file_size > limit or member.flag_bits & 1 or member.is_dir()
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise BackupError("备份包含异常大小、加密或不安全的文件。")
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
            data = package.read(DATA_MEMBER)
        if not isinstance(manifest, dict) or manifest.get("format") != "DesktopCalendarBackup":
            raise BackupError("备份文件格式无效。")
        if type(manifest.get("format_version")) is not int or manifest["format_version"] != FORMAT_VERSION:
            raise BackupError("备份容器格式版本不兼容，请使用支持此备份的版本。")
        raw = validate_data(data)
        if manifest.get("schema_version") != raw.get("version", 1):
            raise BackupError("备份格式或数据版本信息无效。")
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or not isinstance(manifest.get("app_version"), str):
            raise BackupError("备份来源信息无效。")
        expected = {DATA_MEMBER: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
        if manifest.get("files") != expected:
            raise BackupError("备份 SHA256 或文件大小校验失败。")
        return manifest, data
    except BackupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
        raise BackupError(f"无法读取备份：{exc}") from exc


def _stage_bytes(target: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".calendar-stage-", dir=target.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return stage
    except BaseException:
        stage.unlink(missing_ok=True)
        raise


def create_backup(data: bytes, destination: Path, *, reason: str = "manual", overwrite: bool = False) -> Path:
    raw = validate_data(data)
    manifest = {"format": "DesktopCalendarBackup", "format_version": FORMAT_VERSION,
                "schema_version": raw.get("version", 1), "app_version": __version__,
                "created_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
                "files": {DATA_MEMBER: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
    fd, name = tempfile.mkstemp(prefix=".calendar-backup-", dir=destination.parent)
    os.close(fd)
    stage = Path(name)
    try:
        with zipfile.ZipFile(stage, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            package.writestr(DATA_MEMBER, data)
        read_backup(stage)
        with stage.open("r+b") as source:
            os.fsync(source.fileno())
        if overwrite:
            os.replace(stage, destination)
        else:
            # Atomic no-replace publication, including if another process won a race.
            os.link(stage, destination)
        return destination
    finally:
        stage.unlink(missing_ok=True)


def automatic_backup(data_file: Path, *, reason: str = "daily", force: bool = False) -> Path | None:
    if not data_file.exists():
        return None
    directory = data_file.parent / "backups"
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise BackupError("自动备份目录不能是链接或目录联接。")
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    valid = []
    for path in directory.glob("auto-*.dcalbackup"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            manifest, _data = read_backup(path)
            if manifest.get("reason") in ("daily", "before-migration", "before-restore", "before-update"):
                valid.append((datetime.fromisoformat(manifest["created_at"]), path))
        except (BackupError, OSError):
            continue  # Never delete unknown/broken files or count them as safety copies.
    if not force and any(now - created < timedelta(days=1) for created, _path in valid):
        return None
    path = directory / f"auto-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}.dcalbackup"
    create_backup(data_file.read_bytes(), path, reason=reason)
    # Prune only this controlled directory's verified auto snapshots. Always
    # retain the just-created checkpoint, even if another file has a future date.
    retained = [path] + [old for _, old in sorted(valid, reverse=True)]
    for old in retained[AUTO_KEEP:]:
        old.unlink()
    return path


def restore_store(store, backup: Path, *, apply=None) -> Path:
    """One UI-thread transaction: verified checkpoint, disk swap, memory reload.

    ``apply`` refreshes the owning UI before success is reported. Any failure
    restores the old bytes and old in-memory state; the checkpoint stays usable.
    """
    _manifest, data = read_backup(backup)
    if store.load_error:
        raise BackupError("当前数据读取失败；请先单独保留原文件，未覆盖现有数据。")
    store.save()  # Synchronize the current consistent in-memory state first.
    previous = store.data_file.read_bytes()
    protection = automatic_backup(store.data_file, reason="before-restore", force=True)
    if protection is None:
        raise BackupError("无法创建恢复前保护备份，已取消恢复。")
    stage = _stage_bytes(store.data_file, data)
    old_state = store.__dict__.copy()
    rollback = None
    replaced = False
    try:
        # The regular readers apply exactly the same backwards-compatible rules.
        from calendar_core import Store
        candidate = Store(stage)
        if candidate.load_error:
            raise BackupError(candidate.load_error)
        rollback = _stage_bytes(store.data_file, previous)
        os.replace(stage, store.data_file)
        replaced = True
        candidate.data_file = store.data_file
        store.__dict__.update(candidate.__dict__)
        if apply is not None:
            apply()
        return protection
    except BaseException as exc:
        store.__dict__.clear()
        store.__dict__.update(old_state)
        if replaced:
            try:
                os.replace(rollback, store.data_file)
            except OSError as rollback_error:
                raise BackupError(f"恢复失败且自动回滚受阻。原数据仍保存在：{protection}；请勿继续编辑。{rollback_error}") from exc
        raise
    finally:
        stage.unlink(missing_ok=True)
        if rollback is not None:
            rollback.unlink(missing_ok=True)
