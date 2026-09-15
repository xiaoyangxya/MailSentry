import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from mailsentry.cli import app
from mailsentry.models import AlertLevel, Assessment, MailCandidate
from mailsentry.storage import Storage

RUNNER = CliRunner()


def write_config(
    path: Path,
    database_path: Path,
    *,
    password_env: str = "CLI_TEST_PASSWORD",
    notifications_env: str = "CLI_TEST_NOTIFICATIONS",
    classification: str = "",
) -> None:
    path.write_text(
        f"""
accounts:
  - id: personal
    host: imap.example.com
    username: me@example.com
    password_env: {password_env}
{classification}
notifications:
  urls_env: {notifications_env}
storage:
  path: {database_path.as_posix()}
""",
        encoding="utf-8",
    )


def test_doctor_loads_local_dotenv(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "mail.db")
    (tmp_path / ".env").write_text("CLI_TEST_PASSWORD=local-secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLI_TEST_PASSWORD", raising=False)
    monkeypatch.delenv("CLI_TEST_NOTIFICATIONS", raising=False)

    result = RUNNER.invoke(
        app,
        ["doctor", "--config", str(config_path), "--no-connect"],
    )

    assert result.exit_code == 0
    assert "OK account=personal" in result.output


def test_doctor_reports_missing_secret_without_traceback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        tmp_path / "mail.db",
        password_env="MISSING_CLI_TEST_PASSWORD",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSING_CLI_TEST_PASSWORD", raising=False)

    result = RUNNER.invoke(
        app,
        ["doctor", "--config", str(config_path), "--no-connect"],
    )

    assert result.exit_code == 2
    assert "Configuration error:" in result.output
    assert "MISSING_CLI_TEST_PASSWORD" in result.output
    assert "Traceback" not in result.output


def test_doctor_can_send_notification_test(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "mail.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLI_TEST_PASSWORD", "secret")
    monkeypatch.setenv("CLI_TEST_NOTIFICATIONS", "mock://notification")
    sent: list[tuple[tuple[str, ...], str, str]] = []

    class RecordingNotifier:
        def __init__(self, urls: tuple[str, ...]) -> None:
            self.urls = urls

        def send(self, title: str, body: str) -> bool:
            sent.append((self.urls, title, body))
            return True

    monkeypatch.setattr("mailsentry.cli.AppriseNotifier", RecordingNotifier)

    result = RUNNER.invoke(
        app,
        ["doctor", "--config", str(config_path), "--no-connect", "--notify"],
    )

    assert result.exit_code == 0
    assert "OK notifications=1 tested=true" in result.output
    assert sent == [
        (
            ("mock://notification",),
            "MailSentry doctor",
            "Notification channel test succeeded.",
        )
    ]


