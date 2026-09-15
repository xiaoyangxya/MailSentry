from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyotp
import pytest
from fastapi.testclient import TestClient

from mailsentry.application import ControlPanel, discord_webhook_to_apprise
from mailsentry.auth import AuthManager
from mailsentry.managed_config import ConfigStore, RevisionConflict
from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.web import SESSION_COOKIE, create_app, localize_assessment_reason


def config_text(database: Path) -> str:
    return f"""
timezone: Asia/Shanghai
accounts:
  - id: personal
    connector: imap
    enabled: true
    host: imap.example.com
    username: me@example.com
    password_env: PANEL_MAIL_PASSWORD
    tls: true
notifications:
  urls_env: PANEL_NOTIFICATIONS
storage:
  path: {database.as_posix()}
"""


def test_managed_config_stages_activates_and_never_exports_secrets(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path / "managed")
    first = store.stage(
        config_text(tmp_path / "mail.db"),
        secret_updates={
            "PANEL_MAIL_PASSWORD": "mail-secret",
            "PANEL_NOTIFICATIONS": "mock://notification-secret",
        },
        expected_revision=None,
    )

    assert first.config.accounts[0].password() == "mail-secret"
    assert first.config.notifications.urls() == ("mock://notification-secret",)
    active = store.activate(first.id, expected_revision=None)
    exported = store.export_redacted()

    assert active.id == first.id
    assert "mail-secret" not in exported
    assert "notification-secret" not in exported
    assert "must-be-provided" in exported

    with pytest.raises(RevisionConflict):
        store.stage(config_text(tmp_path / "mail.db"), expected_revision=None)


def test_managed_config_preserves_blank_secrets_and_can_restore(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path / "managed")
    first = store.stage(
        config_text(tmp_path / "mail.db"),
        secret_updates={"PANEL_MAIL_PASSWORD": "first"},
        expected_revision=None,
    )
    store.activate(first.id, expected_revision=None)
    second = store.stage(
        config_text(tmp_path / "mail.db"),
        secret_updates={"PANEL_MAIL_PASSWORD": ""},
        expected_revision=first.id,
    )
    store.record_test(second.id, "account:personal")
    store.activate(second.id, expected_revision=first.id)
    assert store.active().config.accounts[0].password() == "first"

    store.restore(first.id)
    assert store.active().id == first.id


def test_admin_setup_sessions_lockout_and_totp(tmp_path: Path) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    auth = AuthManager(tmp_path / "state.db", tmp_path / "totp.secret")
    token = auth.issue_setup_token(now)
    assert token
    assert auth.complete_setup(token, "correct horse battery", now)
    assert not auth.complete_setup(token, "correct horse battery", now)

    for _ in range(5):
        assert auth.login("wrong password", "", "locked", now) is None
    assert auth.login("correct horse battery", "", "locked", now) is None

    session = auth.login("correct horse battery", "", "other", now)
    assert session is not None
    assert auth.authenticate(session.token, now + timedelta(minutes=29)) is not None
    assert auth.authenticate(session.token, now + timedelta(minutes=60)) is None

    secret, _uri = auth.begin_totp()
    assert auth.enable_totp(secret, pyotp.TOTP(secret).at(now), now)
    assert auth.totp_enabled()
    assert auth.login("correct horse battery", "000000", "totp", now) is None
    assert (
        auth.login(
            "correct horse battery",
            pyotp.TOTP(secret).at(now),
            "totp-valid",
            now,
        )
        is not None
    )


