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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
    assert "account processing failed account=broken retry_in_seconds=60" in caplog.text


def test_failed_account_uses_exponential_backoff_and_resets_after_success(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    current_account = account("personal")
    clock = FakeClock()
    service = MailSentryService(
        ServiceConfig(
            accounts=(current_account,),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        ),
        clock=clock,
    )
    attempts = 0

    def process_account(account_config: AccountConfig) -> int:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ConnectionError("IMAP unavailable")
        return 1

    monkeypatch.setattr(service, "_process_account", process_account)
    monkeypatch.setattr(
        "mailsentry.service.dispatch_outbox",
        lambda storage, notifier: (0, 0),
    )

    with caplog.at_level(logging.ERROR):
        assert service.process_once() == (0, 0, 0)
        clock.advance(59)
        assert service.process_once() == (0, 0, 0)
        assert attempts == 1

        clock.advance(1)
        assert service.process_once() == (0, 0, 0)
        clock.advance(119)
        assert service.process_once() == (0, 0, 0)
        assert attempts == 2

        clock.advance(1)
        assert service.process_once() == (1, 0, 0)
        assert service.process_once() == (1, 0, 0)

    assert attempts == 4
    assert "retry_in_seconds=60" in caplog.text
    assert "retry_in_seconds=120" in caplog.text


def test_account_backoff_is_capped_at_one_hour(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    clock = FakeClock()
    service = MailSentryService(
        ServiceConfig(
            accounts=(account("personal"),),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        ),
        clock=clock,
    )

    def fail(_account: AccountConfig) -> int:
        raise ConnectionError("IMAP unavailable")

    monkeypatch.setattr(service, "_process_account", fail)
    monkeypatch.setattr(
        "mailsentry.service.dispatch_outbox",
        lambda storage, notifier: (0, 0),
    )

    delays = (60, 120, 240, 480, 960, 1920, 3600, 3600)
    with caplog.at_level(logging.ERROR):
        for delay in delays:
            assert service.process_once() == (0, 0, 0)
            clock.advance(delay)

    assert caplog.text.count("retry_in_seconds=3600") == 2
