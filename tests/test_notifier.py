import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.notifier import dispatch_outbox
from mailsentry.storage import MAX_NOTIFICATION_ATTEMPTS, Storage


class AlwaysFailNotifier:
    def __init__(self) -> None:
        self.attempts = 0

    def send(self, title: str, body: str) -> bool:
        self.attempts += 1
        return False


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


def test_failed_notification_survives_restart_and_retries_when_due(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    storage = Storage(database_path)
    storage.initialize()

    candidate = MailCandidate(
        account_id="personal",
        uid=1,
        uid_validity=10,
        message_id="<retry-test@example.com>",
        sender="sender@example.com",
        recipients=("me@example.com",),
        subject="Urgent payment",
        received_at=datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
    )
    assessment = Assessment(
        score=90,
        level=AlertLevel.IMMEDIATE,
        reasons=("test notification retry",),
    )

    event_id, _ = storage.save_assessment(candidate, assessment)
    storage.queue_notification(
        event_id,
        "Important email detected",
        "Urgent payment",
    )

    started_at = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)

    # 第一次发送失败，通知应留在持久化队列中。
    assert dispatch_outbox(
        storage,
        AlwaysFailNotifier(),
        now=started_at,
    ) == (0, 1)

    # 重新创建 Storage，模拟整个程序重启。
    restarted_storage = Storage(database_path)
    restarted_storage.initialize()
    notifier = RecordingNotifier()

    # 30 秒后尚未到重试时间。
    assert dispatch_outbox(
        restarted_storage,
        notifier,
        now=started_at + timedelta(seconds=30),
    ) == (0, 0)
    assert notifier.sent == []

    # 60 秒后允许第一次重试。
    assert dispatch_outbox(
        restarted_storage,
        notifier,
        now=started_at + timedelta(seconds=60),
    ) == (1, 0)
    assert notifier.sent == [
        ("Important email detected", "Urgent payment")
    ]


def test_notification_becomes_permanently_failed_after_attempt_limit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mail.db"
    storage = Storage(database_path)
    storage.initialize()
    candidate = MailCandidate(
        account_id="personal",
        uid=2,
        uid_validity=10,
        message_id="<permanent-failure@example.com>",
        sender="sender@example.com",
        recipients=("me@example.com",),
        subject="Urgent payment",
        received_at=datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
    )
    event_id, _ = storage.save_assessment(
        candidate,
        Assessment(90, AlertLevel.IMMEDIATE, ("test permanent failure",)),
    )
    storage.queue_notification(event_id, "Important email detected", "Urgent payment")
    notifier = AlwaysFailNotifier()
    attempted_at = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)

    for attempt in range(MAX_NOTIFICATION_ATTEMPTS):
        assert dispatch_outbox(storage, notifier, now=attempted_at) == (0, 1)
        if attempt < MAX_NOTIFICATION_ATTEMPTS - 1:
            delay_seconds = min(60 * (2**attempt), 3600)
            attempted_at += timedelta(seconds=delay_seconds)

    restarted_storage = Storage(database_path)
    restarted_storage.initialize()
    assert dispatch_outbox(
        restarted_storage,
        notifier,
        now=attempted_at + timedelta(days=1),
    ) == (0, 0)
    assert notifier.attempts == MAX_NOTIFICATION_ATTEMPTS

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT attempts, delivered_at, next_attempt_at, permanently_failed_at
            FROM notification_outbox
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()

    assert row == (
        MAX_NOTIFICATION_ATTEMPTS,
        None,
        None,
        attempted_at.isoformat(),
    )