def test_web_requires_login_csrf_and_escapes_untrusted_mail(tmp_path: Path) -> None:
    panel = ControlPanel(tmp_path / "managed", tmp_path / "state.db")
    token = panel.auth.issue_setup_token()
    assert token
    app = create_app(panel, allow_insecure_http=True)

    candidate = MailCandidate(
        account_id="personal",
        uid=1,
        uid_validity=1,
        message_id="<xss@example.com>",
        sender="<img src=x onerror=alert(1)>",
        recipients=(),
        subject="<script>alert(1)</script>",
        received_at=datetime.now(UTC),
    )
    event_id, _created = panel.storage.save_assessment(
        candidate,
        Assessment(90, AlertLevel.IMMEDIATE, ("test reason",)),
    )

    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        client.get(f"/setup?token={token}")
        preauth_csrf = client.cookies.get("mailsentry_form_csrf")
        assert preauth_csrf
        setup = client.post(
            "/setup",
            data={
                "token": token,
                "password": "correct horse battery",
                "confirm_password": "correct horse battery",
                "csrf_token": preauth_csrf,
            },
            follow_redirects=False,
        )
        assert setup.status_code == 303
        client.get("/login")
        login = client.post(
            "/login",
            data={
                "password": "correct horse battery",
                "totp_code": "",
                "csrf_token": preauth_csrf,
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert client.cookies.get(SESSION_COOKIE)

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert dashboard.headers["x-frame-options"] == "DENY"
        events = client.get("/events")
        assert "<script>alert(1)</script>" not in events.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in events.text

        session = panel.auth.authenticate(client.cookies.get(SESSION_COOKIE, ""))
        assert session
        accepted = client.post(
            f"/events/{event_id}/feedback",
            data={"csrf_token": session.csrf_token, "verdict": "important"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        duplicate = client.post(
            f"/events/{event_id}/feedback",
            data={"csrf_token": session.csrf_token, "verdict": "not-important"},
            follow_redirects=False,
        )
        assert duplicate.status_code == 409
        assert "Recorded" in client.get("/events").text

        rejected = client.post(
            "/notifications/retry",
            data={"csrf_token": "wrong"},
            follow_redirects=False,
        )
        assert rejected.status_code == 409

        language_response = client.get("/language/zh", follow_redirects=False)
        assert language_response.status_code == 303
        localized_configuration = client.get("/configuration")
        assert '<html lang="zh">' in localized_configuration.text
        assert "邮箱秘密变量名" in localized_configuration.text
        assert "替换邮箱密码" in localized_configuration.text
        assert "留空则保留当前值" in localized_configuration.text
        assert "校验并保存草稿" in localized_configuration.text
        assert "Mailbox secret name" not in localized_configuration.text
        assert "Replace mailbox secret" not in localized_configuration.text
        assert "Validate and save draft" not in localized_configuration.text

        localized_dashboard = client.get("/")
        assert "待发送通知" in localized_dashboard.text
        assert "Pending notifications" not in localized_dashboard.text
        localized_events = client.get("/events")
        assert "提醒级别" in localized_events.text
        assert "Alert level" not in localized_events.text
        localized_security = client.get("/security")
        assert "身份验证器" in localized_security.text
        assert ">Authenticator<" not in localized_security.text


def test_setup_token_expires(tmp_path: Path) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    auth = AuthManager(tmp_path / "state.db", tmp_path / "totp.secret")
    token = auth.issue_setup_token(now)
    assert token
    assert not auth.complete_setup(
        token,
        "correct horse battery",
        now + timedelta(minutes=16),
    )


def test_discord_webhook_is_converted_without_changing_other_apprise_urls() -> None:
    webhook = "https://discord.com/api/webhooks/123/token-value"
    assert discord_webhook_to_apprise(webhook) == "discord://123/token-value"
    assert discord_webhook_to_apprise("ntfys://example/topic") == "ntfys://example/topic"


def test_builtin_assessment_reasons_are_localized_without_changing_data() -> None:
    assert localize_assessment_reason("baseline", "zh") == "基础分"
    assert (
        localize_assessment_reason("important subject keyword: invoice", "zh")
        == "重要主题关键词：invoice"
    )
    assert localize_assessment_reason("baseline", "en") == "baseline"


def test_common_presets_update_a_valid_managed_draft(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    panel = ControlPanel(tmp_path / "managed", database)
    revision = panel.stage_with_presets(
        config_text(database),
        {
            "PANEL_MAIL_PASSWORD": "mail-secret",
            "PANEL_NOTIFICATIONS": (
                "https://discord.com/api/webhooks/123/token-value"
            ),
        },
        None,
        mail_preset="gmail",
        model_preset="openai",
        account_id="primary",
        account_username="owner@gmail.com",
        immediate_threshold="85",
        digest_threshold="55",
        digest_times="08:00,20:00",
        retention_days="45",
        cloud_enabled="false",
    )

    assert revision.config.accounts[0].id == "primary"
    assert revision.config.accounts[0].host == "imap.gmail.com"
    assert revision.config.classification.immediate_threshold == 85
    assert revision.config.notifications.urls() == ("discord://123/token-value",)
    assert revision.config.storage.retention_days == 45


def test_add_account_uses_stable_generated_secret_reference(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    panel = ControlPanel(tmp_path / "managed", database)
    first = panel.config_store.stage(
        config_text(database),
        secret_updates={"PANEL_MAIL_PASSWORD": "first"},
        expected_revision=None,
    )
    panel.config_store.activate(first.id, expected_revision=None)

    draft = panel.stage_new_account(
        account_id="family",
        username="family@gmail.com",
        password="second",
        preset="gmail",
    )
    added = draft.config.accounts[1]

    assert added.id == "family"
    assert added.host == "imap.gmail.com"
    assert added.password_env.startswith("MAILSENTRY_ACCOUNT_")
    assert added.password() == "second"


def test_mailbox_preview_persists_metadata_but_not_transient_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "state.db"
    panel = ControlPanel(tmp_path / "managed", database)
    revision = panel.stage(
        config_text(database),
        {"PANEL_MAIL_PASSWORD": "mail-secret"},
        None,
    )
    marker = "BODY-MUST-STAY-TRANSIENT"
    candidate = MailCandidate(
        account_id="personal",
        uid=1,
        uid_validity=1,
        message_id="<preview@example.com>",
        sender="sender@example.com",
        recipients=("owner@example.com",),
        subject="Preview subject",
        received_at=datetime.now(UTC),
        text_excerpt=marker,
    )

    class PreviewConnector:
        def __init__(self, account, include_text=False) -> None:
            assert account.id == "personal"
            assert include_text is False

        def fetch_since(self, since):
            return SimpleNamespace(candidates=(candidate,), uid_validity=1, last_uid=1)

    monkeypatch.setattr("mailsentry.application.ImapConnector", PreviewConnector)
    summary = panel.preview_account(revision.id, "personal")

    assert summary[0]["subject"] == "Preview subject"
    assert panel.storage.account_preview(revision.id, "personal") == summary
    assert marker.encode() not in database.read_bytes()


def test_owner_can_revoke_other_sessions(tmp_path: Path) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    auth = AuthManager(tmp_path / "state.db", tmp_path / "totp.secret")
    token = auth.issue_setup_token(now)
    assert token and auth.complete_setup(token, "correct horse battery", now)
    current = auth.login("correct horse battery", "", "one", now)
    other = auth.login("correct horse battery", "", "two", now)
    assert current and other

    assert auth.revoke_other_sessions(current.token) == 1
    assert auth.authenticate(current.token, now) is not None
    assert auth.authenticate(other.token, now) is None
