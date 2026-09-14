from datetime import UTC, datetime
from pathlib import Path

from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.storage import Storage


def candidate(uid: int, message_id: str = "<same@example.com>") -> MailCandidate:
    return MailCandidate(
        account_id="personal",
        uid=uid,
        uid_validity=1,
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


def test_cursor_round_trip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()

    assert storage.get_cursor("personal") is None
    storage.set_cursor("personal", uid_validity=22, last_uid=99)

    assert storage.get_cursor("personal") == (22, 99)


def test_feedback_is_capped(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "mail.db")
    storage.initialize()
    event_id, _ = storage.save_assessment(
        candidate(1), Assessment(20, AlertLevel.AUDIT, ("baseline",))
    )

    storage.add_feedback(event_id, True)
    storage.add_feedback(event_id, True)

    assert storage.sender_feedback("sender@example.com") == 30
