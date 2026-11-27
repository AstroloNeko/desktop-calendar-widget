"""SQLite adapter; desktop backend selection is owned by the storage lifecycle.

Reads are detached snapshots of the owning Store at its loaded revision. Call
Store.load() explicitly to observe other writers; every commit checks that loaded
revision inside BEGIN IMMEDIATE. There is no second mutable object cache.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from desktop_calendar.storage.calendar_repository import RepositoryCommit, RepositoryConflictError, SnapshotToken
from data_compat import DataCompatibilityError
from desktop_calendar.storage.json_calendar_repository import JsonCalendarRepository
from desktop_calendar.storage.sqlite_calendar_schema import SCHEMA_VERSION, APPLICATION_ID, COLUMNS, statements, preference_scope


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class SqliteCalendarRepository(JsonCalendarRepository):
    """Reuse detached read/structural validation only, never JSON I/O/transactions."""
    supports_json_backup = False

    def __init__(self, store, *, busy_timeout_ms=250):
        if getattr(store, 'repository', None) is not None:
            raise ValueError('A Store already owns a repository; construct a separate Store instead.')
        self.legacy_store = store
        self.path = Path(store.data_file)
        if self.path.suffix.lower() not in ('.db', '.sqlite', '.sqlite3'):
            raise ValueError('SQLite requires an explicit .db/.sqlite/.sqlite3 path; JSON import is not supported.')
        self.busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._loaded = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect()) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    db.execute('BEGIN IMMEDIATE')
                    # Recheck after obtaining the writer lock (concurrent initializer).
                    if db.execute('PRAGMA user_version').fetchone()[0] == 0:
                        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                            raise DataCompatibilityError('未识别的 SQLite 数据库，拒绝初始化。')
                        for sql in statements(): db.execute(sql)
                        db.execute('INSERT INTO calendars VALUES(?,?,?,0)', (uuid.uuid4().hex, 'Local Calendar', 'local-naive'))
                        db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (SCHEMA_VERSION,))
                        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                    db.commit()
                self._validate(db)
                db.execute('PRAGMA journal_mode=WAL')
        except sqlite3.DatabaseError as exc:
            raise DataCompatibilityError('SQLite 日历无法安全打开：' + str(exc)) from exc

    def _connect(self):
        if self._closed: raise RuntimeError('Repository is closed')
        db = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            db.close()
            raise
        return db

    def _validate(self, db):
        if (db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION or
                db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID):
            raise DataCompatibilityError('SQLite Schema 不兼容，未执行降级或导入。')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise DataCompatibilityError('SQLite 完整性检查失败。')
        if db.execute('SELECT count(*) FROM calendars').fetchone()[0] != 1:
            raise DataCompatibilityError('仅支持一个本地 Calendar。')

    def _revision(self, db):
        if (db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION or
                db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID):
            raise DataCompatibilityError('SQLite Schema 已变化，拒绝读取或提交。')
        row = db.execute('SELECT id,revision FROM calendars').fetchone()
        return SnapshotToken(row['id'], row['revision'])

    def revision(self):
        if self._closed: raise RuntimeError('Repository is closed')
        return self._loaded

    def invalidate_snapshots(self):
        raise RuntimeError('SQLite requires explicit reload; arbitrary generation changes are not supported.')

    def close(self):
        # Each transaction owns/closes its connection; no connection crosses threads.
        self._closed = True

    def _get(self, collection, identifier):
        self.revision()
        return super()._get(collection, identifier)

    def _document(self, db):
        raw = dict(version=9, date_states={}, settings={}, notified=[])
        for table, columns in COLUMNS.items():
            raw[table] = [{name: row[name] for name in columns}
                          for row in db.execute(f'SELECT * FROM {table} ORDER BY order_key,id')]
        bools = {'done','has_time','skip_non_working_days','end_as_ddl','enabled','reminder_enabled','workdays_only'}
        for table in ('events','routines'):
            for row in raw[table]:
                for key in bools & row.keys(): row[key] = bool(row[key])
        for row in raw['routines']:
            for table,key,field in (('routine_weekdays','weekday','weekdays'),
                                    ('habit_completions','occurrence_date','habit_done')):
                row[field] = [r[0] for r in db.execute(f'SELECT {key} FROM {table} WHERE routine_id=? ORDER BY order_key', (row['id'],))]
            row['schedule_history'] = [json.loads(r[0]) for r in db.execute(
                'SELECT rule_json FROM habit_schedule_history WHERE routine_id=? ORDER BY order_key', (row['id'],))]
        raw['date_states'] = dict(db.execute('SELECT day,state FROM date_states'))
        raw['settings'] = {r[0]: json.loads(r[1]) for r in db.execute('SELECT key,value_json FROM preferences')}
        raw['notified'] = [r[0] for r in db.execute('SELECT plan_key FROM reminder_delivery_state')]
        return raw

    def read_document(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            token = self._revision(db)
            raw = self._document(db)
            db.commit()
        # Strict round-trip validation before Store's tolerant legacy loader.
        from calendar_core import Event, RoutineItem, EventCategory, DEFAULT_SETTINGS
        for table, model in (('events',Event),('routines',RoutineItem),('categories',EventCategory)):
            for row in raw[table]:
                if asdict(model.from_dict(row)) != row:
                    raise DataCompatibilityError('SQLite 业务记录需要修复，拒绝静默归一化。')
        self._loaded = token
        # Store.load historically merges date overrides: clear only after a valid snapshot.
        self.legacy_store.date_states = {}
        self.legacy_store.settings = deepcopy(DEFAULT_SETTINGS)
        return raw

    def write_current(self):
        """Legacy save may persist auxiliary states, NEVER replace domain collections."""
        store = self.legacy_store
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            if self._revision(db) != self._loaded:
                raise RepositoryConflictError('数据库已变化，请重新加载。')
            raw = self._document(db)
        for table in COLUMNS:
            current = store.sorted_categories() if table == 'categories' else getattr(store, table)
            expected_rows = raw[table]
            if table == 'categories':
                expected_rows = sorted(expected_rows, key=lambda c:(c['sort_order'],c['created_at'],c['id']))
            if [asdict(item) for item in current] != expected_rows:
                raise RepositoryConflictError('SQLite 不支持通过 Store.save 修改业务对象，请使用 Application Service。')
        if store.date_states != raw['date_states']:
            raise RepositoryConflictError('日期状态必须通过 Application Service 提交。')
        try:
            self._apply({'notified': set(store.notified)}, preferences=deepcopy(store.settings))
        except sqlite3.Error as exc:
            raise OSError('SQLite 辅助状态保存失败：' + str(exc)) from exc

    def write_reminder_preference(self, key, value):
        """Update one auxiliary key without loading and validating the entire calendar."""
        try:
            self._apply({}, preferences={key: deepcopy(value)}, replace_preferences=False)
        except sqlite3.Error as exc:
            raise OSError('SQLite 提醒状态保存失败：' + str(exc)) from exc

    def write_preferences(self, updates):
        """Persist selected preference keys without reading or comparing full collections."""
        try:
            return self._apply({}, preferences=deepcopy(dict(updates)), replace_preferences=False)
        except sqlite3.Error as exc:
            raise OSError('SQLite 偏好设置保存失败：' + str(exc)) from exc

    def apply_changes(self, changes, *, expected_revision=None, operation_id=None, preference_updates=None):
        try:
            return self._apply(changes, expected_revision=expected_revision, operation_id=operation_id,
                               preferences=preference_updates, replace_preferences=False)
        except sqlite3.IntegrityError as exc:
            raise ValueError('SQLite 数据约束未满足：' + str(exc)) from exc
        except sqlite3.Error as exc:
            raise OSError('SQLite 存储操作未完成：' + str(exc)) from exc

    def import_snapshot(self, changes, preferences, *, source_hash):
        """B3a only: validated complete snapshot into an unused isolated database.

        No defaults/settings merge, no history generation, no notification pruning.
        Schema creation precedes this one atomic data/receipt transaction.
        """
        if len(source_hash) != 64 or any(c not in '0123456789abcdef' for c in source_hash):
            raise ValueError('Invalid source hash')
        if set(changes) != {'events','routines','categories','date_states','notified'} or not isinstance(preferences,dict):
            raise ValueError('A complete validated import snapshot is required')
        return self._apply(changes, preferences=deepcopy(preferences),
                           operation_id='migration:'+source_hash, initial_import=True)

    def _commit(self, db):
        db.commit()  # Narrow fault-injection point, not a second persistence path.

    def receipt(self, operation_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM operation_receipts WHERE operation_id=?', (operation_id,)).fetchone()
            return dict(row) if row else None

    def existing_event_ids(self, identifiers):
        """Read the frozen target IDs from SQLite without updating the Store revision."""
        identifiers = tuple(identifiers)
        if not identifiers:
            return set()
        placeholders = ','.join('?' for _ in identifiers)
        with closing(self._connect()) as db:
            return {row[0] for row in db.execute(
                'SELECT id FROM events WHERE id IN ('+placeholders+')', identifiers)}

    def _apply(self, changes, *, expected_revision=None, operation_id=None, preferences=None, initial_import=False,
               replace_preferences=True):
        from calendar_core import MutationUncertainError
        store = self.legacy_store
        if store.load_error: raise DataCompatibilityError(store.load_error)
        candidates = self._prepare_changes(changes)
        if operation_id is not None and (not isinstance(operation_id,str) or not 1 <= len(operation_id) <= 200):
            raise ValueError('Invalid operation_id')
        explicit_id = operation_id is not None
        operation_id = operation_id or uuid.uuid4().hex
        request = {}
        for key,val in candidates.items():
            if key in COLUMNS:
                old = {e.id:e for e in getattr(store,key)}
                # Explicit replay keys hash the complete supplied request. Internal
                # one-shot IDs need only a delta + base revision for outcome proof.
                request[key] = ([asdict(e) for e in val] if explicit_id else
                    dict(order=[e.id for e in val], changed=[asdict(e) for e in val if old.get(e.id) != e],
                         base=self._loaded.generation))
            else:
                request[key] = sorted(val) if isinstance(val,set) else val
        if preferences is not None:
            request['preferences' if replace_preferences else 'preference_updates'] = preferences
        digest = hashlib.sha256(encoded(request).encode()).hexdigest()
        expected = self._loaded if expected_revision is None else expected_revision
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            committing = False
            try:
                if initial_import:
                    # Also reject a pre-populated revision-0 file; never overwrite it.
                    tables = (*COLUMNS,'date_states','preferences','reminder_delivery_state','operation_receipts')
                    if self._revision(db).generation != 0 or any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in tables):
                        raise RepositoryConflictError('迁移导入仅允许全新空数据库。')
                previous = db.execute('SELECT * FROM operation_receipts WHERE operation_id=?', (operation_id,)).fetchone()
                if previous:
                    if previous['request_hash'] != digest:
                        raise RepositoryConflictError('operation_id 已用于不同操作。')
                    db.rollback()
                    # Do not republish stale candidates; reload actual current committed state.
                    store.load()
                    if store.load_error: raise DataCompatibilityError(store.load_error)
                    return RepositoryCommit(bool(previous['changed']), SnapshotToken(self._loaded.session, previous['committed_revision']), operation_id, True)
                actual = self._revision(db)
                if actual != expected or actual != self._loaded:
                    raise RepositoryConflictError('SQLite Revision 冲突，请重新加载后重新预览。')
                changed = initial_import  # Empty calendars still get a source-bound receipt.
                new_revision = actual.generation + 1
                # Deferred FK allows category deletion + event unlink as ONE transaction.
                for table in ('categories','events','routines'):
                    if table in candidates:
                        options = {'preserve_order':True} if initial_import else {}
                        changed |= self._sync_records(db, table, candidates[table], actual.session, new_revision, **options)
                if 'date_states' in candidates:
                    changed |= self._sync_pairs(db, 'date_states', 'day', 'state', candidates['date_states'], actual.session)
                if 'notified' in candidates:
                    old = {r[0] for r in db.execute('SELECT plan_key FROM reminder_delivery_state')}
                    new = candidates['notified']
                    for key in old-new: db.execute('DELETE FROM reminder_delivery_state WHERE plan_key=?', (key,))
                    for key in new-old: db.execute('INSERT INTO reminder_delivery_state VALUES(?,?)', (actual.session,key))
                    changed |= old != new
                if preferences is not None:
                    new = {key: encoded(val) for key, val in preferences.items()}
                    if replace_preferences:
                        old = {r[0]: r[1] for r in db.execute('SELECT key,value_json FROM preferences')}
                        for key in old.keys() - new.keys():
                            db.execute('DELETE FROM preferences WHERE key=?', (key,))
                    else:
                        old = {key: row[0] for key in new
                               if (row := db.execute('SELECT value_json FROM preferences WHERE key=?', (key,)).fetchone())}
                    for key,val in new.items():
                        if old.get(key) != val:
                            db.execute('INSERT INTO preferences VALUES(?,?,?,?) ON CONFLICT(calendar_id,key) DO UPDATE SET value_json=excluded.value_json,scope=excluded.scope',
                                       (actual.session,preference_scope(key),key,val))
                    changed |= old != new
                if not changed:
                    db.rollback()
                    return RepositoryCommit(False, actual)
                db.execute('UPDATE calendars SET revision=? WHERE id=?', (new_revision, actual.session))
                affected = {}
                for key,values in candidates.items():
                    if key in COLUMNS:
                        old = {e.id:e for e in getattr(store,key)}
                        new = {e.id:e for e in values}
                        affected[key] = sorted(identifier for identifier in old.keys() | new.keys()
                                               if old.get(identifier) != new.get(identifier))
                db.execute('INSERT INTO operation_receipts(operation_id,request_hash,committed_revision,changed,operation_type,affected_ids) VALUES(?,?,?,?,?,?)',
                           (operation_id,digest,new_revision,1,','.join(sorted(request)),encoded(affected)))
                committing = True
                self._commit(db)
            except BaseException as exc:
                if db.in_transaction:
                    try:
                        db.rollback()
                    except sqlite3.Error as rollback_error:
                        store.load_error = 'SQLite 回滚无法确认，已阻止后续写入。'
                        raise MutationUncertainError(store.load_error) from rollback_error
                    raise
                if not committing:
                    raise
                # Commit may have succeeded before an exception. Verify durable receipt,
                # never replay SQL automatically and never use JSON file digests.
                try: proof = self.receipt(operation_id)
                except (sqlite3.Error, OSError): proof = None
                if not proof or proof['request_hash'] != digest:
                    store.load_error = 'SQLite 提交结果待核查，已阻止后续写入。'
                    raise MutationUncertainError(store.load_error) from exc
                new_revision = proof['committed_revision']
        for key,value in candidates.items(): setattr(store,key,value)
        if preferences is not None:
            store.settings = preferences if replace_preferences else {**store.settings, **preferences}
        self._loaded = SnapshotToken(actual.session,new_revision)
        return RepositoryCommit(True,self._loaded,operation_id)

    def _sync_records(self, db, table, items, calendar_id, revision, *, preserve_order=False):
        columns = tuple(COLUMNS[table])
        # Safe only after the transaction has matched the Store's loaded revision.
        # Avoid scanning/deserializing every database row for one object edit.
        previous = self.legacy_store.sorted_categories() if table == 'categories' else getattr(self.legacy_store,table)
        old = {item.id:(order,item) for order,item in enumerate(previous)}
        ids = {item.id for item in items}
        changed = False
        for identifier in old.keys()-ids:
            db.execute(f'DELETE FROM {table} WHERE id=? AND calendar_id=?',(identifier,calendar_id))
            changed = True
        # Categories are exposed in established sort_order order after reload.
        if table == 'categories' and not preserve_order: items = sorted(items, key=lambda c:(c.sort_order,c.created_at,c.id))
        for order,item in enumerate(items):
            prior = old.get(item.id)
            if prior is not None and prior[0] == order and prior[1] == item:
                continue
            row = asdict(item)
            names = ('calendar_id',) + columns + ('order_key','revision')
            sql_names = ','.join('"'+name+'"' for name in names)
            updates = ','.join(f'"{name}"=excluded."{name}"' for name in names if name not in ('id','calendar_id'))
            db.execute(f'INSERT INTO {table}({sql_names}) VALUES({",".join("?" for _ in names)}) ON CONFLICT(calendar_id,id) DO UPDATE SET {updates}',
                       (calendar_id,)+(tuple(row[k] for k in columns))+(order,revision))
            changed = True
            if table == 'routines':
                for child,field,key in (('routine_weekdays','weekdays','weekday'),('habit_completions','habit_done','occurrence_date'),('habit_schedule_history','schedule_history','until_date')):
                    values = row[field]
                    old_values = db.execute(f'SELECT * FROM {child} WHERE routine_id=? ORDER BY order_key',(item.id,)).fetchall()
                    existing = [json.loads(r['rule_json']) if field=='schedule_history' else r[key] for r in old_values]
                    if existing == values: continue
                    db.execute('UPDATE routines SET revision=? WHERE calendar_id=? AND id=?',
                               (revision,calendar_id,item.id))
                    db.execute(f'DELETE FROM {child} WHERE routine_id=?',(item.id,))
                    for n,value in enumerate(values):
                        if field == 'schedule_history':
                            db.execute(f'INSERT INTO {child} VALUES(?,?,?,?,?)',(calendar_id,item.id,value['until'],encoded(value),n))
                        else: db.execute(f'INSERT INTO {child} VALUES(?,?,?,?)',(calendar_id,item.id,value,n))
                    changed = True
        return changed

    def _sync_pairs(self, db, table, key_col, value_col, values, calendar_id):
        old = dict(db.execute(f'SELECT {key_col},{value_col} FROM {table}'))
        for key in old.keys()-values.keys(): db.execute(f'DELETE FROM {table} WHERE {key_col}=?',(key,))
        for key,value in values.items():
            if old.get(key) != value:
                db.execute(f'INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(calendar_id,{key_col}) DO UPDATE SET {value_col}=excluded.{value_col}',(calendar_id,key,value))
        return old != values

    def backup_to(self, destination):
        """Create a new consistent DB, including WAL, without touching source state."""
        destination = Path(destination)
        # Reserve exclusively; never overwrite a previous backup or the source.
        with destination.open('xb'): pass
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                self._validate(target)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
