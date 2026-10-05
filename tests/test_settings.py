from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from mailsentry.application import ControlPanel
from mailsentry.managed_config import RevisionConflict
from mailsentry.web import SESSION_COOKIE, create_app


@pytest.fixture
def owner(tmp_path):
    panel = ControlPanel(tmp_path / "managed", tmp_path / "state.db")
    token = panel.auth.issue_setup_token()
    assert panel.auth.complete_setup(token, "correct horse battery")
    session = panel.auth.login("correct horse battery", "", "test")
    client = TestClient(create_app(panel, allow_insecure_http=True))
    client.cookies.set(SESSION_COOKIE, session.token)
    yield panel, client, session.csrf_token
    client.close()
    panel.stop()


def post_settings(owner, section, source="", **values):
    panel, client, csrf = owner
    active = panel.config_store.active()
    response = client.post(
        f"/settings/{section}",
        data={
            "csrf_token": csrf,
            "source_revision": source,
            "expected_revision": active.id if active else "",
            **values,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    revision = parse_qs(urlsplit(response.headers["location"]).query)["draft"][0]
    return panel.config_store.get(revision)


def add_mailbox(owner, source="", alias="personal", preset="gmail"):
    return post_settings(
        owner,
        "accounts",
        source,
        account_id=alias,
        username=f"{alias}@example.com",
        password=f"{alias}-private-password",
        preset=preset,
        host="imap.example.com",
        port="993",
        folder="INBOX",
        enabled="true",
    )


def test_multimailbox_settings_chain_preserves_credentials_and_active_state(owner):
    panel, client, _ = owner
    first = add_mailbox(owner)
    second = add_mailbox(owner, first.id, "work", "custom")
    rules = post_settings(
        owner,
        "rules",
        second.id,
        important_keywords="付款\nurgent\n付款",
        allow_senders="boss@example.com\n@trusted.example",
        deny_senders="@noise.example",
        important_attachment_extensions=".pdf\n.xlsx",
        sender_reputation="@example.com = 10",
        immediate_threshold="80",
        digest_threshold="50",
    )
    ai = post_settings(
        owner,
        "ai",
        rules.id,
        enabled="true",
        base_url="https://provider.example/v1",
        model="example-model",
        timeout_seconds="12",
        secret="private-api-key",
    )
    notify = post_settings(
        owner,
        "notifications",
        ai.id,
        secret="https://discord.com/api/webhooks/123/token",
        digest_times="09:00,18:00",
        timezone="Asia/Shanghai",
    )
    final = post_settings(
        owner, "advanced", notify.id, config_text=notify.config_path.read_text(encoding="utf-8")
    )
    assert [a.id for a in final.config.accounts] == ["personal", "work"]
    assert [a.password() for a in final.config.accounts] == [
        "personal-private-password",
        "work-private-password",
    ]
    assert final.config.classification.cloud.api_key() == "private-api-key"
    assert final.config.notifications.urls() == ("discord://123/token",)
    assert final.config.classification.important_keywords == ("付款", "urgent")
    assert panel.config_store.active() is None
    assert panel.rule_example(final.id, "boss@example.com", "hello").score == 100
    assert panel.rule_example(final.id, "bot@noise.example", "urgent").score == 0
    for language in ("zh", "en"):
        client.cookies.set("mailsentry_language", language)
        for path in (
            "/accounts",
            "/rules",
            "/ai",
            "/notifications",
            "/advanced",
            "/configuration/review",
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert final.id in page.text
            for secret in (
                "personal-private-password",
                "work-private-password",
                "private-api-key",
                "discord://123/token",
            ):
                assert secret not in page.text
    example = client.post(
        "/settings/rules/example",
        data={
            "csrf_token": owner[2],
            "source_revision": final.id,
            "sender": "boss@example.com",
            "subject": "<script>example</script>",
        },
    )
    assert example.status_code == 200
    assert "100 / Immediate" in example.text
    assert "<script>example</script>" not in example.text


def test_edit_remove_and_disable_are_scoped_to_selected_mailbox(owner):
    panel, _, _ = owner
    first = add_mailbox(owner)
    second = add_mailbox(owner, first.id, "work", "custom")
    edited = post_settings(
        owner,
        "accounts",
        second.id,
        original_id="work",
        username="work@example.com",
        host="imap.example.com",
        port="993",
        folder="INBOX",
        enabled="false",
        password="",
    )
    assert edited.config.accounts[0].enabled
    assert not edited.config.accounts[1].enabled
    assert edited.config.accounts[1].password() == "work-private-password"
    removed = post_settings(owner, "accounts", edited.id, original_id="work", action="delete")
    assert len(removed.config.accounts) == 1
    assert len(panel.config_store.get(second.id).config.accounts) == 2


def test_invalid_settings_keep_inputs_but_do_not_echo_secrets(owner):
    panel, client, csrf = owner
    response = client.post(
        "/settings/accounts",
        data={
            "csrf_token": csrf,
            "account_id": "work",
            "username": "work@example.com",
            "preset": "custom",
            "host": "",
            "password": "secret-never-echoed",
            "enabled": "true",
        },
    )
    assert response.status_code == 400
    assert "IMAP host is required" in response.text
    assert 'value="work@example.com"' in response.text
    assert "secret-never-echoed" not in response.text
    assert panel.config_store.active() is None
    assert client.post("/settings/accounts", data={"csrf_token": "wrong"}).status_code == 409


def test_draft_tests_reset_and_stale_basis_cannot_be_saved_or_activated(owner):
    panel, _, _ = owner
    first = add_mailbox(owner)
    panel.config_store.record_test(first.id, "account:personal")
    second = add_mailbox(owner, first.id, "work")
    assert not panel.config_store.test_results(second.id)
    panel.config_store.activate(first.id, expected_revision=None)
    new_draft = panel.stage_new_account(
        account_id="family",
        username="family@example.com",
        password="private",
        preset="gmail",
    )
    panel.config_store.restore(None)
    with pytest.raises(RevisionConflict):
        panel.save_settings(
            "advanced",
            {
                "config_text": new_draft.config_path.read_text(encoding="utf-8"),
            },
            source_revision=new_draft.id,
            expected_revision=None,
        )
    with pytest.raises(RevisionConflict):
        panel.activate(new_draft.id, None)


def test_missing_checks_block_activation_and_empty_secret_keeps_draft_key(owner):
    panel, _, _ = owner
    first = add_mailbox(owner)
    ai = post_settings(
        owner,
        "ai",
        first.id,
        enabled="false",
        base_url="https://example.com",
        model="demo",
        timeout_seconds="15",
        secret="saved-key",
    )
    changed = post_settings(
        owner,
        "ai",
        ai.id,
        enabled="false",
        base_url="https://example.com",
        model="next-model",
        timeout_seconds="15",
        secret="",
    )
    assert changed.config.classification.cloud.api_key() == "saved-key"
    with pytest.raises(ValueError, match="successful tests"):
        panel.activate(changed.id, None)
    assert panel.config_store.active() is None
