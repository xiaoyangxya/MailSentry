from __future__ import annotations

import apprise

from mailsentry.storage import Storage


class AppriseNotifier:
    def __init__(self, urls: tuple[str, ...]) -> None:
        self._apprise = apprise.Apprise()
        for url in urls:
            if not self._apprise.add(url):
                raise ValueError("an Apprise notification URL is invalid")

    def send(self, title: str, body: str) -> bool:
        return bool(self._apprise.notify(title=title, body=body))


def dispatch_outbox(storage: Storage, notifier: AppriseNotifier) -> tuple[int, int]:
    delivered = 0
    failed = 0
    for item in storage.pending_notifications():
        success = notifier.send(str(item["title"]), str(item["body"]))
        storage.mark_notification(int(item["id"]), success)
        if success:
            delivered += 1
        else:
            failed += 1
    return delivered, failed
