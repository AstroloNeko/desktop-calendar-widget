"""Schema-9 JSON adapter. Store is the ONLY authoritative collection owner."""
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import asdict
import hashlib
import json
import uuid

from calendar_repository import SnapshotToken, RepositoryCommit, RepositoryConflictError
from data_compat import DataCompatibilityError, decode_calendar_data


class JsonCalendarRepository:
    def __init__(self, store):
        self.legacy_store = store  # Explicit transitional bridge; never a second state cache.
        self._session = uuid.uuid4().hex
        self._generation = 0

    def revision(self):
        return SnapshotToken(self._session, self._generation)

    def invalidate_snapshots(self):
        self._generation += 1

    def _get(self, collection, identifier):
        matches = [item for item in getattr(self.legacy_store, collection) if item.id == identifier]
        if len(matches) > 1:
            raise RepositoryConflictError('存在重复 ID，无法确定目标。')
        return deepcopy(matches[0]) if matches else None

    def get_event(self, identifier): return self._get('events', identifier)
    def get_routine(self, identifier): return self._get('routines', identifier)
    def get_category(self, identifier): return self._get('categories', identifier)
    def list_events(self): return deepcopy(self.legacy_store.events)
    def list_routines(self): return deepcopy(self.legacy_store.routines)
    def list_categories(self): return deepcopy(self.legacy_store.sorted_categories())
    def get_date_states(self): return dict(self.legacy_store.date_states)
    def read_preferences(self): return deepcopy(self.legacy_store.settings)

    def read_calendar_snapshot(self):
        return dict(revision=self.revision(), events=self.list_events(), routines=self.list_routines(),
                    categories=self.list_categories(), date_states=self.get_date_states())

    def read_document(self):
        raw = decode_calendar_data(self.legacy_store.data_file.read_bytes(),
                                   supported=self.legacy_store.supported_schema, app_version=self.legacy_store.application_version)
        self._generation += 1  # Reload invalidates session snapshots, including restore.
        return raw

    def write_current(self):
        store = self.legacy_store
        if store.load_error:
            raise DataCompatibilityError(f'当前数据未能安全加载，已拒绝保存：{store.load_error}')
        try:
            disk = store.data_file.read_bytes()
        except FileNotFoundError:
            pass
        else:
            decode_calendar_data(disk, supported=store.supported_schema, app_version=store.application_version)
        if store._migration_backup_pending and store.data_file.exists():
            from data_backup import automatic_backup
            automatic_backup(store.data_file, reason='before-migration', force=True)
            store._migration_backup_pending = False
        store.data_file.parent.mkdir(parents=True, exist_ok=True)
        notifications = sorted(store.notified, key=lambda item: item[-16:])[-600:]
        payload = dict(version=store.supported_schema, events=[asdict(e) for e in store.events],
            categories=[asdict(c) for c in store.sorted_categories()], routines=[asdict(r) for r in store.routines],
            date_states=store.date_states, settings=store.settings, notified=notifications)
        temporary = store.data_file.with_suffix(store.data_file.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(store.data_file)
        store.notified = set(notifications)
        self._generation += 1

    def _disk_digest(self):
        try:
            with self.legacy_store.data_file.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').digest()
        except FileNotFoundError:
            return None

    def apply_changes(self, changes, *, expected_revision=None):
        from calendar_core import Event, RoutineItem, EventCategory, MutationUncertainError
        store = self.legacy_store
        if store.load_error:
            raise DataCompatibilityError(store.load_error)
        if expected_revision is not None and expected_revision != self.revision():
            raise RepositoryConflictError('快照已变化，请重新读取后提交。')
        if not isinstance(changes, Mapping) or set(changes) - {'events','routines','categories','date_states','notified'}:
            raise ValueError('Unsupported prepared change set')
        candidates = {}
        for key, value in changes.items():
            if key in ('events','routines','categories'):
                kind = {'events':Event,'routines':RoutineItem,'categories':EventCategory}[key]
                if not isinstance(value, (list,tuple)) or any(not isinstance(item,kind) for item in value):
                    raise ValueError('Invalid collection')
                identifiers = [item.id for item in value]
                if any(not isinstance(i,str) or not i.strip() for i in identifiers) or len(set(identifiers)) != len(identifiers):
                    raise RepositoryConflictError('变更集合存在空或重复 ID。')
                # Legacy prepared lists share unchanged canonical objects. Only changed
                # records are copied; public detached reads never expose those objects.
                originals = {id(item) for item in getattr(store,key)}
                candidates[key] = [item if id(item) in originals else deepcopy(item) for item in value]
            elif key == 'date_states':
                from datetime import date
                if not isinstance(value,dict): raise ValueError('Invalid date states')
                for day,status in value.items():
                    if not isinstance(day, str): raise ValueError('Invalid date key')
                    date.fromisoformat(day)
                    if status not in ('leave','holiday'): raise ValueError('Invalid date status')
                candidates[key] = dict(value)
            else:
                if not isinstance(value,(set,frozenset)) or any(not isinstance(item,str) for item in value):
                    raise ValueError('Invalid notification state')
                candidates[key] = set(value)
        if all(getattr(store,key) == value for key,value in candidates.items()):
            return RepositoryCommit(False,self.revision())
        original_digest = self._disk_digest()
        before = {key:getattr(store,key) for key in candidates}
        before.setdefault('notified',store.notified)
        for key,value in candidates.items(): setattr(store,key,value)
        try:
            store.save()  # Legacy bridge/hook; delegates back to write_current exactly once.
        except BaseException as exc:
            try: unchanged = self._disk_digest() == original_digest
            except OSError: unchanged = False
            if not unchanged:
                store.load_error = '保存结果待核查，已停止继续写入；请重新加载并核对数据，不要重复操作。'
                raise MutationUncertainError(store.load_error) from exc
            for key,value in before.items(): setattr(store,key,value)
            raise
        return RepositoryCommit(True,self.revision())
