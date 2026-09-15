from datetime import UTC, datetime

import pytest

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


@pytest.mark.parametrize(
    "priority_header",
    (
        b"Importance: high\r\n",
        b"Priority: urgent\r\n",
        b"X-MSMail-Priority: High\r\n",
        b"X-Priority: 1 (Highest)\r\n",
        b"X-Priority: 2 (High)\r\n",
    ),
)
def test_parses_common_high_priority_headers(priority_header: bytes) -> None:
    parsed = candidate_from_imap(
        account_id="personal",
        uid=8,
        uid_validity=11,
        header_bytes=b"Subject: Please review\r\n" + priority_header + b"\r\n",
        flags=frozenset(),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        body_structure=None,
    )

    assert parsed.has_priority_header is True


def test_does_not_treat_low_priority_headers_as_high_priority() -> None:
    parsed = candidate_from_imap(
        account_id="personal",
        uid=9,
        uid_validity=11,
        header_bytes=(
            b"Importance: low\r\n"
            b"Priority: non-urgent\r\n"
            b"X-MSMail-Priority: Low\r\n"
            b"X-Priority: 10 (Invalid)\r\n\r\n"
        ),
        flags=frozenset(),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        body_structure=None,
    )

    assert parsed.has_priority_header is False


def test_extracts_and_limits_clean_plain_text_for_transient_cloud_input() -> None:
    parsed = candidate_from_imap(
        account_id="personal",
        uid=10,
        uid_validity=11,
        header_bytes=b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
        flags=frozenset(),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        body_structure=None,
        body_bytes=("  hello\x00   world  " + "x" * 4_100).encode(),
    )

    assert parsed.text_excerpt.startswith("hello world ")
    assert len(parsed.text_excerpt) == 4_000


def test_extracts_html_text_without_active_or_visual_markup() -> None:
    parsed = candidate_from_imap(
        account_id="personal",
        uid=11,
        uid_validity=11,
        header_bytes=b"Content-Type: text/html; charset=utf-8\r\n\r\n",
        flags=frozenset(),
        internal_date=datetime(2026, 1, 1, tzinfo=UTC),
        body_structure=None,
        body_bytes=(
            b"<style>secret-style</style><p>Hello <b>team</b></p>"
            b"<script>secret-script</script><svg>secret-svg</svg>"
        ),
    )

    assert parsed.text_excerpt == "Hello team"
