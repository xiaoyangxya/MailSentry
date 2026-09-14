from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from imapclient import IMAPClient

from mailsentry.config import AccountConfig
from mailsentry.models import MailCandidate
from mailsentry.parser import candidate_from_imap

HEADER_FIELDS = (
    "BODY.PEEK[HEADER.FIELDS "
    "(MESSAGE-ID FROM TO SUBJECT DATE LIST-UNSUBSCRIBE X-PRIORITY IMPORTANCE)]"
)
FETCH_FIELDS = [HEADER_FIELDS, "BODYSTRUCTURE", "FLAGS", "INTERNALDATE"]


@dataclass(frozen=True, slots=True)
class ImapFetchResult:
    candidates: tuple[MailCandidate, ...]
    uid_validity: int
    last_uid: int


class ImapConnector:
    """Read-only IMAP access for one Monitored Account."""

    def __init__(self, account: AccountConfig) -> None:
        self._account = account

    def check(self) -> None:
        with self._client() as client:
            client.select_folder(self._account.folder, readonly=True)

    def wait_for_change(self, timeout: float) -> None:
        with self._client() as client:
            client.select_folder(self._account.folder, readonly=True)
            client.idle()
            try:
                client.idle_check(timeout=timeout)
            finally:
                client.idle_done()

    def fetch_after(
        self, uid: int, expected_uid_validity: int | None = None
    ) -> ImapFetchResult:
        with self._client() as client:
            status = client.select_folder(self._account.folder, readonly=True)
            uid_validity = int(status[b"UIDVALIDITY"])
            if expected_uid_validity is not None and uid_validity != expected_uid_validity:
                uids = client.search(["SINCE", date.today() - timedelta(days=1)])
            else:
                uids = client.search(["UID", f"{uid + 1}:*"])
            return self._fetch(client, uids, status)

    def fetch_since(self, since: date) -> ImapFetchResult:
        with self._client() as client:
            status = client.select_folder(self._account.folder, readonly=True)
            uids = client.search(["SINCE", since])
            return self._fetch(client, uids, status)

    def _client(self) -> IMAPClient:
        client = IMAPClient(
            self._account.host,
            port=self._account.port,
            ssl=self._account.tls,
            timeout=30,
        )
        client.login(self._account.username, self._account.password())
        return client

    def _fetch(
        self, client: IMAPClient, uids: Iterable[int], status: dict[Any, Any]
    ) -> ImapFetchResult:
        uid_validity = int(status[b"UIDVALIDITY"])
        selected_last_uid = max(0, int(status.get(b"UIDNEXT", 1)) - 1)
        ordered_uids = sorted(int(uid) for uid in uids)
        if not ordered_uids:
            return ImapFetchResult((), uid_validity, selected_last_uid)
        response = client.fetch(ordered_uids, FETCH_FIELDS)
        candidates: list[MailCandidate] = []
        for uid in ordered_uids:
            data = response[uid]
            header = _header_bytes(data)
            candidates.append(
                candidate_from_imap(
                    account_id=self._account.id,
                    uid=uid,
                    uid_validity=uid_validity,
                    header_bytes=header,
                    flags=frozenset(_decode_flags(data.get(b"FLAGS", ()))),
                    internal_date=data.get(b"INTERNALDATE"),
                    body_structure=data.get(b"BODYSTRUCTURE"),
                )
            )
        return ImapFetchResult(
            candidates=tuple(candidates),
            uid_validity=uid_validity,
            last_uid=max(selected_last_uid, ordered_uids[-1]),
        )


def _header_bytes(data: dict[Any, Any]) -> bytes:
    for key, value in data.items():
        if isinstance(key, bytes) and key.startswith(b"BODY[") and isinstance(value, bytes):
            return value
    return b""


def _decode_flags(flags: Iterable[Any]) -> list[str]:
    return [
        flag.decode("utf-8", errors="replace") if isinstance(flag, bytes) else str(flag)
        for flag in flags
    ]
