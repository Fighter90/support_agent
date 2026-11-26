"""Хранилище состояния: диалоги, сообщения, эскалации, КП, база знаний, журнал вызовов.

SQLite в режиме WAL: одна ВМ, сотни обращений в месяц — отдельный сервер БД не нужен.
Каталог товаров лежит в отдельном файле catalog.db (см. catalog.py), чтобы его можно было
пересобирать из фида и подменять атомарно, не трогая диалоги.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import get_settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,
  status TEXT DEFAULT 'active',          -- active | escalated | answered | closed
  scenario TEXT DEFAULT '',              -- known_product | need_selection | question | other
  page_url TEXT DEFAULT '', title TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, content TEXT,
  meta TEXT DEFAULT '{}', created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conv_id, id);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, reason TEXT, category TEXT, summary TEXT,
  status TEXT DEFAULT 'open', answer TEXT DEFAULT '', manager TEXT DEFAULT '', add_to_kb INTEGER DEFAULT 0,
  created_at REAL, answered_at REAL
);
CREATE TABLE IF NOT EXISTS quotes (
  id TEXT PRIMARY KEY, number TEXT, conv_id TEXT, items TEXT, total REAL, vat REAL,
  customer TEXT DEFAULT '{}', status TEXT DEFAULT 'awaiting_requisites', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS kb (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, source TEXT, url TEXT DEFAULT '', created_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, body, content='kb', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb BEGIN
  INSERT INTO kb_fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
CREATE TRIGGER IF NOT EXISTS kb_ad AFTER DELETE ON kb BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body); END;
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, tool TEXT, args TEXT, result TEXT, ms INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, provider TEXT, model TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_rub REAL, ms INTEGER, ok INTEGER, error TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS pii (
  conv_id TEXT, token TEXT, value TEXT, kind TEXT, PRIMARY KEY (conv_id, token)
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, message_id INTEGER, rating INTEGER, created_at REAL
);
"""


def _connect() -> sqlite3.Connection:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(s.app_db, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    return con


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    con = _connect()
    try:
        with _lock:
            yield con
            con.commit()
    finally:
        con.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def now() -> float:
    return time.time()


def new_id(prefix: str = '') -> str:
    return prefix + uuid.uuid4().hex[:12]


def rows(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid or 0


def dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def loads(x: str | None, default: Any = None) -> Any:
    if not x:
        return default
    try:
        return json.loads(x)
    except ValueError:
        return default
