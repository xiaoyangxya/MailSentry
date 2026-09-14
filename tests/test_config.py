from datetime import time
from pathlib import Path

import pytest

from mailsentry.config import ConfigurationError, load_config


def write_config(path: Path, extra: str = "") -> None:
    path.write_text(
        """
accounts:
  - id: personal
    host: imap.example.com
    username: me@example.com
    password_env: MAIL_PASSWORD
storage:
  path: test.db
"""
        + extra,
        encoding="utf-8",
    )


def test_loads_minimal_secure_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_config(path)

    config = load_config(path)

    assert config.accounts[0].tls is True
    assert config.accounts[0].port == 993
    assert config.storage.path == Path("test.db")
    assert config.poll_seconds == 300
    assert config.notifications.digest_times == (time(9), time(18))


def test_rejects_plaintext_imap(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
accounts:
  - id: personal
    host: imap.example.com
    username: me@example.com
    password_env: MAIL_PASSWORD
    tls: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="must use TLS"):
        load_config(path)


def test_rejects_invalid_threshold_order(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_config(
        path,
        """
classification:
  immediate_threshold: 40
  digest_threshold: 50
""",
    )

    with pytest.raises(ConfigurationError, match="thresholds"):
        load_config(path)


def test_loads_custom_digest_times_in_order(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_config(
        path,
        """
notifications:
  digest_times:
    - "18:30"
    - "08:15"
""",
    )

    config = load_config(path)

    assert config.notifications.digest_times == (time(8, 15), time(18, 30))


def test_rejects_unknown_timezone(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_config(path, "\ntimezone: Not/A_Timezone\n")

    with pytest.raises(ConfigurationError, match="unknown timezone"):
        load_config(path)
