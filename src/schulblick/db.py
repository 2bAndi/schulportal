"""SQLite storage. One writer (the worker), many readers (the web app)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Children as discovered in the portal (id = SPH pupil id).
CREATE TABLE IF NOT EXISTS children (
    id TEXT PRIMARY KEY,
    portal_name TEXT NOT NULL,          -- "Muster, Carlotta Ida"
    first_name TEXT NOT NULL,           -- "Carlotta"
    class_name TEXT,                    -- "G5c"
    updated_at TEXT NOT NULL
);

-- Courses per child, from "Mein Unterricht".
CREATE TABLE IF NOT EXISTS courses (
    child_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    name TEXT NOT NULL,
    norm TEXT NOT NULL,
    teacher_code TEXT,
    teacher_name TEXT,
    open_excuses INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (child_id, book_id)
);

-- Lesson entries incl. homework ("Aktuelle Einträge").
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,                -- child:book:entry
    child_id TEXT NOT NULL,
    course TEXT NOT NULL,
    teacher_code TEXT,
    topic TEXT,
    lesson_date TEXT,                   -- ISO date
    content TEXT,
    homework TEXT,
    homework_done INTEGER,              -- NULL = no homework
    hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    child_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,                 -- JSON: {"columns": [...], "rows": [...], "totals": {...}}
    hash TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS substitutions (
    id TEXT PRIMARY KEY,                -- hash of day + row
    day TEXT NOT NULL,                  -- ISO date
    hours TEXT, classes TEXT, substitute TEXT, teacher TEXT, kind TEXT,
    subject TEXT, room TEXT, note TEXT,
    child_ids TEXT NOT NULL,            -- JSON list
    certainty TEXT NOT NULL,            -- JSON {child_id: "sure"|"maybe"}
    active INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_days (
    day TEXT PRIMARY KEY,
    week TEXT,
    infos TEXT NOT NULL,                -- JSON list of {title, text}
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,                -- portal event id
    title TEXT NOT NULL,
    start TEXT NOT NULL,                -- ISO datetime or date
    end TEXT,
    all_day INTEGER NOT NULL,
    category TEXT,
    kind TEXT NOT NULL,                 -- exam | test | holiday | week | general
    location TEXT,
    description TEXT,
    group_name TEXT,
    child_ids TEXT NOT NULL,            -- JSON list, [] = nobody, ["*"] = whole family
    parents_only INTEGER NOT NULL DEFAULT 0,
    hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timetables (
    child_id TEXT PRIMARY KEY,
    week TEXT,
    data TEXT NOT NULL,                 -- JSON
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,                -- portal "Uniquid"
    subject TEXT NOT NULL,
    sender TEXT,
    sent_at TEXT NOT NULL,              -- ISO datetime (latest activity)
    recipients TEXT,                    -- readable list
    groups TEXT,                        -- readable list of group recipients
    body TEXT,                          -- plain text, NULL when not fetched
    child_ids TEXT NOT NULL,            -- JSON list of children it concerns, ["*"] = all
    kid_ids TEXT NOT NULL,              -- JSON list of children who may see it, ["*"] = all
    first_seen TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,                 -- parent | child
    child_first_name TEXT,
    password_hash TEXT,
    totp_secret TEXT,
    prev_visit TEXT,
    last_visit TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS login_failures (
    key TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key, at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    message TEXT
);
CREATE INDEX IF NOT EXISTS sync_runs_area ON sync_runs(area, started_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    # Each connection is used by one job/request at a time, but those may hop threads
    # (APScheduler pool, FastAPI threadpool), so the same-thread check is disabled.
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default=None):
    if value is None:
        return default
    return json.loads(value)
