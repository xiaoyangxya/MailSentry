from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from mailsentry.config import ClassificationConfig
from mailsentry.models import AlertLevel, Assessment, MailCandidate

LOGGER = logging.getLogger(__name__)
AssessmentRefiner = Callable[[MailCandidate, Assessment], Assessment]

PROTECTED_SUBJECT_SIGNALS = (
    "payment",
    "invoice",
    "security alert",
    "verification code",
    "one-time password",
    "password reset",
    "suspicious login",
    "fraud",
    "付款",
    "支付",
    "账单",
    "安全提醒",
    "验证码",
    "一次性密码",
    "密码重置",
    "异常登录",
    "欺诈",
)


class RuleClassifier:
    """Conservative, explainable classifier used before any optional cloud model."""

    def __init__(
        self,
        config: ClassificationConfig,
        *,
        refiner: AssessmentRefiner | None = None,
    ) -> None:
        self._config = config
        self._refiner = refiner

    def assess(self, candidate: MailCandidate, sender_feedback: int = 0) -> Assessment:
        sender = candidate.sender.lower()
        if _matches_sender(sender, self._config.allow_senders):
            return Assessment(100, AlertLevel.IMMEDIATE, ("sender is explicitly allowed",))
        if _matches_sender(sender, self._config.deny_senders):
            return Assessment(0, AlertLevel.AUDIT, ("sender is explicitly denied",))

        score = 20
        reasons: list[str] = ["baseline"]

        reputation = _configured_reputation(sender, self._config.sender_reputation)
        if reputation:
            score += reputation
            reasons.append(f"configured sender reputation: {reputation:+d}")

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
            has_protected_signal = any(
                signal in subject for signal in PROTECTED_SUBJECT_SIGNALS
            )
            if adjustment < 0 and has_protected_signal:
                reasons.append("negative sender feedback ignored for protected subject signal")
            else:
                score += adjustment
                reasons.append(f"sender feedback adjustment: {adjustment:+d}")

        score = max(0, min(100, score))
        if score >= self._config.immediate_threshold:
            level = AlertLevel.IMMEDIATE
        elif score >= self._config.digest_threshold:
            level = AlertLevel.DIGEST
        else:
            level = AlertLevel.AUDIT
        rule_assessment = Assessment(score, level, tuple(reasons))
        if self._refiner is None or level is not AlertLevel.DIGEST:
            return rule_assessment
        try:
            return self._refiner(candidate, rule_assessment)
        except Exception as exc:
            LOGGER.warning(
                "cloud assessment failed error=%s; using rule assessment",
                type(exc).__name__,
            )
            return rule_assessment


def _matches_sender(sender: str, configured: tuple[str, ...]) -> bool:
    address_domain = sender.rsplit("@", maxsplit=1)[-1]
    return any(
        sender == value or (value.startswith("@") and address_domain == value[1:])
        for value in configured
    )


def _configured_reputation(
    sender: str,
    configured: tuple[tuple[str, int], ...],
) -> int:
    for identity, adjustment in configured:
        if not identity.startswith("@") and sender == identity:
            return adjustment
    address_domain = sender.rsplit("@", maxsplit=1)[-1]
    for identity, adjustment in configured:
        if identity.startswith("@") and address_domain == identity[1:]:
            return adjustment
    return 0
