from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.notifier import dispatch_outbox
from mailsentry.storage import Storage


class AlwaysFailNotifier:
    def send(self, title: str, body: str) -> bool:
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