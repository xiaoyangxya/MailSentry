from __future__ import annotations

from pathlib import Path

from mailsentry.config import ClassificationConfig
from mailsentry.models import AlertLevel, Assessment, MailCandidate


class RuleClassifier:
    """Conservative, explainable classifier used before any optional cloud model."""

    def __init__(self, config: ClassificationConfig) -> None:
        self._config = config

    def assess(self, candidate: MailCandidate, sender_feedback: int = 0) -> Assessment:
        sender = candidate.sender.lower()
        if _matches_sender(sender, self._config.allow_senders):
            return Assessment(100, AlertLevel.IMMEDIATE, ("sender is explicitly allowed",))
        if _matches_sender(sender, self._config.deny_senders):
            return Assessment(0, AlertLevel.AUDIT, ("sender is explicitly denied",))

        score = 20
        reasons: list[str] = ["baseline"]

        normalized_flags = {flag.lower() for flag in candidate.flags}
        if {"important", "$important", "\\important"} & normalized_flags:
            score += 40
            reasons.append("provider marked the message important")
        elif candidate.has_priority_header:
            score += 40
            reasons.append("message has a high-priority header")

        subject = candidate.subject.lower()
        matched_keywords = [
            keyword for keyword in self._config.important_keywords if keyword in subject
        ]
        if matched_keywords:
            score += 35
            reasons.append(f"important subject keyword: {matched_keywords[0]}")

        important_extensions = set(self._config.important_attachment_extensions)
        if any(
            attachment.filename and Path(attachment.filename.lower()).suffix in important_extensions
            for attachment in candidate.attachments
        ):
            score += 15
            reasons.append("message has a potentially important attachment")

        if candidate.has_list_unsubscribe:
            score -= 20
            reasons.append("bulk-mail unsubscribe header")

        if sender_feedback:
            adjustment = max(-30, min(30, sender_feedback))
            score += adjustment
            reasons.append(f"sender feedback adjustment: {adjustment:+d}")

        score = max(0, min(100, score))
        if score >= self._config.immediate_threshold:
            level = AlertLevel.IMMEDIATE
        elif score >= self._config.digest_threshold:
            level = AlertLevel.DIGEST
        else:
            level = AlertLevel.AUDIT
        return Assessment(score, level, tuple(reasons))


def _matches_sender(sender: str, configured: tuple[str, ...]) -> bool:
    address_domain = sender.rsplit("@", maxsplit=1)[-1]
    return any(
        sender == value or (value.startswith("@") and address_domain == value[1:])
        for value in configured
    )
