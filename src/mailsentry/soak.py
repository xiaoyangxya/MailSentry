from __future__ import annotations

import ctypes
import json
import os
import random
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from mailsentry.config import AccountConfig, ServiceConfig, StorageConfig
from mailsentry.connectors.imap import ImapFetchResult
from mailsentry.models import AttachmentMetadata, MailCandidate
from mailsentry.notifier import NotificationSender, dispatch_outbox
from mailsentry.service import MailboxConnector, MailSentryService
from mailsentry.storage import Storage

PRIVACY_MARKER = "SOAK_PRIVATE_BODY_MUST_NOT_PERSIST"
EVENT_ID_PATTERN = re.compile(r"^Event ID: (\d+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SoakConfig:
    database_path: Path
    metrics_path: Path
    seed: int = 20260915
    duration_seconds: int = 24 * 60 * 60
    event_interval_seconds: int = 5 * 60
    account_count: int = 10
    accelerated: bool = False
    rss_limit_mib: int = 512

    def validate(self) -> None:
        if self.account_count < 1:
            raise ValueError("account_count must be positive")
        if self.duration_seconds < self.event_interval_seconds * 2:
            raise ValueError("duration must generate at least two events per account")
        if self.event_interval_seconds < 1:
            raise ValueError("event_interval_seconds must be positive")
        if self.rss_limit_mib < 1:
            raise ValueError("rss_limit_mib must be positive")
        if self.database_path.resolve() == self.metrics_path.resolve():
            raise ValueError("database_path and metrics_path must be different")
        for path in (self.database_path, self.metrics_path):
            if path.exists():
                raise ValueError(f"refusing to overwrite existing soak output: {path}")


@dataclass(frozen=True, slots=True)
class SoakReport:
    passed: bool
    release_evidence: bool
    accounts: int
    seed: int
    duration_seconds: int
    event_interval_seconds: int
    generated: int
    observed: int
    persisted: int
    duplicate_events: int
    notifications_queued: int
    notifications_delivered: int
    duplicate_notifications: int
    notification_failures: int
    notifications_pending: int
    disconnects: int
    idle_renewals: int
    uidvalidity_changes: int
    service_restarts: int
    cursor_mismatches: int
    privacy_marker_found: bool
    current_rss_mib: float
    peak_rss_mib: float
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["failures"] = list(self.failures)
        return result


class _RecordingNotifier(NotificationSender):
    def __init__(self) -> None:
        self.failure_count = 0
        self.delivered_event_ids: list[int] = []

    def send(self, title: str, body: str) -> bool:
        if self.failure_count == 0:
            self.failure_count = 1
            return False
        self.delivered_event_ids.extend(
            int(match) for match in EVENT_ID_PATTERN.findall(body)
        )
        return True


class _Simulation:
    def __init__(self, config: SoakConfig) -> None:
        self.config = config
        self.accounts = tuple(_account(index) for index in range(config.account_count))
        self.events_per_account = config.duration_seconds // config.event_interval_seconds
        self.split = self.events_per_account // 2
        self.generated_by_account = {
            account.id: self.events_per_account for account in self.accounts
        }
        self.observed_by_account: Counter[str] = Counter()
        self.idle_renewals = 0
        self.disconnects = 0
        self.uidvalidity_changes = 0
        self.phase = 0
        self.stop_event = threading.Event()
        self._phase_waiters: set[str] = set()
        self._phase_started_at = 0.0
        self._lock = threading.Lock()
        self._rng = random.Random(config.seed)
        self._nonce = {
            account.id: self._rng.randrange(1_000_000, 9_999_999)
            for account in self.accounts
        }

    def begin_phase(self, phase: int, stop_event: threading.Event) -> None:
        with self._lock:
            self.phase = phase
            self.stop_event = stop_event
            self._phase_waiters = set()
            self._phase_started_at = time.monotonic()

    def connector(self, account: AccountConfig, include_text: bool) -> MailboxConnector:
        if include_text:
            raise AssertionError("soak harness must not enable a cloud provider")
        return _SimulatedConnector(self, account)

    def fetch_since(self, account: AccountConfig, since: date) -> ImapFetchResult:
        del since
        return ImapFetchResult((), _base_validity(account), 0)

    def fetch_after(
        self, account: AccountConfig, uid: int, expected_uid_validity: int
    ) -> ImapFetchResult:
        candidates = self._available(account)
        validity = candidates[-1].uid_validity if candidates else _base_validity(account)
        selected = (
            candidates
            if validity != expected_uid_validity
            else tuple(candidate for candidate in candidates if candidate.uid > uid)
        )
        with self._lock:
            self.observed_by_account[account.id] += len(selected)
        last_uid = candidates[-1].uid if candidates else 0
        return ImapFetchResult(selected, validity, last_uid)

    def wait_for_change(self, account: AccountConfig, timeout: float) -> None:
        if timeout <= 0:
            raise AssertionError("IDLE timeout must be positive")
        with self._lock:
            self.idle_renewals += 1
            should_disconnect = (
                self.phase == 1
                and account.id == "soak-00"
                and self.disconnects == 0
            )
            if should_disconnect:
                self.disconnects += 1
        if should_disconnect:
            raise ConnectionError("synthetic deterministic disconnect")
        if not self.config.accelerated:
            elapsed = time.monotonic() - self._phase_started_at
            target = self._phase_total_count() * self.config.event_interval_seconds
            if elapsed < target:
                self.stop_event.wait(
                    min(timeout, self.config.event_interval_seconds, target - elapsed)
                )
                return
        with self._lock:
            self._phase_waiters.add(account.id)
            if len(self._phase_waiters) == len(self.accounts):
                self.stop_event.set()

    def expected_cursor(self, account: AccountConfig) -> tuple[int, int]:
        candidates = self._available(account)
        return candidates[-1].uid_validity, candidates[-1].uid

    def _available(self, account: AccountConfig) -> tuple[MailCandidate, ...]:
        account_index = int(account.id.rsplit("-", maxsplit=1)[1])
        phase_count = self._phase_event_count()
        if self.phase <= 1:
            return tuple(
                self._candidate(account, account_index, event_index, event_index + 1)
                for event_index in range(phase_count)
            )
        if account.id == "soak-00":
            replay = self._candidate(account, account_index, self.split - 1, 1, changed=True)
            new_events = tuple(
                self._candidate(
                    account,
                    account_index,
                    event_index,
                    event_index - self.split + 2,
                    changed=True,
                )
                for event_index in range(self.split, self.events_per_account)
                if event_index < self.split + phase_count
            )
            return (replay, *new_events)
        return tuple(
            self._candidate(account, account_index, event_index, event_index + 1)
            for event_index in range(self.split + phase_count)
        )

    def _phase_event_count(self) -> int:
        phase_total = self._phase_total_count()
        if self.config.accelerated:
            return phase_total
        elapsed = max(0.0, time.monotonic() - self._phase_started_at)
        return min(
            phase_total,
            int(elapsed // self.config.event_interval_seconds),
        )

    def _phase_total_count(self) -> int:
        return self.split if self.phase <= 1 else self.events_per_account - self.split

    def _candidate(
        self,
        account: AccountConfig,
        account_index: int,
        event_index: int,
        uid: int,
        *,
        changed: bool = False,
    ) -> MailCandidate:
        mode = event_index % 3
        subjects = (
            "Urgent security alert payment required",
            "Provider-important quarterly review",
            "Synthetic newsletter",
        )
        validity = _base_validity(account) + int(changed)
        return MailCandidate(
            account_id=account.id,
            uid=uid,
            uid_validity=validity,
            message_id=(
                f"<soak-{self._nonce[account.id]}-{event_index}@invalid.example>"
            ),
            sender=f"sender-{account_index:02d}@invalid.example",
            recipients=(f"recipient-{account_index:02d}@invalid.example",),
            subject=f"{subjects[mode]} #{event_index}",
            received_at=datetime(2030, 1, 1, tzinfo=UTC)
            + timedelta(seconds=event_index * self.config.event_interval_seconds),
            flags=frozenset({"\\Important"}) if mode == 1 else frozenset(),
            has_priority_header=mode == 0,
            has_list_unsubscribe=mode == 2,
            attachments=(
                AttachmentMetadata("synthetic-report.pdf", "application/pdf", 1234),
            ),
            text_excerpt=f"{PRIVACY_MARKER}:{account.id}:{event_index}",
        )


class _SimulatedConnector:
    def __init__(self, simulation: _Simulation, account: AccountConfig) -> None:
        self._simulation = simulation
        self._account = account

    def fetch_since(self, since: date) -> ImapFetchResult:
        return self._simulation.fetch_since(self._account, since)

    def fetch_after(
        self, uid: int, expected_uid_validity: int | None = None
    ) -> ImapFetchResult:
        if expected_uid_validity is None:
            raise AssertionError("soak fetch requires an expected UIDVALIDITY")
        return self._simulation.fetch_after(self._account, uid, expected_uid_validity)

    def wait_for_change(self, timeout: float) -> None:
        self._simulation.wait_for_change(self._account, timeout)


def run_soak(config: SoakConfig) -> SoakReport:
    config.validate()
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.metrics_path.parent.mkdir(parents=True, exist_ok=True)
    simulation = _Simulation(config)
    notifier = _RecordingNotifier()
    base_wall_clock = datetime.now(UTC) + timedelta(days=1)

    initial_service = _service(
        simulation, notifier, config.database_path, lambda: base_wall_clock
    )
    initial_service.bootstrap()
    _run_phase(initial_service, simulation, phase=1)
    _write_sample(config.metrics_path, simulation, initial_service.storage, "phase-1")

    restarted_service = _service(
        simulation,
        notifier,
        config.database_path,
        lambda: base_wall_clock + timedelta(days=1),
    )
    simulation.uidvalidity_changes = 1
    _run_phase(restarted_service, simulation, phase=2)

    retry_time = base_wall_clock + timedelta(days=3)
    dispatch_outbox(restarted_service.storage, notifier, now=retry_time)
    dispatch_outbox(
        restarted_service.storage, notifier, now=retry_time + timedelta(days=1)
    )
    _write_sample(config.metrics_path, simulation, restarted_service.storage, "phase-2")

    report = _build_report(config, simulation, restarted_service.storage, notifier)
    _append_json(
        config.metrics_path,
        {
            "kind": "final",
            "timestamp": datetime.now(UTC).isoformat(),
            **report.to_dict(),
        },
    )
    return report


def _service(
    simulation: _Simulation,
    notifier: NotificationSender,
    database_path: Path,
    wall_clock,
) -> MailSentryService:
    return MailSentryService(
        ServiceConfig(
            accounts=simulation.accounts,
            storage=StorageConfig(path=database_path, retention_days=30),
            poll_seconds=30,
        ),
        connector_factory=simulation.connector,
        notifier=notifier,
        wall_clock=wall_clock,
        outbox_check_seconds=0.01 if simulation.config.accelerated else 30,
        retry_delay=(
            (lambda failures: 0.001 * failures)
            if simulation.config.accelerated
            else None
        ),
    )


def _run_phase(
    service: MailSentryService, simulation: _Simulation, *, phase: int
) -> None:
    stop_event = threading.Event()
    simulation.begin_phase(phase, stop_event)
    service.run_until(stop_event)


def _build_report(
    config: SoakConfig,
    simulation: _Simulation,
    storage: Storage,
    notifier: _RecordingNotifier,
) -> SoakReport:
    expected = simulation.events_per_account * config.account_count
    persisted, queued, delivered = _database_counts(config.database_path)
    pending, permanently_failed = storage.notification_counts()
    cursor_mismatches = sum(
        storage.get_cursor(account.id) != simulation.expected_cursor(account)
        for account in simulation.accounts
    )
    delivery_counts = Counter(notifier.delivered_event_ids)
    duplicate_notifications = sum(count - 1 for count in delivery_counts.values())
    privacy_marker_found = _privacy_marker_found(config.database_path)
    current_rss, peak_rss = _rss_mib()
    failures: list[str] = []
    if persisted != expected:
        failures.append(f"event reconciliation failed: expected={expected} persisted={persisted}")
    if duplicate_notifications:
        failures.append(f"duplicate notifications delivered={duplicate_notifications}")
    if pending or permanently_failed:
        failures.append(
            f"notification outbox unhealthy: pending={pending} failed={permanently_failed}"
        )
    if cursor_mismatches:
        failures.append(f"cursor mismatches={cursor_mismatches}")
    if privacy_marker_found:
        failures.append("privacy marker found in persistent output")
    if peak_rss > config.rss_limit_mib:
        failures.append(
            f"peak RSS exceeded limit: peak={peak_rss:.2f}MiB limit={config.rss_limit_mib}MiB"
        )
    return SoakReport(
        passed=not failures,
        release_evidence=(
            not failures
            and not config.accelerated
            and config.account_count == 10
            and config.duration_seconds >= 24 * 60 * 60
        ),
        accounts=config.account_count,
        seed=config.seed,
        duration_seconds=config.duration_seconds,
        event_interval_seconds=config.event_interval_seconds,
        generated=expected,
        observed=sum(simulation.observed_by_account.values()),
        persisted=persisted,
        duplicate_events=max(0, persisted - expected),
        notifications_queued=queued,
        notifications_delivered=delivered,
        duplicate_notifications=duplicate_notifications,
        notification_failures=notifier.failure_count,
        notifications_pending=pending + permanently_failed,
        disconnects=simulation.disconnects,
        idle_renewals=simulation.idle_renewals,
        uidvalidity_changes=simulation.uidvalidity_changes,
        service_restarts=1,
        cursor_mismatches=cursor_mismatches,
        privacy_marker_found=privacy_marker_found,
        current_rss_mib=current_rss,
        peak_rss_mib=peak_rss,
        failures=tuple(failures),
    )


def _write_sample(
    path: Path, simulation: _Simulation, storage: Storage, kind: str
) -> None:
    current_rss, peak_rss = _rss_mib()
    accounts = {}
    for account in simulation.accounts:
        cursor = storage.get_cursor(account.id)
        accounts[account.id] = {
            "generated": (
                simulation.split
                if simulation.phase == 1
                else simulation.generated_by_account[account.id]
            ),
            "observed": simulation.observed_by_account[account.id],
            "cursor": list(cursor) if cursor else None,
        }
    persisted, queued, delivered = _database_counts(simulation.config.database_path)
    _append_json(
        path,
        {
            "kind": kind,
            "timestamp": datetime.now(UTC).isoformat(),
            "accounts": accounts,
            "persisted": persisted,
            "notifications_queued": queued,
            "notifications_delivered": delivered,
            "current_rss_mib": current_rss,
            "peak_rss_mib": peak_rss,
        },
    )


def _append_json(path: Path, value: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _database_counts(path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as connection:
        persisted = int(connection.execute("SELECT COUNT(*) FROM mail_events").fetchone()[0])
        queued = int(
            connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
        )
        delivered = int(
            connection.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE delivered_at IS NOT NULL"
            ).fetchone()[0]
        )
    return persisted, queued, delivered


def _privacy_marker_found(database_path: Path) -> bool:
    marker = PRIVACY_MARKER.encode()
    return any(
        marker in path.read_bytes()
        for path in database_path.parent.glob(f"{database_path.name}*")
        if path.is_file()
    )


def _account(index: int) -> AccountConfig:
    return AccountConfig(
        id=f"soak-{index:02d}",
        host="invalid.example",
        username=f"soak-{index:02d}@invalid.example",
        password_env=f"MAILSENTRY_SOAK_UNUSED_{index:02d}",
    )


def _base_validity(account: AccountConfig) -> int:
    return 10_000 + int(account.id.rsplit("-", maxsplit=1)[1])


def _rss_mib() -> tuple[float, float]:
    if os.name == "nt":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        )
        if not ok:
            return 0.0, 0.0
        current = counters.WorkingSetSize / (1024 * 1024)
        peak = counters.PeakWorkingSetSize / (1024 * 1024)
        return current, peak
    values: dict[str, float] = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, raw, _unit = line.split()
            values[key.rstrip(":")] = float(raw) / 1024
    return values.get("VmRSS", 0.0), values.get("VmHWM", 0.0)


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]
