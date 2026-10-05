from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from mailsentry.auth import AuthManager
from mailsentry.classifier import RuleClassifier
from mailsentry.cloud_classifier import ResponsesCompatibleClassifier
from mailsentry.config import AccountConfig, ClassificationConfig
from mailsentry.connectors import ImapConnector
from mailsentry.managed_config import ConfigStore, ManagedConfigError, ManagedRevision
from mailsentry.models import MailCandidate
from mailsentry.notifier import AppriseNotifier
from mailsentry.runtime import RuntimeSupervisor
from mailsentry.service import MailSentryService
from mailsentry.storage import Storage


class ControlPanel:
    """Expose control-panel use cases while hiding storage and runtime mechanics."""

    def __init__(
        self,
        managed_root: Path,
        state_database: Path,
        *,
        config_store: ConfigStore | None = None,
        runtime: RuntimeSupervisor | None = None,
    ) -> None:
        self.config_store = config_store or ConfigStore(managed_root)
        self.runtime = runtime or RuntimeSupervisor()
        self.state_database = state_database.resolve()
        self.storage = Storage(state_database)
        self.storage.initialize()
        self.auth = AuthManager(state_database, managed_root / "totp.secret")

    def start(self) -> None:
        active = self.config_store.active()
        if active is None:
            return
        self.config_store.apply_environment(active)
        self.runtime.start(active.config, active.id)

    def stop(self) -> None:
        self.runtime.stop()

    def dashboard(self) -> dict[str, object]:
        active = self.config_store.active()
        runtime = self.runtime.status()
        accounts: list[dict[str, object]] = []
        if active:
            for account in active.config.accounts:
                cursor = self.storage.account_status(account.id)
                accounts.append(
                    {
                        "id": account.id,
                        "host": account.host,
                        "username": account.username,
                        "enabled": account.enabled,
                        "last_scan": str(cursor["updated_at"]) if cursor else None,
                        "last_uid": int(cursor["last_uid"]) if cursor else None,
                        "runtime": runtime.accounts.get(account.id, {}),
                    }
                )
        pending, failed = self.storage.notification_counts()
        return {
            "revision": active.id if active else None,
            "runtime": asdict(runtime),
            "accounts": accounts,
            "notifications_pending": pending,
            "notifications_failed": failed,
        }

    def stage(
        self,
        config_text: str,
        secret_updates: dict[str, str | None],
        expected_revision: str | None,
        *,
        source_revision: str | None = None,
    ) -> ManagedRevision:
        try:
            document = yaml.safe_load(config_text) or {}
            configured_database = Path(
                document.get("storage", {}).get("path", "data/mailsentry.db")
            ).resolve()
        except (AttributeError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise ManagedConfigError("configuration must be a YAML mapping") from exc
        if configured_database != self.state_database:
            raise ManagedConfigError(
                "managed storage.path must match the serve --state-database path"
            )
        revision = self.config_store.stage(
            config_text,
            secret_updates=secret_updates,
            expected_revision=expected_revision,
            source_revision=source_revision,
        )
        self._audit("configuration.stage", revision.id, "success", expected_revision)
        return revision

    def editing_document(self, revision_id: str = "") -> tuple[dict, ManagedRevision | None]:
        revision = self.config_store.get(revision_id) if revision_id else self.config_store.active()
        if revision:
            return yaml.safe_load(revision.config_path.read_text(encoding="utf-8")), revision
        return {
            "accounts": [],
            "storage": {"path": self.state_database.as_posix(), "retention_days": 30},
            "classification": {},
            "notifications": {},
            "timezone": "Asia/Shanghai",
        }, None

    def save_settings(
        self,
        section: str,
        values: dict[str, str],
        *,
        source_revision: str,
        expected_revision: str | None,
    ) -> ManagedRevision:
        """Apply one visible form to a complete draft, retaining other settings and secrets."""
        document, source = self.editing_document(source_revision)
        updates: dict[str, str | None] = {}
        try:
            if section == "accounts":
                accounts = document.setdefault("accounts", [])
                original = values.get("original_id", "")
                account = next((item for item in accounts if item["id"] == original), None)
                if original and account is None:
                    raise ManagedConfigError("unknown account")
                if values.get("action") == "delete":
                    if account is None:
                        raise ManagedConfigError("unknown account")
                    accounts.remove(account)
                    if not accounts:
                        raise ManagedConfigError(
                            "keep at least one mailbox; disable it to pause monitoring"
                        )
                else:
                    account_id = original or values.get("account_id", "").strip()
                    if not re.fullmatch(r"[\w.-]+", account_id) or (
                        not original and any(item["id"] == account_id for item in accounts)
                    ):
                        raise ManagedConfigError("account id must be non-empty and unique")
                    username = values.get("username", "").strip()
                    password = values.get("password", "")
                    host = (
                        "imap.gmail.com"
                        if values.get("preset") == "gmail"
                        else values.get("host", "").strip()
                    )
                    if not username or (account is None and not password):
                        raise ManagedConfigError("mailbox username and password are required")
                    if not host:
                        raise ManagedConfigError("IMAP host is required")
                    if (
                        account
                        and self.storage.get_cursor(account_id) is not None
                        and (
                            account["username"] != username
                            or account["host"] != host
                            or account.get("folder", "INBOX") != (values.get("folder") or "INBOX")
                        )
                    ):
                        raise ManagedConfigError(
                            "add a new mailbox alias to change a monitored mailbox identity"
                        )
                    port = int(values.get("port", "993"))
                    if not 1 <= port <= 65535:
                        raise ManagedConfigError("IMAP port must be between 1 and 65535")
                    if account is None:
                        account = {
                            "id": account_id,
                            "password_env": (
                                f"MAILSENTRY_ACCOUNT_{secrets.token_hex(4).upper()}_PASSWORD"
                            ),
                        }
                        accounts.append(account)
                    account.update(
                        connector="imap",
                        host=host,
                        port=port,
                        username=username,
                        folder=values.get("folder", "INBOX").strip() or "INBOX",
                        tls=True,
                        enabled=values.get("enabled") == "true",
                    )
                    if password:
                        updates[account["password_env"]] = password
            elif section == "rules":
                classification = document.setdefault("classification", {})
                for key in (
                    "important_keywords",
                    "allow_senders",
                    "deny_senders",
                    "important_attachment_extensions",
                ):
                    classification[key] = list(
                        dict.fromkeys(
                            item.strip().lower()
                            for item in values.get(key, "").splitlines()
                            if item.strip()
                        )
                    )
                for key in ("allow_senders", "deny_senders"):
                    if any("@" not in item or item == "@" for item in classification[key]):
                        raise ManagedConfigError("use an email address or @domain, one per line")
                reputation = {}
                for line in values.get("sender_reputation", "").splitlines():
                    if line.strip():
                        identity, adjustment = line.rsplit("=", 1)
                        reputation[identity.strip()] = int(adjustment.strip())
                classification["sender_reputation"] = reputation
                for key in ("immediate_threshold", "digest_threshold"):
                    classification[key] = int(values[key])
            elif section == "ai":
                cloud = document.setdefault("classification", {}).setdefault("cloud", {})
                cloud.update(
                    enabled=values.get("enabled") == "true",
                    base_url=values.get("base_url", "").strip(),
                    model=values.get("model", "").strip(),
                    timeout_seconds=float(values.get("timeout_seconds", "15")),
                )
                key = cloud.setdefault("api_key_env", "MAILSENTRY_CLOUD_API_KEY")
                if values.get("remove_secret") == "yes":
                    updates[key] = None
                elif values.get("secret"):
                    updates[key] = values["secret"]
            elif section == "notifications":
                notifications = document.setdefault("notifications", {})
                notifications["digest_times"] = [
                    item.strip()
                    for item in values.get("digest_times", "").split(",")
                    if item.strip()
                ]
                document["timezone"] = values.get("timezone", "").strip()
                key = notifications.setdefault("urls_env", "MAILSENTRY_APPRISE_URLS")
                if values.get("remove_secret") == "yes":
                    updates[key] = None
                elif values.get("secret"):
                    updates[key] = ",".join(
                        discord_webhook_to_apprise(item.strip())
                        for item in values["secret"].splitlines()
                        if item.strip()
                    )
            elif section == "advanced":
                return self.stage(
                    values.get("config_text", ""),
                    {},
                    expected_revision,
                    source_revision=source.id if source else None,
                )
            else:
                raise ManagedConfigError("unknown settings page")
        except (ValueError, KeyError) as exc:
            if isinstance(exc, ManagedConfigError):
                raise
            raise ManagedConfigError(
                "check numeric fields and sender adjustments (address = score)"
            ) from exc
        return self.stage(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            updates,
            expected_revision,
            source_revision=source.id if source else None,
        )

    def rule_example(self, revision_id: str, sender: str, subject: str):
        _, revision = self.editing_document(revision_id)
        candidate = MailCandidate(
            account_id="example",
            uid=0,
            uid_validity=0,
            message_id=None,
            sender=sender,
            recipients=(),
            subject=subject,
            received_at=datetime.now(UTC),
        )
        return RuleClassifier(
            revision.config.classification if revision else ClassificationConfig()
        ).assess(candidate, self.storage.sender_feedback(sender))

    def stage_with_presets(
        self,
        config_text: str,
        secret_updates: dict[str, str | None],
        expected_revision: str | None,
        *,
        mail_preset: str,
        model_preset: str,
        account_id: str = "",
        account_username: str = "",
        immediate_threshold: str = "",
        digest_threshold: str = "",
        digest_times: str = "",
        retention_days: str = "",
        cloud_enabled: str = "",
        cloud_model: str = "",
    ) -> ManagedRevision:
        try:
            document = yaml.safe_load(config_text) or {}
            if not isinstance(document, dict):
                raise TypeError
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise ManagedConfigError("configuration must be a YAML mapping") from exc
        accounts = document.get("accounts", [])
        if not isinstance(accounts, list):
            raise ManagedConfigError("accounts must be a list")
        if accounts and account_id:
            accounts[0]["id"] = account_id.strip()
        if accounts and account_username:
            accounts[0]["username"] = account_username.strip()
        if mail_preset == "gmail" and accounts:
            accounts[0].update(
                {
                    "connector": "imap",
                    "host": "imap.gmail.com",
                    "port": 993,
                    "folder": "INBOX",
                    "tls": True,
                }
            )
        cloud = document.setdefault("classification", {}).setdefault("cloud", {})
        classification = document["classification"]
        try:
            if immediate_threshold:
                classification["immediate_threshold"] = int(immediate_threshold)
            if digest_threshold:
                classification["digest_threshold"] = int(digest_threshold)
            if retention_days:
                document.setdefault("storage", {})["retention_days"] = int(retention_days)
        except ValueError as exc:
            raise ManagedConfigError("thresholds and retention days must be integers") from exc
        if model_preset == "openai":
            cloud["base_url"] = "https://api.openai.com/v1"
        elif model_preset == "deepseek":
            cloud["base_url"] = "https://api.deepseek.com"
        if cloud_enabled:
            cloud["enabled"] = cloud_enabled == "true"
        if cloud_model:
            cloud["model"] = cloud_model.strip()
        if digest_times:
            document.setdefault("notifications", {})["digest_times"] = [
                value.strip() for value in digest_times.split(",") if value.strip()
            ]
        notifications_key = document.setdefault("notifications", {}).get(
            "urls_env", "MAILSENTRY_APPRISE_URLS"
        )
        value = secret_updates.get(notifications_key)
        if value:
            secret_updates[notifications_key] = discord_webhook_to_apprise(value)
        return self.stage(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            secret_updates,
            expected_revision,
        )

    def stage_new_account(
        self,
        *,
        account_id: str,
        username: str,
        password: str,
        preset: str,
        source_revision: str = "",
        host: str = "",
        port: int = 993,
        folder: str = "INBOX",
    ) -> ManagedRevision:
        active = self.config_store.active()
        return self.save_settings(
            "accounts",
            {
                "account_id": account_id,
                "username": username,
                "password": password,
                "preset": preset,
                "host": host,
                "port": str(port),
                "folder": folder,
                "enabled": "true",
            },
            source_revision=source_revision,
            expected_revision=active.id if active else None,
        )

    def stage_account_enabled(self, account_id: str, enabled: bool) -> ManagedRevision:
        active = self.config_store.active()
        if active is None:
            raise ManagedConfigError("no active configuration")
        document = yaml.safe_load(active.config_path.read_text(encoding="utf-8"))
        for account in document.get("accounts", []):
            if str(account.get("id")) == account_id:
                account["enabled"] = enabled
                return self.stage(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    {},
                    active.id,
                )
        raise ManagedConfigError(f"unknown account {account_id!r}")

    def test_account(self, revision_id: str, account_id: str) -> None:
        revision = self._with_environment(revision_id)
        account = self._account(revision, account_id)
        ImapConnector(account).check()
        self.config_store.record_test(revision.id, f"account:{account.id}")
        self._audit("account.test", account.id, "success", revision.id)

    def preview_account(self, revision_id: str, account_id: str) -> list[dict[str, object]]:
        revision = self._with_environment(revision_id)
        account = self._account(revision, account_id)
        include_text = revision.config.classification.cloud.enabled
        result = ImapConnector(account, include_text=include_text).fetch_since(
            date.today() - timedelta(days=1)
        )
        cloud = (
            ResponsesCompatibleClassifier(revision.config.classification) if include_text else None
        )
        classifier = RuleClassifier(
            revision.config.classification,
            refiner=cloud.refine if cloud else None,
        )
        summary = []
        for candidate in result.candidates[:100]:
            assessment = classifier.assess(
                candidate, self.storage.sender_feedback(candidate.sender)
            )
            summary.append(
                {
                    "sender": candidate.sender,
                    "subject": candidate.subject,
                    "received_at": candidate.received_at.isoformat(),
                    "score": assessment.score,
                    "level": assessment.level.value,
                    "reasons": list(assessment.reasons),
                }
            )
        self.storage.save_account_preview(revision.id, account.id, summary)
        self.config_store.record_test(revision.id, f"preview:{account.id}")
        self._audit("account.preview", account.id, "success", revision.id)
        return summary

    def test_notifications(self, revision_id: str, *, send: bool) -> None:
        revision = self._with_environment(revision_id)
        urls = revision.config.notifications.urls()
        notifier = AppriseNotifier(urls)
        if send and not notifier.send(
            "MailSentry control panel test",
            "The configured notification channel is working.",
        ):
            raise ManagedConfigError("notification test failed")
        self.config_store.record_test(revision.id, "notifications")
        self._audit("notifications.test", "all", "success", revision.id)

    def test_cloud(self, revision_id: str) -> None:
        revision = self._with_environment(revision_id)
        config = revision.config
        if not config.classification.cloud.enabled:
            raise ManagedConfigError("cloud classification is not enabled")
        candidate = MailCandidate(
            account_id="synthetic",
            uid=0,
            uid_validity=0,
            message_id=None,
            sender="synthetic@example.invalid",
            recipients=(),
            subject="Please review this synthetic test",
            received_at=datetime.now(UTC),
            text_excerpt="Synthetic control-panel provider test. No real mail was used.",
        )
        local = RuleClassifier(config.classification).assess(candidate)
        ResponsesCompatibleClassifier(config.classification).refine(candidate, local)
        self.config_store.record_test(revision.id, "cloud")
        self._audit("cloud.test", "provider", "success", revision.id)

    def activate(self, revision_id: str, expected_revision: str | None) -> ManagedRevision:
        self.config_store.check_basis(revision_id, expected_revision)
        revision = self.config_store.get(revision_id)
        if revision.config.storage.path.resolve() != self.state_database:
            raise ManagedConfigError(
                "managed storage.path must match the serve --state-database path"
            )
        tested = self.config_store.test_results(revision_id)
        required = {
            f"account:{account.id}" for account in revision.config.accounts if account.enabled
        }
        if revision.config.notifications.urls():
            required.add("notifications")
        if revision.config.classification.cloud.enabled:
            required.add("cloud")
        new_accounts = [
            account
            for account in revision.config.accounts
            if account.enabled and self.storage.get_cursor(account.id) is None
        ]
        required.update(f"preview:{account.id}" for account in new_accounts)
        missing = required - tested
        if missing:
            raise ManagedConfigError(
                "enabled integrations require successful tests: " + ", ".join(sorted(missing))
            )
        for account in new_accounts:
            if self.storage.account_preview(revision.id, account.id) is None:
                raise ManagedConfigError(f"24-hour preview for account {account.id!r} expired")
            MailSentryService(revision.config).bootstrap_account(account, days=1)
        activated = self.config_store.activate(revision_id, expected_revision=expected_revision)
        try:
            if self.runtime.status().running:
                self.runtime.reload(activated.config, activated.id)
            else:
                self.runtime.start(activated.config, activated.id)
        except Exception:
            self.config_store.restore(expected_revision)
            self._audit("configuration.activate", revision_id, "runtime-failed", revision_id)
            raise
        self._audit("configuration.activate", revision_id, "success", revision_id)
        return activated

    def events(
        self, *, page: int = 1, level: str | None = None, page_size: int = 20
    ) -> list[dict[str, object]]:
        rows = self.storage.recent_assessments(
            page_size, alert_level=level, offset=(max(page, 1) - 1) * page_size
        )
        events = []
        for row in rows:
            event = dict(row)
            event["reasons"] = json.loads(str(event["reasons_json"]))
            events.append(event)
        return events

    def feedback(self, event_id: int, important: bool) -> None:
        if self.storage.has_feedback(event_id):
            raise ManagedConfigError("feedback has already been recorded for this event")
        sender = self.storage.add_feedback(event_id, important)
        self._audit(
            "assessment.feedback",
            str(event_id),
            "important" if important else "not-important",
            self.runtime.status().revision,
        )
        del sender

    def retry_notifications(self, outbox_id: int | None = None) -> int:
        count = self.storage.retry_notification(outbox_id)
        self._audit(
            "notification.retry",
            str(outbox_id) if outbox_id is not None else "all",
            f"queued:{count}",
            self.runtime.status().revision,
        )
        return count

    def audit(self, limit: int = 50) -> list[dict[str, object]]:
        return [dict(row) for row in self.storage.recent_audit(limit)]

    def _with_environment(self, revision_id: str) -> ManagedRevision:
        return self.config_store.get(revision_id)

    @staticmethod
    def _account(revision: ManagedRevision, account_id: str) -> AccountConfig:
        for account in revision.config.accounts:
            if account.id == account_id:
                return account
        raise ManagedConfigError(f"unknown account {account_id!r}")

    def _audit(
        self,
        action: str,
        target: str,
        result: str,
        revision: str | None,
    ) -> None:
        self.storage.add_audit(action, target, result, revision)


def discord_webhook_to_apprise(value: str) -> str:
    prefixes = (
        "https://discord.com/api/webhooks/",
        "https://discordapp.com/api/webhooks/",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            return "discord://" + value.removeprefix(prefix)
    return value
