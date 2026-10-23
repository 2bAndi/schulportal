"""Accounts, sessions, login throttling and TOTP."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .. import db
from ..config import Settings

_hasher = PasswordHasher()
# A real hash so that unknown usernames take the same time as wrong passwords.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

COOKIE_NAME = "schulblick_session"
MAX_FAILURES_PER_USER = 5      # within 15 minutes
MAX_FAILURES_PER_IP = 20       # within 15 minutes
FAILURE_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    child_first_name: str | None
    has_totp: bool
    prev_visit: str | None

    @property
    def is_parent(self) -> bool:
        return self.role == "parent"


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        child_first_name=row["child_first_name"],
        has_totp=bool(row["totp_secret"]),
        prev_visit=row["prev_visit"],
    )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def sync_users(conn: sqlite3.Connection, settings: Settings) -> None:
    """Make the users table match [[users]] in the config. Passwords are kept."""
    names = [u.username for u in settings.users]
    with db.transaction(conn):
        for u in settings.users:
            conn.execute(
                "INSERT INTO users(username, display_name, role, child_first_name, created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name, role=excluded.role, "
                "child_first_name=excluded.child_first_name",
                (u.username, u.display_name, u.role, u.child, db.utcnow()),
            )
        placeholders = ",".join("?" * len(names)) or "''"
        conn.execute(f"DELETE FROM users WHERE username NOT IN ({placeholders})", names)


def set_password(conn: sqlite3.Connection, username: str, password: str) -> bool:
    if len(password) < 8:
        raise ValueError("password must have at least 8 characters")
    cur = conn.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(password), username.lower()))
    conn.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username.lower(),))
    return cur.rowcount == 1


def set_totp(conn: sqlite3.Connection, username: str, secret: str | None) -> bool:
    cur = conn.execute("UPDATE users SET totp_secret=? WHERE username=?", (secret, username.lower()))
    return cur.rowcount == 1


# --------------------------------------------------------------------------- throttling


def _recent_failures(conn: sqlite3.Connection, key: str) -> int:
    since = (datetime.now(timezone.utc) - FAILURE_WINDOW).isoformat(timespec="seconds")
    return conn.execute("SELECT COUNT(*) FROM login_failures WHERE key=? AND at>=?", (key, since)).fetchone()[0]


def is_throttled(conn: sqlite3.Connection, username: str, ip: str) -> bool:
    return (_recent_failures(conn, f"user:{username.lower()}") >= MAX_FAILURES_PER_USER
            or _recent_failures(conn, f"ip:{ip}") >= MAX_FAILURES_PER_IP)


def record_failure(conn: sqlite3.Connection, username: str, ip: str) -> None:
    now = db.utcnow()
    conn.execute("INSERT INTO login_failures(key, at) VALUES(?,?), (?,?)",
                 (f"user:{username.lower()}", now, f"ip:{ip}", now))


def clear_failures(conn: sqlite3.Connection, username: str) -> None:
    conn.execute("DELETE FROM login_failures WHERE key=?", (f"user:{username.lower()}",))


# --------------------------------------------------------------------------- login


@dataclass
class LoginResult:
    user: User | None
    needs_totp: bool = False
    error: str | None = None


def check_credentials(conn: sqlite3.Connection, settings: Settings, username: str, password: str,
                      totp_code: str | None) -> LoginResult:
    row = conn.execute("SELECT * FROM users WHERE username=?", (username.lower().strip(),)).fetchone()
    stored = row["password_hash"] if row is not None and row["password_hash"] else _DUMMY_HASH
    try:
        _hasher.verify(stored, password)
        password_ok = row is not None and bool(row["password_hash"])
    except (VerificationError, InvalidHashError):
        password_ok = False
    if not password_ok:
        return LoginResult(None, error="Name oder Passwort stimmt nicht.")

    if row["totp_secret"]:
        if not totp_code:
            return LoginResult(None, needs_totp=True)
        if not pyotp.TOTP(row["totp_secret"]).verify(totp_code.replace(" ", ""), valid_window=1):
            return LoginResult(None, needs_totp=True, error="Der Code aus der Authenticator-App stimmt nicht.")
    elif row["role"] == "parent" and settings.require_totp_for_parents:
        return LoginResult(None, error="Für Eltern-Konten ist noch kein zweiter Faktor eingerichtet. "
                                       "Bitte zuerst `schulblick totp-setup` auf dem Server ausführen.")
    return LoginResult(_row_to_user(row))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, settings: Settings, user: User, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent) VALUES(?,?,?,?,?)",
        (_token_hash(token), user.id, now.isoformat(timespec="seconds"),
         (now + timedelta(days=settings.session_days)).isoformat(timespec="seconds"), user_agent[:200]),
    )
    return token


def user_for_token(conn: sqlite3.Connection, token: str | None) -> User | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at > ?",
        (_token_hash(token), db.utcnow()),
    ).fetchone()
    return _row_to_user(row) if row else None


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def touch_visit(conn: sqlite3.Connection, user: User) -> str | None:
    """Advance prev_visit when the last visit is older than 30 minutes. Returns the 'new since' marker."""
    row = conn.execute("SELECT prev_visit, last_visit FROM users WHERE id=?", (user.id,)).fetchone()
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(row["last_visit"]) if row["last_visit"] else None
    prev = row["prev_visit"]
    if last is None or now - last > timedelta(minutes=30):
        prev = row["last_visit"]
        conn.execute("UPDATE users SET prev_visit=?, last_visit=? WHERE id=?",
                     (prev, now.isoformat(timespec="seconds"), user.id))
    else:
        conn.execute("UPDATE users SET last_visit=? WHERE id=?", (now.isoformat(timespec="seconds"), user.id))
    return prev
