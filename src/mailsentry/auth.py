from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SETUP_MINUTES = 15
IDLE_MINUTES = 30
SESSION_HOURS = 12
FAILURE_WINDOW_MINUTES = 15
MAX_FAILURES = 5


@dataclass(frozen=True, slots=True)
class AdminSession:
    token: str
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime


class AuthManager:
    """Own setup, password verification, TOTP, throttling, and server sessions."""

    def __init__(self, database: Path, totp_secret_file: Path) -> None:
        self.database = database
        self.totp_secret_file = totp_secret_file
        self.hasher = PasswordHasher()
        self.initialize()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(AUTH_SCHEMA)

    def needs_setup(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM admin_auth WHERE id = 1").fetchone() is None

    def issue_setup_token(self, now: datetime | None = None) -> str | None:
        if not self.needs_setup():
            return None
        raw = secrets.token_urlsafe(32)
        current = now or datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("DELETE FROM admin_setup_tokens")
            connection.execute(
                "INSERT INTO admin_setup_tokens(token_hash, expires_at) VALUES (?, ?)",
                (self._digest(raw), (current + timedelta(minutes=SETUP_MINUTES)).isoformat()),
            )
        return raw

    def complete_setup(
        self, token: str, password: str, now: datetime | None = None
    ) -> bool:
        if len(password) < 12 or len(password) > 128:
            raise ValueError("password must contain 12 to 128 characters")
        current = now or datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM admin_setup_tokens WHERE token_hash = ?",
                (self._digest(token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(row[0]) < current:
                return False
            connection.execute(
                """
                INSERT INTO admin_auth(id, password_hash, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    updated_at = excluded.updated_at
                """,
                (self.hasher.hash(password), current.isoformat()),
            )
            connection.execute("DELETE FROM admin_setup_tokens")
        return True

    def login(
        self,
        password: str,
        totp_code: str,
        remote: str,
        now: datetime | None = None,
    ) -> AdminSession | None:
        current = now or datetime.now(UTC)
        if self._is_locked(remote, current):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM admin_auth WHERE id = 1"
            ).fetchone()
        valid = False
        if row is not None:
            try:
                valid = self.hasher.verify(str(row[0]), password)
            except VerifyMismatchError:
                valid = False
        totp_secret = self._read_totp_secret()
        if valid and totp_secret:
            valid = pyotp.TOTP(totp_secret).verify(
                totp_code,
                for_time=current,
                valid_window=1,
            )
        if not valid:
            self._record_failure(remote, current)
            return None
        self._clear_failures(remote)
        return self._new_session(current)

    def authenticate(
        self, token: str | None, now: datetime | None = None
    ) -> AdminSession | None:
        if not token:
            return None
        current = now or datetime.now(UTC)
        digest = self._digest(token)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT csrf_token, created_at, last_seen_at
                FROM admin_sessions WHERE token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if row is None:
                return None
            created = datetime.fromisoformat(row[1])
            last_seen = datetime.fromisoformat(row[2])
            if (
                current - last_seen > timedelta(minutes=IDLE_MINUTES)
                or current - created > timedelta(hours=SESSION_HOURS)
            ):
                connection.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?", (digest,)
                )
                return None
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (current.isoformat(), digest),
            )
        return AdminSession(token, str(row[0]), created, current)

    def logout(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?", (self._digest(token),)
            )

    def sessions(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT token_hash, created_at, last_seen_at
                FROM admin_sessions ORDER BY last_seen_at DESC
                """
            ).fetchall()
        return [
            {
                "id": str(row[0])[:12],
                "created_at": str(row[1]),
                "last_seen_at": str(row[2]),
            }
            for row in rows
        ]

    def revoke_other_sessions(self, current_token: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE token_hash != ?",
                (self._digest(current_token),),
            )
            return int(cursor.rowcount)

    def reset(self) -> str:
        password = secrets.token_urlsafe(18)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_auth(id, password_hash, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    updated_at = excluded.updated_at
                """,
                (self.hasher.hash(password), now),
            )
            connection.execute("DELETE FROM admin_sessions")
        self.disable_totp()
        return password

    def begin_totp(self) -> tuple[str, str]:
        secret = pyotp.random_base32()
        return secret, pyotp.TOTP(secret).provisioning_uri(
            name="admin", issuer_name="MailSentry"
        )

    def enable_totp(
        self,
        secret: str,
        code: str,
        now: datetime | None = None,
    ) -> bool:
        if not pyotp.TOTP(secret).verify(
            code,
            for_time=now,
            valid_window=1,
        ):
            return False
        self.totp_secret_file.parent.mkdir(parents=True, exist_ok=True)
        self.totp_secret_file.write_text(secret, encoding="utf-8")
        with suppress(OSError):
            self.totp_secret_file.chmod(0o600)
        return True

    def disable_totp(self) -> None:
        self.totp_secret_file.unlink(missing_ok=True)

    def totp_enabled(self) -> bool:
        return bool(self._read_totp_secret())

    def _new_session(self, now: datetime) -> AdminSession:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions(token_hash, csrf_token, created_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (self._digest(token), csrf, now.isoformat(), now.isoformat()),
            )
        return AdminSession(token, csrf, now, now)

    def _is_locked(self, remote: str, now: datetime) -> bool:
        cutoff = (now - timedelta(minutes=FAILURE_WINDOW_MINUTES)).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_login_failures WHERE attempted_at < ?", (cutoff,)
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM admin_login_failures WHERE remote = ?",
                (remote,),
            ).fetchone()[0]
        return int(count) >= MAX_FAILURES

    def _record_failure(self, remote: str, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO admin_login_failures(remote, attempted_at) VALUES (?, ?)",
                (remote, now.isoformat()),
            )

    def _clear_failures(self, remote: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_login_failures WHERE remote = ?", (remote,)
            )

    def _read_totp_secret(self) -> str:
        try:
            return self.totp_secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()


AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_auth (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    password_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_setup_tokens (
    token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_login_failures
ON admin_login_failures(remote, attempted_at);
"""
