import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.storage import Storage


def candidate(
    uid: int,
    message_id: str = "<same@example.com>",
    *,
    uid_validity: int = 1,
) -> MailCandidate:
    return MailCandidate(
        account_id="personal",
        uid=uid,
        uid_validity=uid_validity,
        message_id=message_id,
        sender="sender@example.com",
        recipients=("me@example.com",),
        subject="Important subject",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_stores_metadata_and_deduplicates_message_id(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()
    assessment = Assessment(90, AlertLevel.IMMEDIATE, ("test reason",))

    event_id, created = storage.save_assessment(candidate(1), assessment)
    duplicate_id, duplicate_created = storage.save_assessment(candidate(2), assessment)

    assert created is True
    assert duplicate_created is False
    assert duplicate_id == event_id


def test_deduplicates_message_id_after_restart_and_uidvalidity_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    assessment = Assessment(90, AlertLevel.IMMEDIATE, ("test reason",))
    storage = Storage(database_path)
    storage.initialize()

    event_id, created = storage.save_assessment(
        candidate(10, uid_validity=1),
        assessment,
    )

    restarted_storage = Storage(database_path)
    restarted_storage.initialize()
    duplicate_id, duplicate_created = restarted_storage.save_assessment(
        candidate(3, uid_validity=2),
        assessment,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate_id == event_id


def test_cursor_round_trip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()

    assert storage.get_cursor("personal") is None
    storage.set_cursor("personal", uid_validity=22, last_uid=99)

    assert storage.get_cursor("personal") == (22, 99)


def test_status_reports_cursor_and_notification_counts(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()
    storage.set_cursor("personal", uid_validity=22, last_uid=99)
    event_id, _ = storage.save_assessment(
        candidate(1), Assessment(90, AlertLevel.IMMEDIATE, ("test reason",))
    )
    storage.queue_notification(event_id, "Important email detected", "Test event")

    account_status = storage.account_status("personal")

    assert account_status is not None
    assert account_status["uid_validity"] == 22
    assert account_status["last_uid"] == 99
    assert account_status["updated_at"]
    assert storage.account_status("missing") is None
    assert storage.notification_counts() == (1, 0)


def test_recent_assessments_are_newest_first_and_filter_by_level(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()
    storage.save_assessment(
        candidate(1, "<first@example.com>"),
        Assessment(20, AlertLevel.AUDIT, ("baseline",)),
    )
    latest_id, _ = storage.save_assessment(
        candidate(2, "<second@example.com>"),
        Assessment(90, AlertLevel.IMMEDIATE, ("important",)),
    )

    events = storage.recent_assessments()
    immediate = storage.recent_assessments(alert_level="immediate")

    assert [row["id"] for row in events] == [latest_id, latest_id - 1]
    assert [row["id"] for row in immediate] == [latest_id]


def test_initialize_adds_permanent_failure_state_to_existing_outbox(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                next_attempt_at TEXT
            )
            """
        )

    Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(notification_outbox)")
        }
    assert "permanently_failed_at" in columns


def test_initialize_adds_digest_state_to_existing_mail_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE mail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_level TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    Storage(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(mail_events)")
        }
    assert "digested_at" in columns


def test_feedback_is_capped(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()
    event_id, _ = storage.save_assessment(
        candidate(1), Assessment(20, AlertLevel.AUDIT, ("baseline",))
    )

    storage.add_feedback(event_id, True)
    storage.add_feedback(event_id, True)

    assert storage.sender_feedback("sender@example.com") == 30


def test_purge_cascades_to_feedback_and_notification_outbox(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    storage = Storage(database_path)
    storage.initialize()
    event_id, _ = storage.save_assessment(
        candidate(1),
        Assessment(90, AlertLevel.IMMEDIATE, ("old event",)),
    )
    storage.add_feedback(event_id, True)
    storage.queue_notification(event_id, "Important email detected", "Old event")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE mail_events SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", event_id),
        )

    purged = storage.purge(
        30,
        now=datetime(2026, 9, 14, tzinfo=UTC),
    )

    assert purged == 1
    with sqlite3.connect(database_path) as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("mail_events", "feedback", "notification_outbox")
        )
    assert counts == (0, 0, 0)
