import logging
import threading
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace

from mailsentry.config import (
    AccountConfig,
    NotificationConfig,
    ServiceConfig,
    StorageConfig,
)
from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.service import MailSentryService, _latest_digest_slot


def account(account_id: str) -> AccountConfig:
    return AccountConfig(
        id=account_id,
        host="imap.example.com",
        username=f"{account_id}@example.com",
        password_env=f"{account_id.upper()}_PASSWORD",
    )


def mail_candidate(uid: int, subject: str) -> MailCandidate:
    return MailCandidate(
        account_id="personal",
        uid=uid,
        uid_validity=1,
        message_id=f"<{uid}@example.com>",
        sender="sender@example.com",
        recipients=("me@example.com",),
        subject=subject,
        received_at=datetime(2026, 9, 15, tzinfo=UTC),
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


def test_process_once_persists_reconciled_cursor_for_empty_mailbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_account = account("personal")
    service = MailSentryService(
        ServiceConfig(
            accounts=(current_account,),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        )
    )
    service.storage.set_cursor("personal", uid_validity=11, last_uid=99)

    class ReconciledConnector:
        def __init__(self, account_config: AccountConfig) -> None:
            assert account_config is current_account

        def fetch_after(self, uid: int, expected_uid_validity: int):
            assert (uid, expected_uid_validity) == (99, 11)
            return SimpleNamespace(
                candidates=(),
                uid_validity=22,
                last_uid=7,
            )

    monkeypatch.setattr("mailsentry.service.ImapConnector", ReconciledConnector)
    monkeypatch.setattr(
        "mailsentry.service.dispatch_outbox",
        lambda storage, notifier: (0, 0),
    )

    assert service.process_once() == (0, 0, 0)
    assert service.storage.get_cursor("personal") == (22, 7)


def test_process_once_runs_configured_metadata_retention(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_account = account("personal")
    service = MailSentryService(
        ServiceConfig(
            accounts=(current_account,),
            storage=StorageConfig(
                path=tmp_path / "mail.db",
                retention_days=45,
            ),
        )
    )
    retention_runs: list[int] = []
    monkeypatch.setattr(service, "_process_account", lambda account_config: 0)
    monkeypatch.setattr(
        "mailsentry.service.dispatch_outbox",
        lambda storage, notifier: (0, 0),
    )
    monkeypatch.setattr(
        service.storage,
        "purge",
        lambda retention_days: retention_runs.append(retention_days) or 0,
    )

    assert service.process_once() == (0, 0, 0)
    assert retention_runs == [45]


def test_run_forever_starts_one_worker_per_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    accounts = (account("personal"), account("work"))
    service = MailSentryService(
        ServiceConfig(
            accounts=accounts,
            storage=StorageConfig(path=tmp_path / "mail.db"),
        )
    )
    started: list[str] = []

    def run_worker(
        account_config: AccountConfig,
        stop_event: threading.Event,
        outbox_ready: threading.Event,
    ) -> None:
        started.append(account_config.id)

    def stop_supervisor() -> tuple[int, int]:
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_run_account_worker", run_worker)
    monkeypatch.setattr(service, "_dispatch_and_retain", stop_supervisor)

    service.run_forever()

    assert sorted(started) == ["personal", "work"]


def test_account_worker_catches_up_after_idle_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_account = account("personal")
    service = MailSentryService(
        ServiceConfig(
            accounts=(current_account,),
            storage=StorageConfig(path=tmp_path / "mail.db"),
            poll_seconds=900,
        )
    )
    stop_event = threading.Event()
    outbox_ready = threading.Event()
    scans = 0
    idle_timeouts: list[float] = []

    def process_account(account_config: AccountConfig) -> int:
        nonlocal scans
        assert account_config is current_account
        scans += 1
        if scans == 2:
            stop_event.set()
        return 1 if scans == 1 else 0

    class IdleConnector:
        def __init__(self, account_config: AccountConfig) -> None:
            assert account_config is current_account

        def wait_for_change(self, timeout: float) -> None:
            idle_timeouts.append(timeout)

    monkeypatch.setattr(service, "_process_account", process_account)
    monkeypatch.setattr("mailsentry.service.ImapConnector", IdleConnector)

    service._run_account_worker(current_account, stop_event, outbox_ready)

    assert scans == 2
    assert idle_timeouts == [300]
    assert outbox_ready.is_set() is True


def test_account_worker_backs_off_after_connection_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_account = account("personal")
    service = MailSentryService(
        ServiceConfig(
            accounts=(current_account,),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        )
    )
    waits: list[float] = []

    class StopAfterWait:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            self.stopped = True
            return True

    monkeypatch.setattr(
        service,
        "_process_account",
        lambda account_config: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    service._run_account_worker(
        current_account,
        StopAfterWait(),
        threading.Event(),
    )

    assert waits == [60]


def test_dispatch_queues_one_timezone_digest_per_slot_across_restart(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, 1, 0, tzinfo=UTC)
    config = ServiceConfig(
        accounts=(),
        notifications=NotificationConfig(digest_times=(time(9), time(18))),
        storage=StorageConfig(path=tmp_path / "mail.db"),
        timezone="Asia/Shanghai",
    )
    service = MailSentryService(config, wall_clock=lambda: now)
    service.storage.save_assessment(
        mail_candidate(1, "Review contract"),
        Assessment(65, AlertLevel.DIGEST, ("medium score",)),
    )
    service.storage.save_assessment(
        mail_candidate(2, "Newsletter"),
        Assessment(10, AlertLevel.AUDIT, ("low score",)),
    )
    sent: list[tuple[str, str]] = []

    class RecordingNotifier:
        def send(self, title: str, body: str) -> bool:
            sent.append((title, body))
            return True

    service.notifier = RecordingNotifier()

    assert service._dispatch_and_retain() == (1, 0)
    restarted_service = MailSentryService(config, wall_clock=lambda: now)
    restarted_service.notifier = RecordingNotifier()
    assert restarted_service._dispatch_and_retain() == (0, 0)
    assert len(sent) == 1
    assert sent[0][0] == "MailSentry digest: 1 noteworthy email(s)"
    assert "Subject: Review contract" in sent[0][1]
    assert "Why: medium score" in sent[0][1]
    assert "Newsletter" not in sent[0][1]


def test_bootstrap_assessment_is_suppressed_from_future_digest(
    tmp_path: Path,
) -> None:
    service = MailSentryService(
        ServiceConfig(
            accounts=(),
            storage=StorageConfig(path=tmp_path / "mail.db"),
        )
    )
    service.classifier = SimpleNamespace(
        assess=lambda candidate, feedback: Assessment(
            65,
            AlertLevel.DIGEST,
            ("medium score",),
        )
    )

    service._record(mail_candidate(1, "Bootstrap candidate"), notify=False)

    assert (
        service.storage.pending_digest_events(datetime(2030, 1, 1, tzinfo=UTC))
        == []
    )


def test_latest_digest_slot_uses_configured_timezone() -> None:
    before_morning_digest = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)

    slot = _latest_digest_slot(
        before_morning_digest,
        "Asia/Shanghai",
        (time(9), time(18)),
    )

    assert slot == datetime(2029, 12, 31, 10, 0, tzinfo=UTC)
