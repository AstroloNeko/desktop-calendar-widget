"""Bounded durable chat/operation journal; separate from calendar JSON.

Every payload is protected for the current Windows user. SQLite FULL-sync WAL
is the intent/result journal; it is never an alternative calendar database.
"""
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from desktop_calendar.assistant.calendar_chat_protocol import ChatError


class ChatJournal:
    def __init__(self, path, protector):
        self.path, self.protector = Path(path), protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise ChatError('incompatible_chat_data', 'AI会话数据版本过新；日历数据未修改。')
            db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, owner TEXT, payload BLOB, PRIMARY KEY(kind,id))')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, identifier, owner):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT owner,payload FROM records WHERE kind=? AND id=?', (kind, identifier)).fetchone()
        if not row or row[0] != owner:
            raise ChatError('authentication_failed', '记录不存在或不属于当前身份。')
        return json.loads(self.protector.decrypt(row[1]).decode('utf-8'))

    def put(self, kind, identifier, owner, payload):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > 512_000:
            raise ChatError('capacity_exceeded', '会话内容过长，请新建会话。')
        encrypted = self.protector.encrypt(raw)
        with self.lock, self.connect() as db:
            # Never overwrite another principal's row, even on a caller-supplied ID collision.
            db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload WHERE owner=excluded.owner',
                       (kind, identifier, owner, encrypted))

    def transition(self, kind, identifier, owner, before, after):
        """Compare state and preview revision inside a single SQLite write transaction."""
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM records WHERE kind=? AND id=? AND owner=?',
                             (kind, identifier, owner)).fetchone()
            current = json.loads(self.protector.decrypt(row[0])) if row else {}
            if not row or any(current.get(key) != value for key, value in before.items()):
                raise ChatError('operation_conflict', '操作已更新，请查看当前结果。')
            raw = json.dumps(after, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > 512_000:
                raise ChatError('capacity_exceeded', '操作记录过长。')
            db.execute('UPDATE records SET payload=? WHERE kind=? AND id=? AND owner=?',
                       (self.protector.encrypt(raw), kind, identifier, owner))

    def all(self, kind, owner):
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT id FROM records WHERE kind=? AND owner=?', (kind, owner)).fetchall()
        return [self.get(kind, row[0], owner) for row in rows]

    def create_session(self, owner):
        if len(self.all('session', owner)) >= 50:
            raise ChatError('capacity_exceeded', '最多保留50个会话，请先清除旧会话。')
        identifier = secrets.token_urlsafe(24)
        session = dict(session_id=identifier, created_at=time.time(), messages=[], selected=None, candidates=[], pending=None, clarification=None)
        self.put('session', identifier, owner, session)
        return session

    def clear_session(self, owner, identifier):
        session = self.get('session', identifier, owner)
        if session['pending']:
            operation = self.get('operation', session['pending'], owner)
            if operation['status'] == 'pending':
                operation['status'] = 'cancelled'
                self.put('operation', operation['operation_id'], owner, operation)
        # Keep terminal operation/request receipts for deduplication, not chat messages.
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM records WHERE kind=? AND id=? AND owner=?', ('session', identifier, owner))

    def check_capacity(self):
        # Fail closed rather than prune safety receipts and allow duplicate execution.
        with self.connect() as db:
            count = db.execute('SELECT count(*) FROM records').fetchone()[0]
        if count >= 10000 or self.path.stat().st_size > 64*1024*1024:
            raise ChatError('capacity_exceeded', 'AI本地记录已达到容量上限，已停止新增操作；日历不受影响。')
