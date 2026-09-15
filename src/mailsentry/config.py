from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigurationError(ValueError):
    """Raised when configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class CloudClassificationConfig:
    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    model: str = ""
    api_key_env: str = "MAILSENTRY_CLOUD_API_KEY"
    timeout_seconds: float = 15.0
    _api_key: str | None = field(default=None, repr=False, compare=False)

    def api_key(self) -> str:
        value = self._api_key or os.getenv(self.api_key_env)
        if not value:
            raise ConfigurationError(
                f"cloud classifier requires environment variable {self.api_key_env!r}"
            )
        return value


@dataclass(frozen=True, slots=True)
class AccountConfig:
    id: str
    host: str
    username: str
    password_env: str
    connector: str = "imap"
    enabled: bool = True
    port: int = 993
    folder: str = "INBOX"
    tls: bool = True
    _password: str | None = field(default=None, repr=False, compare=False)

    def password(self) -> str:
        value = self._password or os.getenv(self.password_env)
        if not value:
            raise ConfigurationError(
                f"account {self.id!r} requires environment variable {self.password_env!r}"
            )
        return value


@dataclass(frozen=True, slots=True)
class ClassificationConfig:
    allow_senders: tuple[str, ...] = ()
    deny_senders: tuple[str, ...] = ()
    sender_reputation: tuple[tuple[str, int], ...] = ()
    cloud: CloudClassificationConfig = field(default_factory=CloudClassificationConfig)
    important_keywords: tuple[str, ...] = (
        "urgent",
        "deadline",
        "invoice",
        "payment",
        "security alert",
        "紧急",
        "截止",
        "账单",
        "付款",
        "安全提醒",
    )
    important_attachment_extensions: tuple[str, ...] = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
    )
    immediate_threshold: int = 80
    digest_threshold: int = 50


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    urls_env: str = "MAILSENTRY_APPRISE_URLS"
    digest_times: tuple[time, ...] = (time(9), time(18))
    _urls: tuple[str, ...] | None = field(default=None, repr=False, compare=False)

    def urls(self) -> tuple[str, ...]:
        if self._urls is not None:
            return self._urls
        raw = os.getenv(self.urls_env, "")
        return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class StorageConfig:
    path: Path = Path("data/mailsentry.db")
    retention_days: int = 30


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    accounts: tuple[AccountConfig, ...]
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    poll_seconds: int = 300
    timezone: str = "Asia/Shanghai"


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a mapping")
    return value


def _digest_times(value: Any) -> tuple[time, ...]:
    if value is None:
        return NotificationConfig().digest_times
    if not isinstance(value, list) or not value:
        raise ConfigurationError("notifications.digest_times must be a non-empty list")
    parsed: list[time] = []
    for index, item in enumerate(value):
        try:
            parsed_time = time.fromisoformat(str(item))
        except ValueError as exc:
            raise ConfigurationError(
                f"notifications.digest_times[{index}] must use HH:MM format"
            ) from exc
        if parsed_time.second or parsed_time.microsecond:
            raise ConfigurationError(
                f"notifications.digest_times[{index}] must use minute precision"
            )
        parsed.append(parsed_time)
    return tuple(sorted(set(parsed)))


def _sender_reputation(value: Any) -> tuple[tuple[str, int], ...]:
    raw = _mapping(value, "classification.sender_reputation")
    parsed: list[tuple[str, int]] = []
    for identity, raw_adjustment in raw.items():
        normalized = str(identity).strip().lower()
        if "@" not in normalized or normalized == "@":
            raise ConfigurationError(
                "classification.sender_reputation keys must be email addresses or @domains"
            )
        if isinstance(raw_adjustment, bool):
            raise ConfigurationError(
                f"classification.sender_reputation[{identity!r}] must be an integer"
            )
        try:
            adjustment = int(raw_adjustment)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"classification.sender_reputation[{identity!r}] must be an integer"
            ) from exc
        if not -30 <= adjustment <= 30:
            raise ConfigurationError(
                f"classification.sender_reputation[{identity!r}] must be between -30 and 30"
            )
        parsed.append((normalized, adjustment))
    return tuple(parsed)


def _cloud_classification(
    value: Any, secrets: dict[str, str] | None = None
) -> CloudClassificationConfig:
    raw = _mapping(value, "classification.cloud")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigurationError("classification.cloud.enabled must be a boolean")
    model = str(raw.get("model", "")).strip()
    base_url = str(raw.get("base_url", "https://api.deepseek.com")).strip().rstrip("/")
    api_key_env = str(raw.get("api_key_env", "MAILSENTRY_CLOUD_API_KEY")).strip()
    try:
        timeout_seconds = float(raw.get("timeout_seconds", 15))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("classification.cloud.timeout_seconds must be a number") from exc
    if enabled and not model:
        raise ConfigurationError("classification.cloud.model is required when enabled")
    if not base_url.startswith("https://"):
        raise ConfigurationError("classification.cloud.base_url must use HTTPS")
    if not api_key_env:
        raise ConfigurationError("classification.cloud.api_key_env must not be empty")
    if not 1 <= timeout_seconds <= 60:
        raise ConfigurationError(
            "classification.cloud.timeout_seconds must be between 1 and 60"
        )
    return CloudClassificationConfig(
        enabled=enabled,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        _api_key=(secrets or {}).get(api_key_env),
    )


def load_config(path: Path, *, secrets: dict[str, str] | None = None) -> ServiceConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML: {exc}") from exc

    root = _mapping(raw, "root")
    raw_accounts = root.get("accounts", [])
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigurationError("at least one account is required")

    accounts: list[AccountConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_accounts):
        account = _mapping(item, f"accounts[{index}]")
        try:
            parsed = AccountConfig(
                id=str(account["id"]),
                host=str(account["host"]),
                username=str(account["username"]),
                password_env=str(account["password_env"]),
                connector=str(account.get("connector", "imap")),
                enabled=account.get("enabled", True),
                port=int(account.get("port", 993)),
                folder=str(account.get("folder", "INBOX")),
                tls=bool(account.get("tls", True)),
                _password=(secrets or {}).get(str(account["password_env"])),
            )
        except KeyError as exc:
            raise ConfigurationError(
                f"accounts[{index}] is missing required field {exc.args[0]!r}"
            ) from exc
        if not parsed.tls:
            raise ConfigurationError(f"account {parsed.id!r} must use TLS")
        if parsed.connector != "imap":
            raise ConfigurationError(
                f"account {parsed.id!r} uses unsupported connector {parsed.connector!r}"
            )
        if not isinstance(parsed.enabled, bool):
            raise ConfigurationError(f"account {parsed.id!r} enabled must be a boolean")
        if parsed.id in seen_ids:
            raise ConfigurationError(f"duplicate account id {parsed.id!r}")
        seen_ids.add(parsed.id)
        accounts.append(parsed)

    class_raw = _mapping(root.get("classification"), "classification")
    classification = ClassificationConfig(
        allow_senders=tuple(str(v).lower() for v in class_raw.get("allow_senders", [])),
        deny_senders=tuple(str(v).lower() for v in class_raw.get("deny_senders", [])),
        sender_reputation=_sender_reputation(class_raw.get("sender_reputation")),
        cloud=_cloud_classification(class_raw.get("cloud"), secrets),
        important_keywords=tuple(
            str(v).lower()
            for v in class_raw.get("important_keywords", ClassificationConfig().important_keywords)
        ),
        important_attachment_extensions=tuple(
            str(v).lower()
            for v in class_raw.get(
                "important_attachment_extensions",
                ClassificationConfig().important_attachment_extensions,
            )
        ),
        immediate_threshold=int(class_raw.get("immediate_threshold", 80)),
        digest_threshold=int(class_raw.get("digest_threshold", 50)),
    )
    if not 0 <= classification.digest_threshold < classification.immediate_threshold <= 100:
        raise ConfigurationError(
            "thresholds must satisfy 0 <= digest_threshold < immediate_threshold <= 100"
        )

    notify_raw = _mapping(root.get("notifications"), "notifications")
    storage_raw = _mapping(root.get("storage"), "storage")
    config = ServiceConfig(
        accounts=tuple(accounts),
        classification=classification,
        notifications=NotificationConfig(
            urls_env=str(notify_raw.get("urls_env", "MAILSENTRY_APPRISE_URLS")),
            digest_times=_digest_times(notify_raw.get("digest_times")),
            _urls=tuple(
                item.strip()
                for item in (secrets or {})
                .get(
                    str(notify_raw.get("urls_env", "MAILSENTRY_APPRISE_URLS")),
                    "",
                )
                .split(",")
                if item.strip()
            )
            if secrets is not None
            else None,
        ),
        storage=StorageConfig(
            path=Path(storage_raw.get("path", "data/mailsentry.db")),
            retention_days=int(storage_raw.get("retention_days", 30)),
        ),
        poll_seconds=int(root.get("poll_seconds", 300)),
        timezone=str(root.get("timezone", "Asia/Shanghai")),
    )
    if config.poll_seconds < 30:
        raise ConfigurationError("poll_seconds must be at least 30")
    if config.storage.retention_days < 1:
        raise ConfigurationError("retention_days must be at least 1")
    try:
        ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"unknown timezone {config.timezone!r}") from exc
    return config
