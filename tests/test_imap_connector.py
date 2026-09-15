from datetime import date, timedelta

import pytest

from mailsentry.config import AccountConfig
from mailsentry.connectors.imap import HEADER_FIELDS, TEXT_PREVIEW_FIELD, ImapConnector


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


class IdleClient(EmptyMailboxClient):
    def __init__(self) -> None:
        super().__init__(uid_validity=11, uid_next=1)
        self.idle_started = False
        self.idle_timeouts: list[float] = []
        self.idle_finished = False

    def idle(self) -> None:
        self.idle_started = True

    def idle_check(self, timeout: float):
        self.idle_timeouts.append(timeout)
        return []

    def idle_done(self) -> None:
        self.idle_finished = True


def account() -> AccountConfig:
    return AccountConfig(
        id="personal",
        host="imap.example.com",
        username="me@example.com",
        password_env="MAIL_PASSWORD",
    )


def test_fetch_requests_common_priority_headers() -> None:
    for header_name in ("IMPORTANCE", "X-PRIORITY", "PRIORITY", "X-MSMAIL-PRIORITY"):
        assert header_name in HEADER_FIELDS


def test_fetches_text_preview_only_when_cloud_input_is_enabled(monkeypatch) -> None:
    class MessageClient(EmptyMailboxClient):
        def __init__(self) -> None:
            super().__init__(uid_validity=11, uid_next=2)
            self.fetch_fields: list[str] = []

        def search(self, criteria: list[object]) -> list[int]:
            return [1]

        def fetch(self, uids: list[int], fields: list[str]):
            self.fetch_fields = fields
            return {
                1: {
                    b"BODY[HEADER.FIELDS (SUBJECT CONTENT-TYPE)]": (
                        b"Subject: Hello\r\nContent-Type: text/plain\r\n\r\n"
                    ),
                    b"BODY[TEXT]<0>": b"transient body",
                }
            }

    plain_client = MessageClient()
    plain = ImapConnector(account())
    monkeypatch.setattr(plain, "_client", lambda: plain_client)
    plain_result = plain.fetch_after(0, 11)

    cloud_client = MessageClient()
    cloud = ImapConnector(account(), include_text=True)
    monkeypatch.setattr(cloud, "_client", lambda: cloud_client)
    cloud_result = cloud.fetch_after(0, 11)

    assert TEXT_PREVIEW_FIELD not in plain_client.fetch_fields
    assert plain_result.candidates[0].text_excerpt == ""
    assert TEXT_PREVIEW_FIELD in cloud_client.fetch_fields
    assert cloud_result.candidates[0].text_excerpt == "transient body"


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


def test_wait_for_change_uses_read_only_idle_and_ends_session(monkeypatch) -> None:
    client = IdleClient()
    connector = ImapConnector(account())
    monkeypatch.setattr(connector, "_client", lambda: client)

    connector.wait_for_change(timeout=300)

    assert client.idle_started is True
    assert client.idle_timeouts == [300]
    assert client.idle_finished is True


def test_wait_for_change_ends_idle_session_after_check_failure(monkeypatch) -> None:
    class FailingIdleClient(IdleClient):
        def idle_check(self, timeout: float):
            self.idle_timeouts.append(timeout)
            raise ConnectionError("server disconnected")

    client = FailingIdleClient()
    connector = ImapConnector(account())
    monkeypatch.setattr(connector, "_client", lambda: client)

    with pytest.raises(ConnectionError, match="server disconnected"):
        connector.wait_for_change(timeout=300)

    assert client.idle_finished is True
