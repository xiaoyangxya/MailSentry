from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from mailsentry.classifier import RuleClassifier
from mailsentry.cloud_classifier import ResponsesCompatibleClassifier
from mailsentry.config import ConfigurationError, load_config
from mailsentry.connectors import ImapConnector
from mailsentry.models import AlertLevel, AttachmentMetadata, MailCandidate
from mailsentry.notifier import AppriseNotifier
from mailsentry.service import MailSentryService
from mailsentry.soak import SoakConfig, run_soak
from mailsentry.storage import Storage

app = typer.Typer(no_args_is_help=True, help="Monitor mailboxes for important email.")
DEFAULT_CONFIG = Path(os.getenv("MAILSENTRY_CONFIG", "config.yaml"))
DEFAULT_ENV_FILE = Path(".env")
ConfigPath = Annotated[Path, typer.Option(exists=True, dir_okay=False)]


@app.callback()
def configure_environment(
    env_file: Annotated[
        Path,
        typer.Option(
            dir_okay=False,
            help="Load secrets from this file without overriding existing environment variables.",
        ),
    ] = DEFAULT_ENV_FILE,
) -> None:
    """Load local secrets before running a command."""
    load_dotenv(dotenv_path=env_file, override=False)


def _configuration_error(exc: Exception) -> None:
    typer.echo(f"Configuration error: {exc}", err=True)
    raise typer.Exit(2) from None


def _load(path: Path):
    try:
        return load_config(path)
    except ConfigurationError as exc:
        _configuration_error(exc)


def _service(path: Path) -> MailSentryService:
    try:
        return MailSentryService(_load(path))
    except (ConfigurationError, ValueError) as exc:
        _configuration_error(exc)


@app.command()
def doctor(
    config: ConfigPath = DEFAULT_CONFIG,
    connect: Annotated[bool, typer.Option("--connect/--no-connect")] = True,
    notify: Annotated[
        bool,
        typer.Option(help="Send a test notification through every configured channel."),
    ] = False,
) -> None:
    """Validate configuration, secrets, storage, and optionally IMAP connections."""
    settings = _load(config)
    if settings.classification.cloud.enabled:
        try:
            settings.classification.cloud.api_key()
        except ConfigurationError as exc:
            _configuration_error(exc)
        typer.echo(
            "OK cloud_classifier=true "
            f"model={settings.classification.cloud.model} "
            f"base_url={settings.classification.cloud.base_url}"
        )
    storage = Storage(settings.storage.path)
    storage.initialize()
    for account in settings.accounts:
        try:
            account.password()
        except ConfigurationError as exc:
            _configuration_error(exc)
        if connect:
            try:
                ImapConnector(account).check()
            except Exception as exc:
                typer.echo(f"Connection check failed account={account.id}: {exc}", err=True)
                raise typer.Exit(1) from None
        typer.echo(f"OK account={account.id} host={account.host} read_only=true")
    urls = settings.notifications.urls()
    if not urls:
        if notify:
            _configuration_error(
                ConfigurationError(
                    f"notification test requires URLs in {settings.notifications.urls_env}"
                )
            )
        typer.echo(
            f"WARNING no notification URLs found in {settings.notifications.urls_env}", err=True
        )
    else:
        try:
            notifier = AppriseNotifier(urls)
        except ValueError as exc:
            _configuration_error(exc)
        if notify and not notifier.send(
            "MailSentry doctor",
            "Notification channel test succeeded.",
        ):
            typer.echo("Notification test failed.", err=True)
            raise typer.Exit(1)
        typer.echo(f"OK notifications={len(urls)} tested={str(notify).lower()}")
    typer.echo(f"OK storage={settings.storage.path}")


@app.command()
def bootstrap(
    config: ConfigPath = DEFAULT_CONFIG,
    days: Annotated[int, typer.Option(min=1, max=7)] = 1,
) -> None:
    """Dry-run recent mail and establish a cursor without sending notifications."""
    service = _service(config)
    count = service.bootstrap(days)
    typer.echo(f"Bootstrap complete: assessed {count} recent message(s); notifications disabled.")


