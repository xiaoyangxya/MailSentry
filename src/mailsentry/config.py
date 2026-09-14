from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    """Raised when configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class AccountConfig:
    id: str
    host: str
    username: str
    password_env: str
    port: int = 993
    folder: str = "INBOX"
    tls: bool = True

    def password(self) -> str:
        value = os.getenv(self.password_env)
        if not value:
            raise ConfigurationError(
                f"account {self.id!r} requires environment variable {self.password_env!r}"
            )
        return value


@dataclass(frozen=True, slots=True)
class ClassificationConfig:
    allow_senders: tuple[str, ...] = ()
    deny_senders: tuple[str, ...] = ()
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

    def urls(self) -> tuple[str, ...]:
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


def load_config(path: Path) -> ServiceConfig:
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
                port=int(account.get("port", 993)),
                folder=str(account.get("folder", "INBOX")),
                tls=bool(account.get("tls", True)),
            )
        except KeyError as exc:
            raise ConfigurationError(
                f"accounts[{index}] is missing required field {exc.args[0]!r}"
            ) from exc
        if not parsed.tls:
            raise ConfigurationError(f"account {parsed.id!r} must use TLS")
        if parsed.id in seen_ids:
            raise ConfigurationError(f"duplicate account id {parsed.id!r}")
        seen_ids.add(parsed.id)
        accounts.append(parsed)

    class_raw = _mapping(root.get("classification"), "classification")
    classification = ClassificationConfig(
        allow_senders=tuple(str(v).lower() for v in class_raw.get("allow_senders", [])),
        deny_senders=tuple(str(v).lower() for v in class_raw.get("deny_senders", [])),
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
            urls_env=str(notify_raw.get("urls_env", "MAILSENTRY_APPRISE_URLS"))
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
    return config
