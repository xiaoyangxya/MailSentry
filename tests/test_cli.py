from pathlib import Path

from typer.testing import CliRunner

from mailsentry.cli import app

RUNNER = CliRunner()


def write_config(
    path: Path,
    database_path: Path,
    *,
    password_env: str = "CLI_TEST_PASSWORD",
    notifications_env: str = "CLI_TEST_NOTIFICATIONS",
) -> None:
    path.write_text(
        f"""
accounts:
  - id: personal
    host: imap.example.com
    username: me@example.com
    password_env: {password_env}
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
