from datetime import date, timedelta

from mailsentry.config import AccountConfig
from mailsentry.connectors.imap import ImapConnector


class EmptyMailboxClient:
    def __init__(self, uid_validity: int, uid_next: int) -> None:
        self.status = {
            b"UIDVALIDITY": uid_validity,
            b"UIDNEXT": uid_next,
        }
        self.searches: list[list[object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        pass

    def select_folder(self, folder: str, *, readonly: bool):
        assert folder == "INBOX"
        assert readonly is True
        return self.status

    def search(self, criteria: list[object]) -> list[int]:
        self.searches.append(criteria)
        return []


def account() -> AccountConfig:
    return AccountConfig(
        id="personal",
        host="imap.example.com",
        username="me@example.com",
        password_env="MAIL_PASSWORD",
    )


def test_fetch_after_reconciles_changed_uidvalidity_without_candidates(
    monkeypatch,
) -> None:
    client = EmptyMailboxClient(uid_validity=22, uid_next=8)
    connector = ImapConnector(account())
    monkeypatch.setattr(connector, "_client", lambda: client)

    result = connector.fetch_after(uid=99, expected_uid_validity=11)

    assert result.candidates == ()
    assert result.uid_validity == 22
    assert result.last_uid == 7
    assert client.searches == [["SINCE", date.today() - timedelta(days=1)]]


def test_fetch_after_uses_saved_uid_when_uidvalidity_matches(monkeypatch) -> None:
    client = EmptyMailboxClient(uid_validity=11, uid_next=100)
    connector = ImapConnector(account())
    monkeypatch.setattr(connector, "_client", lambda: client)

    result = connector.fetch_after(uid=42, expected_uid_validity=11)

    assert result.candidates == ()
    assert result.uid_validity == 11
    assert result.last_uid == 99
    assert client.searches == [["UID", "43:*"]]
