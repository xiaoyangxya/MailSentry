import logging
from datetime import UTC, datetime
from pathlib import Path

from mailsentry.config import (
    AccountConfig,
    NotificationConfig,
    ServiceConfig,
    StorageConfig,
)
from mailsentry.parser import candidate_from_imap
from mailsentry.service import MailSentryService


def test_transient_content_and_credentials_stay_out_of_persistent_outputs(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    body_secret = "BODY-SECRET-7f45c6"
    attachment_secret = "ATTACHMENT-BYTES-SECRET-a20d19"
    password_secret = "MAILBOX-PASSWORD-SECRET-b55e83"
    webhook_secret = "WEBHOOK-TOKEN-SECRET-350a9c"
    monkeypatch.setenv("PRIVACY_TEST_PASSWORD", password_secret)
    monkeypatch.setenv("PRIVACY_TEST_NOTIFICATIONS", f"mock://{webhook_secret}")
    candidate = candidate_from_imap(
        account_id="personal",
        uid=1,
        uid_validity=1,
        header_bytes=(
            b"Message-ID: <privacy@example.com>\r\n"
            b"From: sender@example.com\r\n"
            b"To: private-recipient@example.com\r\n"
            b"Subject: Security alert\r\n"
            b"Importance: high\r\n\r\n"
            + f"{body_secret}\n{attachment_secret}".encode()
        ),
        flags=frozenset(),
        internal_date=datetime(2026, 9, 15, tzinfo=UTC),
        body_structure=None,
    )
    database_path = tmp_path / "mail.db"
    configured_urls: list[tuple[str, ...]] = []

    class RecordingNotifier:
        def __init__(self, urls: tuple[str, ...]) -> None:
            configured_urls.append(urls)

    monkeypatch.setattr("mailsentry.service.AppriseNotifier", RecordingNotifier)
    config = ServiceConfig(
        accounts=(
            AccountConfig(
                id="personal",
                host="imap.example.com",
                username="me@example.com",
                password_env="PRIVACY_TEST_PASSWORD",
            ),
        ),
        notifications=NotificationConfig(urls_env="PRIVACY_TEST_NOTIFICATIONS"),
        storage=StorageConfig(path=database_path),
    )

    with caplog.at_level(logging.DEBUG):
        service = MailSentryService(config)
        service._record(candidate, notify=True)

    pending = service.storage.pending_notifications()
    assert configured_urls == [(f"mock://{webhook_secret}",)]
    assert not hasattr(candidate, "body")
    assert len(pending) == 1
    assert "Account: personal" in pending[0]["body"]
    assert "From: sender@example.com" in pending[0]["body"]
    assert "Subject: Security alert" in pending[0]["body"]

    database_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("mail.db*"))
    observable_text = caplog.text + str(pending[0]["title"]) + str(pending[0]["body"])
    for secret in (body_secret, attachment_secret, password_secret, webhook_secret):
        assert secret.encode() not in database_bytes
        assert secret not in observable_text
