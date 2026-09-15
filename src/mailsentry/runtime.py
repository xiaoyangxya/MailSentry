from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mailsentry.config import ServiceConfig
from mailsentry.service import MailSentryService


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    running: bool
    revision: str | None
    error: str | None
    accounts: dict[str, dict[str, str | None]]


class RuntimeSupervisor:
    """Own one live MailSentry runtime and replace it without exposing thread details."""

    def __init__(
        self,
        service_factory: Callable[[ServiceConfig], MailSentryService] = MailSentryService,
    ) -> None:
        self._service_factory = service_factory
        self._lock = threading.RLock()
        self._service: MailSentryService | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._revision: str | None = None
        self._error: str | None = None

    def start(self, config: ServiceConfig, revision: str) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("runtime is already running")
            service = self._service_factory(config)
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(service, stop),
                name="mailsentry-runtime",
                daemon=True,
            )
            self._service = service
            self._stop = stop
            self._thread = thread
            self._revision = revision
            self._error = None
            thread.start()

    def reload(self, config: ServiceConfig, revision: str) -> None:
        replacement = self._service_factory(config)
        with self._lock:
            previous_config = self._service.config if self._service else None
            previous_revision = self._revision
            self._stop_locked()
            try:
                self.start(replacement.config, revision)
            except Exception:
                if previous_config is not None and previous_revision is not None:
                    self.start(previous_config, previous_revision)
                raise

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def status(self) -> RuntimeStatus:
        with self._lock:
            return RuntimeStatus(
                running=bool(self._thread and self._thread.is_alive()),
                revision=self._revision,
                error=self._error,
                accounts=self._service.health() if self._service else {},
            )

    def _run(self, service: MailSentryService, stop: threading.Event) -> None:
        try:
            service.run_until(stop)
        except Exception as exc:
            with self._lock:
                self._error = type(exc).__name__

    def _stop_locked(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
            if self._thread.is_alive():
                raise RuntimeError("runtime did not stop within 10 seconds")
        self._service = None
        self._thread = None
        self._stop = None
