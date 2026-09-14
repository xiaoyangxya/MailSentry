from __future__ import annotations

from collections.abc import Iterable
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


class ImapConnector:
    """Read-only IMAP access for one Monitored Account."""

    def __init__(self, account: AccountConfig) -> None:
        self._account = account

    def check(self) -> None:
        with self._client() as client:
            client.select_folder(self._account.folder, readonly=True)

    def folder_cursor(self) -> tuple[int, int]:
        with self._client() as client:
            status = client.select_folder(self._account.folder, readonly=True)
            return int(status[b"UIDVALIDITY"]), max(0, int(status.get(b"UIDNEXT", 1)) - 1)

    def fetch_after(
        self, uid: int, expected_uid_validity: int | None = None
    ) -> list[MailCandidate]:
        with self._client() as client:
            status = client.select_folder(self._account.folder, readonly=True)
            uid_validity = int(status[b"UIDVALIDITY"])
            if expected_uid_validity is not None and uid_validity != expected_uid_validity:
                uids = client.search(["SINCE", date.today() - timedelta(days=1)])
            else:
                uids = client.search(["UID", f"{uid + 1}:*"])
            return self._fetch(client, uids, uid_validity)

    def fetch_since(self, since: date) -> list[MailCandidate]:
        with self._client() as client:
            status = client.select_folder(self._account.folder, readonly=True)
            uid_validity = int(status[b"UIDVALIDITY"])
            uids = client.search(["SINCE", since])
            return self._fetch(client, uids, uid_validity)

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
        self, client: IMAPClient, uids: Iterable[int], uid_validity: int
    ) -> list[MailCandidate]:
        ordered_uids = sorted(int(uid) for uid in uids)
        if not ordered_uids:
            return []
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
        return candidates


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
