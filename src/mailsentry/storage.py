from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailsentry.models import Assessment, MailCandidate


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def get_cursor(self, account_id: str) -> tuple[int, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT uid_validity, last_uid FROM account_cursors WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def set_cursor(self, account_id: str, uid_validity: int, last_uid: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_cursors(account_id, uid_validity, last_uid, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    uid_validity = excluded.uid_validity,
                    last_uid = excluded.last_uid,
                    updated_at = excluded.updated_at
                """,
                (account_id, uid_validity, last_uid, _now()),
            )

    def save_assessment(self, candidate: MailCandidate, assessment: Assessment) -> tuple[int, bool]:
        digest = (
            hashlib.sha256(candidate.message_id.encode()).hexdigest()
            if candidate.message_id
            else None
        )
        attachments = json.dumps(
            [asdict(attachment) for attachment in candidate.attachments], ensure_ascii=False
        )
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM mail_events
                WHERE account_id = ? AND (
                    (uid_validity = ? AND uid = ?)
                    OR (? IS NOT NULL AND message_id_hash = ?)
                )
                """,
                (
                    candidate.account_id,
                    candidate.uid_validity,
                    candidate.uid,
                    digest,
                    digest,
                ),
            ).fetchone()
            if existing:
                return int(existing[0]), False
            cursor = connection.execute(
                """
                INSERT INTO mail_events(
                    account_id, uid, uid_validity, message_id_hash, received_at,
                    sender, subject, attachments_json, score, alert_level,
                    reasons_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.account_id,
                    candidate.uid,
                    candidate.uid_validity,
                    digest,
                    candidate.received_at.isoformat(),
                    candidate.sender,
                    candidate.subject,
                    attachments,
                    assessment.score,
                    assessment.level.value,
                    json.dumps(assessment.reasons, ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cursor.lastrowid), True

    def queue_notification(self, event_id: int, title: str, body: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox(event_id, title, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, title, body, _now()),
            )

    def pending_notifications(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, event_id, title, body, attempts
                    FROM notification_outbox
                    WHERE delivered_at IS NULL
                    ORDER BY id LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def mark_notification(self, outbox_id: int, delivered: bool) -> None:
        with self._connect() as connection:
            if delivered:
                connection.execute(
                    "UPDATE notification_outbox SET delivered_at = ? WHERE id = ?",
                    (_now(), outbox_id),
                )
            else:
                connection.execute(
                    "UPDATE notification_outbox SET attempts = attempts + 1 WHERE id = ?",
                    (outbox_id,),
                )

    def sender_feedback(self, sender: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(adjustment), 0) FROM feedback WHERE sender = ?",
                (sender.lower(),),
            ).fetchone()
        return max(-30, min(30, int(row[0])))

    def add_feedback(self, event_id: int, important: bool) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sender FROM mail_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise LookupError(f"event {event_id} does not exist")
            sender = str(row[0]).lower()
            connection.execute(
                """
                INSERT INTO feedback(event_id, sender, adjustment, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, sender, 20 if important else -15, _now()),
            )
        return sender

    def purge(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM mail_events WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_cursors (
    account_id TEXT PRIMARY KEY,
    uid_validity INTEGER NOT NULL,
    last_uid INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    uid INTEGER NOT NULL,
    uid_validity INTEGER NOT NULL,
    message_id_hash TEXT,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    attachments_json TEXT NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    alert_level TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, uid_validity, uid)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES mail_events(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    adjustment INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL UNIQUE REFERENCES mail_events(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
"""
