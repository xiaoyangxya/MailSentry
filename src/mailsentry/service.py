from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from typing import Protocol
from zoneinfo import ZoneInfo

from mailsentry.classifier import RuleClassifier
from mailsentry.cloud_classifier import ResponsesCompatibleClassifier
from mailsentry.config import AccountConfig, ServiceConfig
from mailsentry.connectors import ImapConnector
from mailsentry.connectors.imap import ImapFetchResult
from mailsentry.models import AlertLevel, MailCandidate
from mailsentry.notifier import AppriseNotifier, NotificationSender, dispatch_outbox
from mailsentry.storage import Storage

LOGGER = logging.getLogger(__name__)
INITIAL_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 60 * 60
MAX_IDLE_WAIT_SECONDS = 5 * 60
OUTBOX_CHECK_SECONDS = 30
WORKER_JOIN_SECONDS = 5


@dataclass(frozen=True, slots=True)
class _AccountRetry:
    failures: int
    retry_at: float


class MailboxConnector(Protocol):
    def fetch_since(self, since: date) -> ImapFetchResult: ...

    def fetch_after(
        self, uid: int, expected_uid_validity: int | None = None
    ) -> ImapFetchResult: ...

    def wait_for_change(self, timeout: float) -> None: ...


ConnectorFactory = Callable[[AccountConfig, bool], MailboxConnector]


def _default_connector_factory(
    account: AccountConfig, include_text: bool
) -> MailboxConnector:
    if include_text:
        return ImapConnector(account, include_text=True)
    return ImapConnector(account)


