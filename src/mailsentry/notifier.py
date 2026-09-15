from __future__ import annotations

from datetime import datetime
from typing import Protocol

import apprise

from mailsentry.storage import Storage


class NotificationSender(Protocol):
    def send(self, title: str, body: str) -> bool: ...


class AppriseNotifier:
    def __init__(self, urls: tuple[str, ...]) -> None:
        self._apprise = apprise.Apprise()
        for url in urls:
            if not self._apprise.add(url):
                raise ValueError("an Apprise notification URL is invalid")

    def send(self, title: str, body: str) -> bool:
        return bool(self._apprise.notify(title=title, body=body))


def dispatch_outbox(
    storage: Storage,
    notifier: NotificationSender,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    delivered = 0
    failed = 0
    for item in storage.pending_notifications(now=now):
        success = notifier.send(str(item["title"]), str(item["body"]))
        storage.mark_notification(int(item["id"]), success, now=now)
        if success:
            delivered += 1
        else:
            failed += 1
    return delivered, failed
