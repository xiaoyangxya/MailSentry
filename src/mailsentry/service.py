from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from mailsentry.classifier import RuleClassifier
from mailsentry.config import AccountConfig, ServiceConfig
from mailsentry.connectors import ImapConnector
from mailsentry.models import AlertLevel, MailCandidate
from mailsentry.notifier import AppriseNotifier, dispatch_outbox
from mailsentry.storage import Storage

LOGGER = logging.getLogger(__name__)
INITIAL_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class _AccountRetry:
    failures: int
    retry_at: float


class MailSentryService:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.storage = Storage(config.storage.path)
        self.storage.initialize()
        self.classifier = RuleClassifier(config.classification)
        self.notifier = AppriseNotifier(config.notifications.urls())
        self._clock = clock
        self._account_retries: dict[str, _AccountRetry] = {}

    def bootstrap(self, days: int = 1) -> int:
        total = 0
        since = date.today() - timedelta(days=days)
        for account in self.config.accounts:
            connector = ImapConnector(account)
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
        delivered, failed = dispatch_outbox(self.storage, self.notifier)
        return observed, delivered, failed

    def _defer_account(self, account_id: str) -> int:
        previous = self._account_retries.get(account_id)
        failures = 1 if previous is None else previous.failures + 1
        exponent = min(failures - 1, 6)
        delay = min(INITIAL_RETRY_SECONDS * (2**exponent), MAX_RETRY_SECONDS)
        self._account_retries[account_id] = _AccountRetry(
            failures=failures,
            retry_at=self._clock() + delay,
        )
        return delay

    def run_forever(self) -> None:
        while True:
            try:
                observed, delivered, failed = self.process_once()
                LOGGER.info(
                    "cycle complete observed=%s delivered=%s failed=%s",
                    observed,
                    delivered,
                    failed,
                )
            except Exception:
                LOGGER.exception("monitoring cycle failed")
            time.sleep(self.config.poll_seconds)

    def _process_account(self, account: AccountConfig) -> int:
        cursor = self.storage.get_cursor(account.id)
        if cursor is None:
            raise RuntimeError(
                f"account {account.id!r} has not been bootstrapped; run bootstrap first"
            )
        uid_validity, last_uid = cursor
        result = ImapConnector(account).fetch_after(last_uid, uid_validity)
        for candidate in result.candidates:
            self._record(candidate, notify=True)
            self.storage.set_cursor(account.id, candidate.uid_validity, candidate.uid)
        self.storage.set_cursor(account.id, result.uid_validity, result.last_uid)
        return len(result.candidates)

    def _record(self, candidate: MailCandidate, *, notify: bool) -> None:
        feedback = self.storage.sender_feedback(candidate.sender)
        assessment = self.classifier.assess(candidate, feedback)
        event_id, created = self.storage.save_assessment(candidate, assessment)
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