class MailSentryService:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        connector_factory: ConnectorFactory = _default_connector_factory,
        notifier: NotificationSender | None = None,
        outbox_check_seconds: float = OUTBOX_CHECK_SECONDS,
        retry_delay: Callable[[int], float] | None = None,
    ) -> None:
        self.config = config
        self.storage = Storage(config.storage.path)
        self.storage.initialize()
        cloud = (
            ResponsesCompatibleClassifier(config.classification)
            if config.classification.cloud.enabled
            else None
        )
        self.classifier = RuleClassifier(
            config.classification,
            refiner=cloud.refine if cloud else None,
        )
        self.notifier = (
            notifier
            if notifier is not None
            else AppriseNotifier(config.notifications.urls())
        )
        self._connector_factory = connector_factory
        self._clock = clock
        self._wall_clock = wall_clock or _utc_now
        self._outbox_check_seconds = outbox_check_seconds
        self._retry_delay = retry_delay or _retry_delay
        self._account_retries: dict[str, _AccountRetry] = {}

    def bootstrap(self, days: int = 1) -> int:
        total = 0
        since = date.today() - timedelta(days=days)
        for account in self.config.accounts:
            connector = self._connector_factory(
                account, self.config.classification.cloud.enabled
            )
            result = connector.fetch_since(since)
            for candidate in result.candidates:
                self._record(candidate, notify=False)
                total += 1
            self.storage.set_cursor(account.id, result.uid_validity, result.last_uid)
            LOGGER.info(
                "bootstrapped account=%s candidates=%s last_uid=%s",
                account.id,
                len(result.candidates),
                result.last_uid,
            )
        return total

    def process_once(self) -> tuple[int, int, int]:
        observed = 0
        for account in self.config.accounts:
            retry = self._account_retries.get(account.id)
            if retry is not None and self._clock() < retry.retry_at:
                continue
            try:
                observed += self._process_account(account)
            except Exception:
                retry_delay = self._defer_account(account.id)
                LOGGER.exception(
                    "account processing failed account=%s retry_in_seconds=%s",
                    account.id,
                    retry_delay,
                )
            else:
                self._account_retries.pop(account.id, None)
        delivered, failed = self._dispatch_and_retain()
        return observed, delivered, failed

    def _dispatch_and_retain(self) -> tuple[int, int]:
        self._queue_due_digest(self._wall_clock())
        delivered, failed = dispatch_outbox(self.storage, self.notifier)
        purged = self.storage.purge(self.config.storage.retention_days)
        if purged:
            LOGGER.info("expired metadata purged events=%s", purged)
        return delivered, failed

    def _queue_due_digest(self, now: datetime) -> None:
        slot = _latest_digest_slot(
            now,
            self.config.timezone,
            self.config.notifications.digest_times,
        )
        events = self.storage.pending_digest_events(slot)
        title, body = _format_digest(events)
        self.storage.queue_digest(
            slot,
            [int(event["id"]) for event in events],
            title,
            body,
            now=now,
        )

    def _defer_account(self, account_id: str) -> float:
        previous = self._account_retries.get(account_id)
        failures = 1 if previous is None else previous.failures + 1
        delay = self._retry_delay(failures)
        self._account_retries[account_id] = _AccountRetry(
            failures=failures,
            retry_at=self._clock() + delay,
        )
        return delay

    def run_forever(self) -> None:
        stop_event = threading.Event()
        try:
            self.run_until(stop_event)
        except KeyboardInterrupt:
            LOGGER.info("shutdown requested")

    def run_until(self, stop_event: threading.Event) -> None:
        outbox_ready = threading.Event()
        workers = [
            threading.Thread(
                target=self._run_account_worker,
                args=(account, stop_event, outbox_ready),
                name=f"mailsentry-{account.id}",
                daemon=True,
            )
            for account in self.config.accounts
        ]
        for worker in workers:
            worker.start()
        LOGGER.info("monitoring workers started accounts=%s", len(workers))
        try:
            while not stop_event.is_set():
                outbox_ready.clear()
                try:
                    delivered, failed = self._dispatch_and_retain()
                    LOGGER.info(
                        "notification cycle complete delivered=%s failed=%s",
                        delivered,
                        failed,
                    )
                except Exception:
                    LOGGER.exception("notification cycle failed")
                if stop_event.is_set():
                    break
                outbox_ready.wait(self._outbox_check_seconds)
        finally:
            stop_event.set()
            outbox_ready.set()
            for worker in workers:
                worker.join(WORKER_JOIN_SECONDS)

    def _run_account_worker(
        self,
        account: AccountConfig,
        stop_event: threading.Event,
        outbox_ready: threading.Event,
    ) -> None:
        failures = 0
        idle_timeout = min(self.config.poll_seconds, MAX_IDLE_WAIT_SECONDS)
        while not stop_event.is_set():
            try:
                observed = self._process_account(account)
                if observed:
                    outbox_ready.set()
                if stop_event.is_set():
                    break
                self._connector_factory(account, False).wait_for_change(idle_timeout)
            except Exception:
                failures += 1
                retry_delay = self._retry_delay(failures)
                LOGGER.exception(
                    "account worker failed account=%s retry_in_seconds=%s",
                    account.id,
                    retry_delay,
                )
                stop_event.wait(retry_delay)
            else:
                failures = 0

    def _process_account(self, account: AccountConfig) -> int:
        cursor = self.storage.get_cursor(account.id)
        if cursor is None:
            raise RuntimeError(
                f"account {account.id!r} has not been bootstrapped; run bootstrap first"
            )
        uid_validity, last_uid = cursor
        result = self._connector_factory(
            account, self.config.classification.cloud.enabled
        ).fetch_after(last_uid, uid_validity)
        for candidate in result.candidates:
            self._record(candidate, notify=True)
            self.storage.set_cursor(account.id, candidate.uid_validity, candidate.uid)
        self.storage.set_cursor(account.id, result.uid_validity, result.last_uid)
        return len(result.candidates)

    def _record(self, candidate: MailCandidate, *, notify: bool) -> None:
        feedback = self.storage.sender_feedback(candidate.sender)
        assessment = self.classifier.assess(candidate, feedback)
        event_id, created = self.storage.save_assessment(candidate, assessment)
        if created and not notify and assessment.level is AlertLevel.DIGEST:
            self.storage.suppress_digest(event_id)
        if not created or not notify or assessment.level is not AlertLevel.IMMEDIATE:
            return
        reasons = "; ".join(assessment.reasons)
        body = (
            f"Account: {candidate.account_id}\n"
            f"From: {candidate.sender}\n"
            f"Subject: {candidate.subject}\n"
            f"Score: {assessment.score}\n"
            f"Why: {reasons}\n"
            f"Event ID: {event_id}"
        )
        self.storage.queue_notification(event_id, "Important email detected", body)


def _retry_delay(failures: int) -> int:
    exponent = min(failures - 1, 6)
    return min(INITIAL_RETRY_SECONDS * (2**exponent), MAX_RETRY_SECONDS)


def _latest_digest_slot(
    now: datetime,
    timezone_name: str,
    digest_times: tuple[wall_time, ...],
) -> datetime:
    if not digest_times:
        raise ValueError("at least one digest time is required")
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    slots = [
        datetime.combine(local_now.date(), digest_time, timezone)
        for digest_time in digest_times
        if digest_time <= local_now.timetz().replace(tzinfo=None)
    ]
    if slots:
        return max(slots).astimezone(UTC)
    previous_day = local_now.date() - timedelta(days=1)
    return datetime.combine(previous_day, digest_times[-1], timezone).astimezone(UTC)


def _format_digest(events: list) -> tuple[str, str]:
    count = len(events)
    title = f"MailSentry digest: {count} noteworthy email(s)"
    if not events:
        return title, ""
    entries: list[str] = []
    for event in events:
        reasons = "; ".join(json.loads(str(event["reasons_json"])))
        entries.append(
            f"Account: {event['account_id']}\n"
            f"From: {event['sender']}\n"
            f"Subject: {event['subject']}\n"
            f"Score: {event['score']}\n"
            f"Why: {reasons}\n"
            f"Event ID: {event['id']}"
        )
    return title, "\n\n".join(entries)


def _utc_now() -> datetime:
    return datetime.now(UTC)
