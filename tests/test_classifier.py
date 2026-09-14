from datetime import UTC, datetime

import pytest

from mailsentry.classifier import RuleClassifier
from mailsentry.config import ClassificationConfig
from mailsentry.models import AlertLevel, AttachmentMetadata, MailCandidate


def candidate(**changes):
    values = {
        "account_id": "personal",
        "uid": 1,
        "uid_validity": 10,
        "message_id": "<one@example.com>",
        "sender": "person@example.com",
        "recipients": ("me@example.com",),
        "subject": "Hello",
        "received_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    return MailCandidate(**values)


def test_allow_sender_is_immediate() -> None:
    classifier = RuleClassifier(ClassificationConfig(allow_senders=("person@example.com",)))

    assessment = classifier.assess(candidate())

    assert assessment.score == 100
    assert assessment.level is AlertLevel.IMMEDIATE


def test_domain_deny_is_audit_only() -> None:
    classifier = RuleClassifier(ClassificationConfig(deny_senders=("@example.com",)))

    assessment = classifier.assess(candidate())

    assert assessment.score == 0
    assert assessment.level is AlertLevel.AUDIT


def test_provider_keyword_and_attachment_reach_immediate_level() -> None:
    classifier = RuleClassifier(ClassificationConfig())

    assessment = classifier.assess(
        candidate(
            subject="Urgent invoice",
            flags=frozenset({"$Important"}),
            attachments=(AttachmentMetadata("invoice.pdf", "application/pdf", 1200),),
        )
    )

    assert assessment.score == 100
    assert assessment.level is AlertLevel.IMMEDIATE
    assert len(assessment.reasons) == 4


@pytest.mark.parametrize("flag", ("Important", "$Important", "\\Important"))
def test_gmail_importance_flags_raise_the_score(flag: str) -> None:
    classifier = RuleClassifier(ClassificationConfig())

    assessment = classifier.assess(candidate(flags=frozenset({flag})))

    assert assessment.score == 60
    assert assessment.level is AlertLevel.DIGEST
    assert "provider marked the message important" in assessment.reasons


def test_high_priority_header_raises_the_score_once() -> None:
    classifier = RuleClassifier(ClassificationConfig())

    assessment = classifier.assess(
        candidate(
            flags=frozenset({"\\Important"}),
            has_priority_header=True,
        )
    )

    assert assessment.score == 60
    assert assessment.reasons.count("provider marked the message important") == 1
    assert "message has a high-priority header" not in assessment.reasons


def test_high_priority_header_is_explainable_without_provider_flag() -> None:
    classifier = RuleClassifier(ClassificationConfig())

    assessment = classifier.assess(candidate(has_priority_header=True))

    assert assessment.score == 60
    assert assessment.level is AlertLevel.DIGEST
    assert "message has a high-priority header" in assessment.reasons


def test_bulk_mail_penalty_and_feedback_are_bounded() -> None:
    classifier = RuleClassifier(ClassificationConfig())

    assessment = classifier.assess(candidate(has_list_unsubscribe=True), sender_feedback=-100)

    assert assessment.score == 0
    assert assessment.level is AlertLevel.AUDIT
