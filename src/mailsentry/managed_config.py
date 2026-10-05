from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import dotenv_values

from mailsentry.config import ConfigurationError, ServiceConfig, load_config


class ManagedConfigError(ValueError):
    """Raised when a managed configuration revision cannot be used safely."""


class RevisionConflict(ManagedConfigError):
    """Raised when a caller tries to replace a revision it did not read."""


@dataclass(frozen=True, slots=True)
class ManagedRevision:
    id: str
    config_path: Path
    secrets_path: Path
    config: ServiceConfig


class ConfigStore:
    """Own revision creation, secret merging, validation, and atomic activation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.revisions = root / "revisions"
        self.active_manifest = root / "active.json"
        self._lock = threading.RLock()
        self._managed_keys: set[str] = set()

    def initialize(self) -> None:
        self.revisions.mkdir(parents=True, exist_ok=True)
        for path in (self.root, self.revisions):
            with suppress(OSError):
                path.chmod(0o700)

    def active(self) -> ManagedRevision | None:
        self.initialize()
        try:
            document = json.loads(self.active_manifest.read_text(encoding="utf-8"))
            revision_id = str(document["revision"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagedConfigError(f"cannot read active configuration: {exc}") from exc
        return self.get(revision_id)

    def get(self, revision_id: str) -> ManagedRevision:
        if not revision_id or any(part in revision_id for part in ("/", "\\", "..")):
            raise ManagedConfigError("invalid configuration revision")
        directory = self.revisions / revision_id
        config_path = directory / "config.yaml"
        secrets_path = directory / "secrets.env"
        try:
            config = load_config(
                config_path,
                secrets=self._read_secrets(secrets_path),
            )
        except ConfigurationError as exc:
            raise ManagedConfigError(str(exc)) from exc
        return ManagedRevision(revision_id, config_path, secrets_path, config)

    def stage(
        self,
        config_text: str,
        *,
        secret_updates: dict[str, str | None] | None = None,
        expected_revision: str | None = None,
        source_revision: str | None = None,
    ) -> ManagedRevision:
        if len(config_text.encode("utf-8")) > 256 * 1024:
            raise ManagedConfigError("configuration exceeds 256 KiB")
        with self._lock:
            active = self.active()
            current_id = active.id if active else None
            if expected_revision != current_id:
                raise RevisionConflict(
                    f"configuration changed from {expected_revision!r} to {current_id!r}"
                )
            source = self.get(source_revision) if source_revision else active
            if source and source.id != current_id:
                metadata = json.loads(
                    (source.config_path.parent / "revision.json").read_text(encoding="utf-8")
                )
                if metadata.get("based_on") != current_id:
                    raise RevisionConflict("draft is based on an older active configuration")
            revision_id = f"{datetime.now(UTC):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
            directory = self.revisions / revision_id
            directory.mkdir(parents=False)
            directory.chmod(0o700)
            config_path = directory / "config.yaml"
            secrets_path = directory / "secrets.env"
            try:
                config_path.write_text(config_text, encoding="utf-8")
                existing = self._read_secrets(source.secrets_path) if source else {}
                for key, value in (secret_updates or {}).items():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                        raise ManagedConfigError(f"invalid secret name {key!r}")
                    if value is None:
                        existing.pop(key, None)
                    elif value:
                        if len(value.encode("utf-8")) > 16 * 1024:
                            raise ManagedConfigError(f"secret {key!r} exceeds 16 KiB")
                        existing[key] = value
                self._write_secrets(secrets_path, existing)
                revision = self.get(revision_id)
                self._write_metadata(directory, "draft", based_on=current_id)
                return revision
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def activate(
        self,
        revision_id: str,
        *,
        expected_revision: str | None,
    ) -> ManagedRevision:
        with self._lock:
            self.check_basis(revision_id, expected_revision)
            active = self.active()
            current_id = active.id if active else None
            if expected_revision != current_id:
                raise RevisionConflict(
                    f"configuration changed from {expected_revision!r} to {current_id!r}"
                )
            revision = self.get(revision_id)
            secrets = self._read_secrets(revision.secrets_path)
            self._validate_required_secrets(revision.config, secrets)
            manifest = self.root / f".active-{uuid.uuid4().hex}.tmp"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "revision": revision.id,
                        "previous_revision": current_id,
                        "activated_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(manifest, self.active_manifest)
            self._write_metadata(revision.config_path.parent, "active")
            self._prune({revision.id, current_id} - {None})
            return revision

    def check_basis(self, revision_id: str, expected_revision: str | None) -> None:
        active = self.active()
        current = active.id if active else None
        if current != expected_revision:
            raise RevisionConflict("active configuration changed; reopen settings")
        revision = self.get(revision_id)
        metadata = json.loads(
            (revision.config_path.parent / "revision.json").read_text(encoding="utf-8")
        )
        if revision_id != current and metadata.get("based_on") != current:
            raise RevisionConflict("draft is based on an older active configuration")

    def restore(self, revision_id: str | None) -> None:
        """Restore the active pointer after a runtime activation failure."""
        with self._lock:
            if revision_id is None:
                self.active_manifest.unlink(missing_ok=True)
                return
            self.get(revision_id)
            temporary = self.root / f".restore-{uuid.uuid4().hex}.tmp"
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "revision": revision_id,
                        "restored_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.active_manifest)

    def record_test(self, revision_id: str, name: str) -> None:
        revision = self.get(revision_id)
        path = revision.config_path.parent / "tests.json"
        try:
            results = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            results = {}
        results[name] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    def test_results(self, revision_id: str) -> frozenset[str]:
        revision = self.get(revision_id)
        try:
            results = json.loads(
                (revision.config_path.parent / "tests.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ValueError):
            return frozenset()
        return frozenset(str(key) for key in results)

    def import_legacy(
        self,
        config_path: Path,
        env_file: Path | None = None,
    ) -> ManagedRevision:
        if self.active() is not None:
            raise ManagedConfigError("managed configuration already exists")
        try:
            legacy_config = load_config(config_path)
        except ConfigurationError as exc:
            raise ManagedConfigError(str(exc)) from exc
        referenced = {
            *(account.password_env for account in legacy_config.accounts),
            legacy_config.notifications.urls_env,
            legacy_config.classification.cloud.api_key_env,
        }
        secrets = {
            str(key): str(value)
            for key, value in (
                dotenv_values(env_file) if env_file and env_file.exists() else {}
            ).items()
            if value is not None and str(key) in referenced
        }
        return self.stage(
            config_path.read_text(encoding="utf-8"),
            secret_updates=secrets,
            expected_revision=None,
        )

    def export_redacted(self, revision_id: str | None = None) -> str:
        revision = self.get(revision_id) if revision_id else self.active()
        if revision is None:
            raise ManagedConfigError("no active configuration")
        document = yaml.safe_load(revision.config_path.read_text(encoding="utf-8")) or {}
        secrets = self._read_secrets(revision.secrets_path)
        document["managed_export"] = {
            "schema_version": 1,
            "revision": revision.id,
            "secret_status": {key: "must-be-provided" for key in sorted(secrets)},
        }
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)

    def apply_environment(self, revision: ManagedRevision) -> None:
        # Managed revisions carry resolved secrets in private runtime-only fields.
        # This method remains as a compatibility no-op for older callers.
        del revision

    def _activate_environment(self, values: dict[str, str]) -> None:
        for key in self._managed_keys - values.keys():
            os.environ.pop(key, None)
        for key, value in values.items():
            os.environ[key] = value
        self._managed_keys = set(values)

    @staticmethod
    def _validate_required_secrets(config: ServiceConfig, secrets: dict[str, str]) -> None:
        missing = []
        for account in config.accounts:
            if not account.enabled:
                continue
            try:
                account.password()
            except ConfigurationError:
                missing.append(account.password_env)
        if config.classification.cloud.enabled:
            try:
                config.classification.cloud.api_key()
            except ConfigurationError:
                missing.append(config.classification.cloud.api_key_env)
        if missing:
            raise ManagedConfigError(
                "missing required secret(s): " + ", ".join(sorted(set(missing)))
            )

    @staticmethod
    def _read_secrets(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        return {
            str(key): str(value) for key, value in dotenv_values(path).items() if value is not None
        }

    @staticmethod
    def _write_secrets(path: Path, values: dict[str, str]) -> None:
        contents = "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n"
            for key, value in sorted(values.items())
        )
        path.write_text(contents, encoding="utf-8")
        with suppress(OSError):
            path.chmod(0o600)

    @staticmethod
    def _write_metadata(directory: Path, status: str, *, based_on: str | None = None) -> None:
        (directory / "revision.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "based_on": based_on,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _prune(self, keep: set[str]) -> None:
        candidates = (
            path for path in self.revisions.iterdir() if path.is_dir() and path.name not in keep
        )
        for path in candidates:
            shutil.rmtree(path)
