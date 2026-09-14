# MailSentry / 信哨

[简体中文](README.zh-CN.md)

MailSentry is a lightweight, self-hosted service that watches multiple mailboxes,
assesses incoming messages, and notifies you when an email looks important. It uses
read-only IMAP access and never marks, moves, or deletes source messages.

> Status: foundational v0.1. The rule engine, SQLite persistence, per-account IMAP
> IDLE monitoring with five-minute catch-up scans, scheduled digest and immediate
> Apprise notifications, CLI, and container packaging are available. Optional
> cloud-model assessment remains on the roadmap.

## Quick start

Requirements: Python 3.11 or Docker Desktop.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

Edit `config.yaml` and `.env`, then validate the setup. Local Python commands
automatically load `.env` without overriding variables already present in the
environment:

```powershell
.\.venv\Scripts\mail-sentinel doctor --no-connect
.\.venv\Scripts\mail-sentinel doctor
.\.venv\Scripts\mail-sentinel doctor --notify
.\.venv\Scripts\mail-sentinel bootstrap
.\.venv\Scripts\mail-sentinel run
```

The first bootstrap assesses up to 24 hours of recent mail without sending any
notification. Later runs only process messages after the saved mailbox cursor.

## Configuration and secrets

Non-secret settings belong in `config.yaml`. Account passwords and Apprise URLs are
read from environment variables named by that configuration. Use
`--env-file path\to\secrets.env` before the command name to load another file.
Never commit `.env`.

Apprise accepts notification URLs for Telegram, ntfy, Feishu, generic webhooks, and
many other services. See the [Apprise service documentation](https://appriseit.com/services/).

Assessments at the digest alert level are grouped into one notification at each
`notifications.digest_times` slot in the configured `timezone`. The defaults are
09:00 and 18:00 Asia/Shanghai. Persisted digest slots prevent duplicate delivery
after a restart.

## Docker

Start Docker Desktop, create `config.yaml` and `.env`, then run:

```powershell
docker compose run --rm mailsentry bootstrap
docker compose up -d
```

Persistent metadata is stored in the `mailsentry-data` volume. Message bodies and
attachment content are not persisted.

## Development

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
```

MailSentry is licensed under AGPL-3.0-or-later.
