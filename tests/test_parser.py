from datetime import UTC, datetime

from mailsentry.parser import candidate_from_imap


def test_parses_headers_without_persisting_a_body() -> None:
    headers = (
        b"Message-ID: <one@example.com>\r\n"
        b"From: Sender <sender@example.com>\r\n"
        b"To: Me <me@example.com>\r\n"
        b"Subject: =?utf-8?b?57Sn5oCl6LSm5Y2V?=\r\n"
        b"Date: Thu, 01 Jan 2026 12:00:00 +0800\r\n"
        b"List-Unsubscribe: <mailto:leave@example.com>\r\n\r\n"
    )
    body_structure = (
        b"APPLICATION",
        b"PDF",
        (b"NAME", b"invoice.pdf"),
        None,
        None,
        b"BASE64",
        1234,
        None,
        (b"ATTACHMENT", (b"FILENAME", b"invoice.pdf")),
    )

    parsed = candidate_from_imap(
        account_id="personal",
        uid=7,
        uid_validity=11,
        header_bytes=headers,
        flags=frozenset({"$Important"}),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        body_structure=body_structure,
    )

    assert parsed.sender == "sender@example.com"
    assert parsed.subject == "紧急账单"
    assert parsed.has_list_unsubscribe is True
    assert parsed.attachments[0].filename == "invoice.pdf"
    assert parsed.attachments[0].size == 1234