def test_explain_assesses_without_saving_or_notifying(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "mail.db"
    write_config(
        config_path,
        database_path,
        classification="""classification:
  sender_reputation:
    "@example.com": 15
""",
    )
    monkeypatch.chdir(tmp_path)

    result = RUNNER.invoke(
        app,
        [
            "explain",
            "--config",
            str(config_path),
            "--sender",
            "person@example.com",
            "--subject",
            "Security alert",
            "--priority-header",
            "--attachment",
            "report.pdf",
        ],
    )

    assert result.exit_code == 0
    assert '"score": 100' in result.output
    assert '"level": "immediate"' in result.output
    assert "configured sender reputation: +15" in result.output
    assert "message has a high-priority header" in result.output
    assert "important subject keyword: security alert" in result.output
    assert "message has a potentially important attachment" in result.output
    assert Storage(database_path).recent_assessments() == []


def test_explain_can_explicitly_test_configured_cloud_provider(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "mail.db"
    write_config(
        config_path,
        database_path,
        classification="""classification:
  cloud:
    enabled: true
    base_url: https://api.deepseek.com
    model: deepseek-flash
    api_key_env: CLI_TEST_CLOUD_KEY
""",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLI_TEST_CLOUD_KEY", "secret")
    seen_text: list[str] = []

    class FakeCloudClassifier:
        def __init__(self, classification) -> None:
            assert classification.cloud.model == "deepseek-flash"

        def refine(self, candidate, assessment):
            seen_text.append(candidate.text_excerpt)
            return Assessment(88, AlertLevel.IMMEDIATE, ("cloud: needs action",))

    monkeypatch.setattr(
        "mailsentry.cli.ResponsesCompatibleClassifier", FakeCloudClassifier
    )

    result = RUNNER.invoke(
        app,
        [
            "explain",
            "--config",
            str(config_path),
            "--sender",
            "person@example.com",
            "--subject",
            "Please review",
            "--text",
            "Synthetic message only",
            "--cloud",
        ],
    )

    assert result.exit_code == 0
    assert '"score": 88' in result.output
    assert seen_text == ["Synthetic message only"]
    assert Storage(database_path).recent_assessments() == []


def test_status_reports_saved_health_without_connecting(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "mail.db"
    write_config(config_path, database_path)
    storage = Storage(database_path)
    storage.initialize()
    storage.set_cursor("personal", uid_validity=1, last_uid=42)
    monkeypatch.chdir(tmp_path)

    result = RUNNER.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert '"account": "personal"' in lines[0]
    assert '"last_uid": 42' in lines[0]
    assert '"connection": "not-checked"' in lines[0]
    assert '"notifications_pending": 0' in lines[1]


def test_status_connect_checks_every_account_and_fails_if_one_is_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "mail.db")
    monkeypatch.chdir(tmp_path)

    class OfflineConnector:
        def __init__(self, account) -> None:
            self.account = account

        def check(self) -> None:
            raise ConnectionError("mailbox unavailable")

    monkeypatch.setattr("mailsentry.cli.ImapConnector", OfflineConnector)

    result = RUNNER.invoke(
        app,
        ["status", "--config", str(config_path), "--connect"],
    )

    assert result.exit_code == 1
    assert '"connection": "failed"' in result.output
    assert '"error": "mailbox unavailable"' in result.output


def test_events_prints_recent_assessments_and_filters_level(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "mail.db"
    write_config(config_path, database_path)
    storage = Storage(database_path)
    storage.initialize()
    storage.save_assessment(
        MailCandidate(
            account_id="personal",
            uid=1,
            uid_validity=1,
            message_id="<cli-event@example.com>",
            sender="sender@example.com",
            recipients=("me@example.com",),
            subject="Important subject",
            received_at=datetime(2026, 9, 15, tzinfo=UTC),
        ),
        Assessment(90, AlertLevel.IMMEDIATE, ("important keyword",)),
    )
    monkeypatch.chdir(tmp_path)

    result = RUNNER.invoke(
        app,
        [
            "events",
            "--config",
            str(config_path),
            "--level",
            "immediate",
        ],
    )

    assert result.exit_code == 0
    assert '"event_id": 1' in result.output
    assert '"level": "immediate"' in result.output
    assert '"reasons": ["important keyword"]' in result.output


def test_events_rejects_unknown_level(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "mail.db")
    monkeypatch.chdir(tmp_path)

    result = RUNNER.invoke(
        app,
        ["events", "--config", str(config_path), "--level", "urgent"],
    )

    assert result.exit_code == 2
    assert "level must be 'immediate', 'digest', or 'audit'" in result.output


def test_soak_command_runs_without_config_or_external_services(tmp_path: Path) -> None:
    database_path = tmp_path / "soak.db"
    metrics_path = tmp_path / "soak.jsonl"

    result = RUNNER.invoke(
        app,
        [
            "soak",
            "--database",
            str(database_path),
            "--metrics",
            str(metrics_path),
            "--duration-hours",
            "0.0034",
            "--event-interval-seconds",
            "3",
            "--accelerated",
        ],
    )

    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary["passed"] is True
    assert summary["release_evidence"] is False
    assert summary["accounts"] == 10
    assert database_path.exists()
    assert metrics_path.exists()
