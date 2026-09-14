import logging
from pathlib import Path

from mailsentry.config import AccountConfig, ServiceConfig, StorageConfig
from mailsentry.service import MailSentryService


def account(account_id: str) -> AccountConfig:
    return AccountConfig(
        id=account_id,
        host="imap.example.com",
        username=f"{account_id}@example.com",
        password_env=f"{account_id.upper()}_PASSWORD",
    )


def test_process_once_isolates_account_failures_and_dispatches_outbox(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    broken = account("broken")
    healthy = account("healthy")
    service = MailSentryService(
        ServiceConfig(
            accounts=(broken, healthy),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        )
    )
    processed: list[str] = []
    dispatched: list[object] = []

    def process_account(current: AccountConfig) -> int:
        processed.append(current.id)
        if current is broken:
            raise RuntimeError("mailbox unavailable")
        return 2

    def fake_dispatch_outbox(storage, notifier) -> tuple[int, int]:
        dispatched.append((storage, notifier))
        return 1, 0

    monkeypatch.setattr(service, "_process_account", process_account)
    monkeypatch.setattr("mailsentry.service.dispatch_outbox", fake_dispatch_outbox)

    with caplog.at_level(logging.ERROR):
        result = service.process_once()

    assert result == (2, 1, 0)
    assert processed == ["broken", "healthy"]
    assert dispatched == [(service.storage, service.notifier)]
    assert "account processing failed account=broken" in caplog.text
