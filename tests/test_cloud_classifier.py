import json
from datetime import UTC, datetime

import pytest

from mailsentry.cloud_classifier import (
    CloudAssessmentError,
    ResponsesCompatibleClassifier,
)
from mailsentry.config import ClassificationConfig, CloudClassificationConfig
from mailsentry.models import (
    AlertLevel,
    Assessment,
    AttachmentMetadata,
    MailCandidate,
)


def candidate() -> MailCandidate:
    return MailCandidate(
        account_id="private-account",
        uid=42,
        uid_validity=7,
        message_id="<private-id@example.com>",
        sender="sender@example.com",
        recipients=("private-recipient@example.com",),
        subject="Please review",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        attachments=(AttachmentMetadata("report.pdf", "application/pdf", 123),),
        text_excerpt="x" * 4_500,
    )


def response(score: int = 91) -> bytes:
    return json.dumps(
        {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {"score": score, "reasons": ["Action is requested"]}
                            ),
                        }
                    ],
                },
            ],
        }
    ).encode()


def test_sends_minimal_payload_to_custom_responses_provider(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "test-secret")
    captured = {}

    def transport(request, timeout: float) -> bytes:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data)
        return response()

    config = ClassificationConfig(
        cloud=CloudClassificationConfig(
            enabled=True,
            base_url="https://provider.example/v1",
            model="provider-model",
            api_key_env="CUSTOM_PROVIDER_KEY",
            timeout_seconds=9,
        )
    )
    assessment = ResponsesCompatibleClassifier(config, transport=transport).refine(
        candidate(), Assessment(60, AlertLevel.DIGEST, ("local reason",))
    )

    assert captured["url"] == "https://provider.example/v1/responses"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["timeout"] == 9
    payload = captured["payload"]
    assert payload["model"] == "provider-model"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    mail_input = json.loads(payload["input"])
    assert set(mail_input) == {"sender", "subject", "attachments", "text"}
    assert len(mail_input["text"]) == 4_000
    assert "private-account" not in payload["input"]
    assert "private-recipient" not in payload["input"]
    assert "private-id" not in payload["input"]
    assert assessment.score == 91
    assert assessment.level is AlertLevel.IMMEDIATE
    assert "cloud: Action is requested" in assessment.reasons


@pytest.mark.parametrize(
    "raw_response",
    (
        b"not-json",
        b'{"status":"failed"}',
        b'{"status":"completed","output":[]}',
        response(101),
    ),
)
def test_rejects_invalid_or_unstructured_provider_output(
    raw_response: bytes, monkeypatch
) -> None:
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "test-secret")
    config = ClassificationConfig(
        cloud=CloudClassificationConfig(
            enabled=True,
            base_url="https://provider.example",
            model="model",
            api_key_env="CUSTOM_PROVIDER_KEY",
        )
    )

    with pytest.raises(CloudAssessmentError):
        ResponsesCompatibleClassifier(
            config, transport=lambda request, timeout: raw_response
        ).refine(candidate(), Assessment(60, AlertLevel.DIGEST, ("local",)))
