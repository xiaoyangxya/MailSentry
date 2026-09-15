from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class AlertLevel(StrEnum):
    IMMEDIATE = "immediate"
    DIGEST = "digest"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    filename: str | None
    content_type: str
    size: int | None = None


@dataclass(frozen=True, slots=True)
class MailCandidate:
    account_id: str
    uid: int
    uid_validity: int
    message_id: str | None
    sender: str
    recipients: tuple[str, ...]
    subject: str
    received_at: datetime
    flags: frozenset[str] = frozenset()
    has_priority_header: bool = False
    has_list_unsubscribe: bool = False
    attachments: tuple[AttachmentMetadata, ...] = ()
    text_excerpt: str = ""


@dataclass(frozen=True, slots=True)
class Assessment:
    score: int
    level: AlertLevel
    reasons: tuple[str, ...] = field(default_factory=tuple)
