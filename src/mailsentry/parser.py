from __future__ import annotations

from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from mailsentry.models import AttachmentMetadata, MailCandidate


def candidate_from_imap(
    *,
    account_id: str,
    uid: int,
    uid_validity: int,
    header_bytes: bytes,
    flags: frozenset[str],
    internal_date: datetime | None,
    body_structure: Any,
) -> MailCandidate:
    headers = BytesHeaderParser(policy=default).parsebytes(header_bytes)
    sender_addresses = getaddresses(headers.get_all("from", []))
    recipient_addresses = getaddresses(headers.get_all("to", []))
    sender = sender_addresses[0][1] if sender_addresses else "unknown"
    recipients = tuple(address for _, address in recipient_addresses if address)
    received_at = _message_date(headers.get("date"), internal_date)
    return MailCandidate(
        account_id=account_id,
        uid=uid,
        uid_validity=uid_validity,
        message_id=headers.get("message-id"),
        sender=sender,
        recipients=recipients,
        subject=_decode(headers.get("subject", "")),
        received_at=received_at,
        flags=flags,
        has_priority_header=_has_high_priority_header(headers),
        has_list_unsubscribe="list-unsubscribe" in headers,
        attachments=tuple(_attachments(body_structure)),
    )


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _has_high_priority_header(headers: Any) -> bool:
    importance = _decode(headers.get("importance", "")).strip().casefold()
    priority = _decode(headers.get("priority", "")).strip().casefold()
    ms_priority = _decode(headers.get("x-msmail-priority", "")).strip().casefold()
    x_priority = _decode(headers.get("x-priority", "")).strip().casefold()
    x_priority_rank = x_priority.split(maxsplit=1)[0] if x_priority else ""
    return (
        importance == "high"
        or priority == "urgent"
        or ms_priority == "high"
        or x_priority_rank in {"1", "2"}
        or x_priority in {"high", "highest"}
    )


def _message_date(header: str | None, internal_date: datetime | None) -> datetime:
    if header:
        try:
            parsed = parsedate_to_datetime(header)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
        except (TypeError, ValueError, OverflowError):
            pass
    if internal_date:
        return internal_date if internal_date.tzinfo else internal_date.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _attachments(node: Any) -> list[AttachmentMetadata]:
    found: list[AttachmentMetadata] = []
    _walk_body_structure(node, found)
    return found


def _walk_body_structure(node: Any, found: list[AttachmentMetadata]) -> None:
    if not isinstance(node, (tuple, list)) or not node:
        return

    if isinstance(node[0], (tuple, list)):
        for part in node:
            if (
                isinstance(part, (tuple, list))
                and part
                and isinstance(part[0], (tuple, list, bytes))
            ):
                _walk_body_structure(part, found)
        return

    if len(node) < 2:
        return
    media_type = _text(node[0]).lower()
    media_subtype = _text(node[1]).lower()
    parameters = _parameter_map(node[2] if len(node) > 2 else None)
    disposition = _find_disposition(node)
    filename = parameters.get("name") or disposition.get("filename")
    is_attachment = disposition.get("type") == "attachment" or filename is not None
    if is_attachment:
        size = node[6] if len(node) > 6 and isinstance(node[6], int) else None
        found.append(
            AttachmentMetadata(
                filename=filename,
                content_type=f"{media_type}/{media_subtype}",
                size=size,
            )
        )


def _parameter_map(value: Any) -> dict[str, str]:
    if not isinstance(value, (tuple, list)):
        return {}
    result: dict[str, str] = {}
    for index in range(0, len(value) - 1, 2):
        result[_text(value[index]).lower()] = _text(value[index + 1])
    return result


def _find_disposition(node: tuple[Any, ...] | list[Any]) -> dict[str, str]:
    for value in reversed(node[7:]):
        if not isinstance(value, (tuple, list)) or not value:
            continue
        disposition_type = _text(value[0]).lower()
        if disposition_type not in {"attachment", "inline"}:
            continue
        result = _parameter_map(value[1] if len(value) > 1 else None)
        result["type"] = disposition_type
        return result
    return {}


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
