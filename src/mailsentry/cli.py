from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from mailsentry.config import ConfigurationError, load_config
from mailsentry.connectors import ImapConnector
from mailsentry.service import MailSentryService
from mailsentry.storage import Storage

app = typer.Typer(no_args_is_help=True, help="Monitor mailboxes for important email.")
DEFAULT_CONFIG = Path(os.getenv("MAILSENTRY_CONFIG", "config.yaml"))
ConfigPath = Annotated[Path, typer.Option(exists=True, dir_okay=False)]


def _load(path: Path):
    try:
        return load_config(path)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def doctor(
    config: ConfigPath = DEFAULT_CONFIG,
    connect: Annotated[bool, typer.Option("--connect/--no-connect")] = True,
) -> None:
    """Validate configuration, secrets, storage, and optionally IMAP connections."""
    settings = _load(config)
    storage = Storage(settings.storage.path)
    storage.initialize()
    for account in settings.accounts:
        account.password()
        if connect:
            ImapConnector(account).check()
        typer.echo(f"OK account={account.id} host={account.host} read_only=true")
    if not settings.notifications.urls():
        typer.echo(
            f"WARNING no notification URLs found in {settings.notifications.urls_env}", err=True
        )
    typer.echo(f"OK storage={settings.storage.path}")


@app.command()
def bootstrap(
    config: ConfigPath = DEFAULT_CONFIG,
    days: Annotated[int, typer.Option(min=1, max=7)] = 1,
) -> None:
    """Dry-run recent mail and establish a cursor without sending notifications."""
    service = MailSentryService(_load(config))
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
    service = MailSentryService(_load(config))
    if once:
        observed, delivered, failed = service.process_once()
        typer.echo(f"observed={observed} delivered={delivered} failed={failed}")
        return
    service.run_forever()


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


if __name__ == "__main__":
    app()
