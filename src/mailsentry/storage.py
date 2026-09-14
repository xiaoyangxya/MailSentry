from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailsentry.models import Assessment, MailCandidate

INITIAL_NOTIFICATION_RETRY_SECONDS = 60
MAX_NOTIFICATION_RETRY_SECONDS = 60 * 60
MAX_NOTIFICATION_ATTEMPTS = 8


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(notification_outbox)")
            }
            if "next_attempt_at" not in columns:
                connection.execute(
                    "ALTER TABLE notification_outbox ADD COLUMN next_attempt_at TEXT"
                )
            if "permanently_failed_at" not in columns:
                connection.execute(
                    "ALTER TABLE notification_outbox ADD COLUMN permanently_failed_at TEXT"
                )
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(mail_events)")
            }
            if "digested_at" not in event_columns:
                connection.execute("ALTER TABLE mail_events ADD COLUMN digested_at TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mail_events_digest_pending
                ON mail_events(alert_level, digested_at, created_at)
                """
            )

    def get_cursor(self, account_id: str) -> tuple[int, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT uid_validity, last_uid FROM account_cursors WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def account_status(self, account_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT uid_validity, last_uid, updated_at
                FROM account_cursors
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()

    def notification_counts(self) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE delivered_at IS NULL AND permanently_failed_at IS NULL
                    ) AS pending,
                    COUNT(*) FILTER (WHERE permanently_failed_at IS NOT NULL) AS failed
                FROM notification_outbox
                """
            ).fetchone()
        return int(row["pending"]), int(row["failed"])

    def recent_assessments(
        self,
        limit: int = 20,
        *,
        alert_level: str | None = None,
    ) -> list[sqlite3.Row]:
        parameters: list[object] = []
        where = ""
        if alert_level is not None:
            where = "WHERE alert_level = ?"
            parameters.append(alert_level)
        parameters.append(limit)
        with self._connect() as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT id, account_id, received_at, sender, subject, score,
                           alert_level, reasons_json, created_at
                    FROM mail_events
                    {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            )

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

    def pending_digest_events(
        self,
        before: datetime,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, account_id, sender, subject, score, reasons_json
                    FROM mail_events
                    WHERE alert_level = ?
                      AND digested_at IS NULL
                      AND created_at <= ?
                    ORDER BY created_at, id
                    LIMIT ?
                    """,
                    ("digest", _timestamp(before), limit),
                ).fetchall()
            )

    def suppress_digest(self, event_id: int, *, now: datetime | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE mail_events
                SET digested_at = ?
                WHERE id = ? AND digested_at IS NULL
                """,
                (_timestamp(now), event_id),
            )

    def queue_digest(
        self,
        slot: datetime,
        event_ids: Sequence[int],
        title: str,
        body: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        slot_at = _timestamp(slot)
        queued_at = _timestamp(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO digest_runs(slot_at, created_at)
                VALUES (?, ?)
                """,
                (slot_at, queued_at),
            )
            if cursor.rowcount == 0:
                return False
            if not event_ids:
                return True
            connection.execute(
                """
                INSERT INTO notification_outbox(event_id, title, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_ids[0], title, body, queued_at),
            )
            placeholders = ", ".join("?" for _ in event_ids)
            connection.execute(
                f"""
                UPDATE mail_events
                SET digested_at = ?
                WHERE digested_at IS NULL
                  AND id IN ({placeholders})
                """,
                (queued_at, *event_ids),
            )
            return True

    def pending_notifications(
        self, limit: int = 100, *, now: datetime | None = None
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, event_id, title, body, attempts
                    FROM notification_outbox
                    WHERE delivered_at IS NULL
                      AND permanently_failed_at IS NULL
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY id LIMIT ?
                    """,
                    (_timestamp(now), limit),
                ).fetchall()
            )

    def mark_notification(
        self, outbox_id: int, delivered: bool, *, now: datetime | None = None
    ) -> None:
        timestamp = _timestamp(now)
        with self._connect() as connection:
            if delivered:
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET delivered_at = ?,
                        next_attempt_at = NULL,
                        permanently_failed_at = NULL
                    WHERE id = ?
                    """,
                    (timestamp, outbox_id),
                )
            else:
                row = connection.execute(
                    "SELECT attempts FROM notification_outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
                if row is None:
                    return
                attempts = int(row["attempts"]) + 1
                if attempts >= MAX_NOTIFICATION_ATTEMPTS:
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET attempts = ?,
                            next_attempt_at = NULL,
                            permanently_failed_at = ?
                        WHERE id = ?
                        """,
                        (attempts, timestamp, outbox_id),
                    )
                    return
                exponent = min(attempts - 1, 6)
                delay_seconds = min(
                    INITIAL_NOTIFICATION_RETRY_SECONDS * (2**exponent),
                    MAX_NOTIFICATION_RETRY_SECONDS,
                )
                next_attempt_at = (
                    datetime.fromisoformat(timestamp) + timedelta(seconds=delay_seconds)
                ).isoformat()
                connection.execute(
                    """
                    UPDATE notification_outbox
                    SET attempts = ?, next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (attempts, next_attempt_at, outbox_id),
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

    def purge(self, retention_days: int, *, now: datetime | None = None) -> int:
        cutoff = (
            (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=retention_days)
        ).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM digest_runs WHERE slot_at < ?", (cutoff,))
            cursor = connection.execute("DELETE FROM mail_events WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _now() -> str:
    return _timestamp()


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


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
    digested_at TEXT,
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
    delivered_at TEXT,
    next_attempt_at TEXT,
    permanently_failed_at TEXT
);

CREATE TABLE IF NOT EXISTS digest_runs (
    slot_at TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""