@app.command("run")
def run_service(
    config: ConfigPath = DEFAULT_CONFIG,
    once: Annotated[bool, typer.Option(help="Process one cycle and exit.")] = False,
) -> None:
    """Monitor configured accounts and deliver immediate notifications."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = _service(config)
    if once:
        observed, delivered, failed = service.process_once()
        typer.echo(f"observed={observed} delivered={delivered} failed={failed}")
        return
    service.run_forever()


@app.command()
def soak(
    database: Annotated[
        Path,
        typer.Option(help="New SQLite path reserved for synthetic soak data."),
    ],
    metrics: Annotated[
        Path,
        typer.Option(help="New JSONL path for timestamped soak evidence."),
    ],
    duration_hours: Annotated[
        float,
        typer.Option(min=0.001, help="Logical and, unless accelerated, wall-clock duration."),
    ] = 24.0,
    event_interval_seconds: Annotated[
        int,
        typer.Option(min=1, help="Synthetic event interval for each account."),
    ] = 300,
    seed: int = 20260915,
    accounts: Annotated[int, typer.Option(min=1)] = 10,
    rss_limit_mib: Annotated[int, typer.Option(min=1)] = 512,
    accelerated: Annotated[
        bool,
        typer.Option(help="Run without wall-clock pacing; never valid release evidence."),
    ] = False,
) -> None:
    """Run an offline deterministic multi-account soak and reconcile its evidence."""
    try:
        report = run_soak(
            SoakConfig(
                database_path=database,
                metrics_path=metrics,
                duration_seconds=max(1, round(duration_hours * 60 * 60)),
                event_interval_seconds=event_interval_seconds,
                seed=seed,
                account_count=accounts,
                accelerated=accelerated,
                rss_limit_mib=rss_limit_mib,
            )
        )
    except ValueError as exc:
        _configuration_error(exc)
    typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def explain(
    sender: Annotated[str, typer.Option(help="Envelope sender address to assess.")],
    config: ConfigPath = DEFAULT_CONFIG,
    subject: Annotated[str, typer.Option(help="Subject text to assess.")] = "",
    attachment: Annotated[
        list[str] | None,
        typer.Option(help="Attachment filename; repeat for multiple files."),
    ] = None,
    provider_important: Annotated[
        bool,
        typer.Option(help="Include a provider importance flag."),
    ] = False,
    priority_header: Annotated[
        bool,
        typer.Option(help="Include a recognized high-priority header."),
    ] = False,
    bulk: Annotated[
        bool,
        typer.Option(help="Include a List-Unsubscribe bulk-mail header."),
    ] = False,
    text: Annotated[
        str,
        typer.Option(help="Optional transient message text for an explicit cloud test."),
    ] = "",
    cloud: Annotated[
        bool,
        typer.Option(help="Test the configured Responses-compatible cloud provider."),
    ] = False,
) -> None:
    """Explain a hypothetical Assessment without saving or notifying it."""
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    candidate = MailCandidate(
        account_id=settings.accounts[0].id,
        uid=0,
        uid_validity=0,
        message_id=None,
        sender=sender,
        recipients=(),
        subject=subject,
        received_at=datetime.now(UTC),
        flags=frozenset({"\\Important"}) if provider_important else frozenset(),
        has_priority_header=priority_header,
        has_list_unsubscribe=bulk,
        attachments=tuple(
            AttachmentMetadata(filename, "application/octet-stream")
            for filename in attachment or ()
        ),
        text_excerpt=text,
    )
    feedback = storage.sender_feedback(sender)
    assessment = RuleClassifier(settings.classification).assess(candidate, feedback)
    if cloud:
        if not settings.classification.cloud.enabled:
            _configuration_error(
                ConfigurationError("classification.cloud must be enabled for --cloud")
            )
        try:
            assessment = ResponsesCompatibleClassifier(
                settings.classification
            ).refine(candidate, assessment)
        except ConfigurationError as exc:
            _configuration_error(exc)
        except Exception as exc:
            typer.echo(
                f"Cloud assessment failed safely: {type(exc).__name__}", err=True
            )
            raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "score": assessment.score,
                "level": assessment.level.value,
                "reasons": assessment.reasons,
            },
            ensure_ascii=False,
        )
    )


@app.command()
def feedback(
    event_id: int,
    verdict: str,
    config: ConfigPath = DEFAULT_CONFIG,
) -> None:
    """Correct an Assessment with `important` or `not-important`."""
    if verdict not in {"important", "not-important"}:
        typer.echo("verdict must be 'important' or 'not-important'", err=True)
        raise typer.Exit(2)
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    try:
        sender = storage.add_feedback(event_id, verdict == "important")
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Feedback recorded for sender={sender}")


@app.command()
def purge(
    config: ConfigPath = DEFAULT_CONFIG,
) -> None:
    """Delete retained metadata older than the configured period."""
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    count = storage.purge(settings.storage.retention_days)
    typer.echo(f"Purged {count} expired event(s).")


@app.command()
def status(
    config: ConfigPath = DEFAULT_CONFIG,
    connect: Annotated[
        bool,
        typer.Option(
            "--connect/--no-connect",
            help="Also verify each read-only IMAP connection.",
        ),
    ] = False,
) -> None:
    """Show saved Monitored Account and notification delivery health."""
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    connection_failed = False
    for account in settings.accounts:
        cursor = storage.account_status(account.id)
        fields = {
            "account": account.id,
            "initialized": cursor is not None,
            "last_scan": str(cursor["updated_at"]) if cursor else None,
            "last_uid": int(cursor["last_uid"]) if cursor else None,
            "connection": "not-checked",
        }
        if connect:
            try:
                ImapConnector(account).check()
            except Exception as exc:
                fields["connection"] = "failed"
                fields["error"] = str(exc)
                connection_failed = True
            else:
                fields["connection"] = "ok"
        typer.echo(json.dumps(fields, ensure_ascii=False))
    pending, permanently_failed = storage.notification_counts()
    typer.echo(
        json.dumps(
            {
                "notifications_pending": pending,
                "notifications_permanently_failed": permanently_failed,
            }
        )
    )
    if connection_failed:
        raise typer.Exit(1)


@app.command()
def events(
    config: ConfigPath = DEFAULT_CONFIG,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    level: Annotated[
        str | None,
        typer.Option(help="Filter by immediate, digest, or audit."),
    ] = None,
) -> None:
    """Show recent explainable Assessments as JSON lines."""
    if level is not None and level not in {item.value for item in AlertLevel}:
        typer.echo("level must be 'immediate', 'digest', or 'audit'", err=True)
        raise typer.Exit(2)
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    for event in storage.recent_assessments(limit, alert_level=level):
        typer.echo(
            json.dumps(
                {
                    "event_id": int(event["id"]),
                    "account": str(event["account_id"]),
                    "received_at": str(event["received_at"]),
                    "sender": str(event["sender"]),
                    "subject": str(event["subject"]),
                    "score": int(event["score"]),
                    "level": str(event["alert_level"]),
                    "reasons": json.loads(str(event["reasons_json"])),
                    "assessed_at": str(event["created_at"]),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    app()
