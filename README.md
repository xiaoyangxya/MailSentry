# MailSentry / 信哨

[简体中文](README.zh-CN.md)

MailSentry is a lightweight, self-hosted service that watches multiple mailboxes,
assesses incoming messages, and notifies you when an email looks important. It uses
read-only IMAP access and never marks, moves, or deletes source messages.

> Status: v0.2 release-candidate development. The existing monitor, CLI, optional
> cloud Assessment, and operations tooling now include a single-Owner authenticated
> Web control panel with revisioned configuration and graceful runtime reload.

## Quick start

Requirements: Python 3.11 or Docker Desktop.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

For the default Web setup, leave these files as import templates, build the image,
and start the control panel:

```powershell
docker compose up -d --build --wait mailsentry
docker compose logs --tail 20 mailsentry
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and use the one-time setup path
printed in the logs. Create the administrator, then either confirm the redacted
legacy import or add a mailbox on the Mailboxes page. An enabled mailbox must pass
its read-only connection test and 24-hour no-notification preview before activation.

For a headless installation, edit `config.yaml` and `.env` and use the existing
CLI workflow. Local Python commands load `.env` without overriding variables
already present in the environment:

```powershell
.\.venv\Scripts\mail-sentinel doctor --no-connect
.\.venv\Scripts\mail-sentinel doctor
.\.venv\Scripts\mail-sentinel doctor --notify
.\.venv\Scripts\mail-sentinel bootstrap
.\.venv\Scripts\mail-sentinel run
.\.venv\Scripts\mail-sentinel serve
.\.venv\Scripts\mail-sentinel status --connect
.\.venv\Scripts\mail-sentinel events --limit 20
.\.venv\Scripts\mail-sentinel explain --sender billing@example.com --subject "Invoice due"
```

The first bootstrap assesses up to 24 hours of recent mail without sending any
notification. Later runs only process messages after the saved mailbox cursor.

Use `status` to inspect saved scan cursors and notification delivery counts. Add
`--connect` to verify every configured mailbox through a read-only IMAP connection;
the command exits unsuccessfully if any connection fails. Use `events` to inspect
recent explainable Assessments as JSON lines, optionally filtered with
`--level immediate`, `--level digest`, or `--level audit`.

## Configuration and secrets

Non-secret settings belong in `config.yaml`. Account passwords and Apprise URLs are
read from environment variables named by that configuration. Use
`--env-file path\to\secrets.env` before the command name to load another file.
Never commit `.env`.

After a confirmed Web import, Configuration Revisions under the managed
configuration directory become the source of truth. Each immutable revision keeps
non-secret YAML separate from a restricted secret file. Empty secret fields retain
the current value; replacement and removal are explicit. Exports contain only
non-secret settings and “must be provided” markers.

Use the dedicated Mailboxes, Detection rules, AI assessment, Notifications, and
Test and apply pages. Add multiple Gmail or custom TLS IMAP accounts before initial
activation. Each mailbox can be edited, disabled, or removed without deleting source
mail or assessment history. Use a new alias when changing the address, host, or
folder of an account that already has a scan cursor.

Rules accept one keyword, attachment extension, or sender per line. Sender lists
accept exact addresses and `@domains`: priority senders receive 100 points and
immediate notification, overriding silent senders, which are recorded only.
Keywords match subjects only and add 35 points once. Try saved rules with the
local score calculator. The AI page exposes Base URL, model ID, API key and timeout;
notifications accept Discord webhook or Apprise URLs and digest scheduling.

Navigation retains the selected draft and its secrets. Saving does not change
active monitoring. Finish editing, then test mailbox connections, new-account
previews, and enabled integrations before applying. Each save creates a new draft
with fresh test requirements. Advanced YAML remains available separately; ordinary
setup needs neither YAML editing nor manual secret-variable names.

For an end-to-end personal Gmail and Discord webhook walkthrough, follow the
[Gmail and Discord setup guide](docs/setup-gmail-discord.md).

Apprise accepts notification URLs for Telegram, ntfy, Feishu, generic webhooks, and
many other services. See the [Apprise service documentation](https://appriseit.com/services/).

Assessments at the digest alert level are grouped into one notification at each
`notifications.digest_times` slot in the configured `timezone`. The defaults are
09:00 and 18:00 Asia/Shanghai. Persisted digest slots prevent duplicate delivery
after a restart.

The explainable rule assessment recognizes Gmail importance flags and common high-
priority headers, in addition to configured senders, subject keywords, attachment
metadata, bulk-mail headers, and recorded Feedback. Equivalent importance signals
are counted only once.

Use `classification.sender_reputation` to assign an adjustment from -30 to 30 to
an exact sender or an `@domain`; an exact sender entry overrides its domain entry.
Negative Feedback cannot suppress explicit security or payment subject signals.
The `explain` command evaluates these rules and existing sender Feedback without
saving an Assessment or sending a notification. Add `--provider-important`,
`--priority-header`, `--bulk`, or repeated `--attachment` options to model signals.

### Optional cloud provider

MailSentry can refine only ambiguous, digest-level rule Assessments through any
provider that implements the OpenAI Responses endpoint and JSON Schema structured
output. DeepSeek is the example in `config.example.yaml`; replace `base_url`,
`model`, and `api_key_env` for another compatible provider:

```yaml
classification:
  cloud:
    enabled: true
    base_url: https://api.deepseek.com
    model: deepseek-flash
    api_key_env: MAILSENTRY_CLOUD_API_KEY
    timeout_seconds: 15
```

Put the actual key in `.env` as `MAILSENTRY_CLOUD_API_KEY=...`, never directly in
YAML. Test the configured API with synthetic input without saving or notifying:

```powershell
.\.venv\Scripts\mail-sentinel explain --sender test@example.com --subject "Please review" --text "Synthetic test message" --cloud
```

When enabled, MailSentry fetches message text read-only and sends only the sender,
subject, attachment metadata, and at most 4,000 cleaned text characters. Account
aliases, recipients, Message-IDs, attachment content, and credentials are excluded.
Provider reasoning is disabled so the bounded output budget is reserved for the
short structured result. Email fields are treated as untrusted instructions, model
output must match a strict
schema, and timeout or invalid output falls back to the local rule Assessment.
`bootstrap` also assesses ambiguous recent messages through the configured provider;
leave cloud classification disabled if historical mail must not leave the VPS.

## Docker

Start Docker Desktop, create the two legacy import templates, then run:

```powershell
docker compose up -d --build --wait mailsentry
docker compose logs --tail 20 mailsentry
```

The application is exposed only at `127.0.0.1:8080` by default. For managed HTTPS,
set `MAILSENTRY_DOMAIN` and start the optional Caddy profile:

```powershell
docker compose --profile https up -d --build --wait
```

Use an existing reverse proxy or private tunnel instead if preferred. The
application rejects remote plain HTTP unless the explicit isolated-network option
is used. `mail-sentinel reset-admin` is the server-side recovery path for a lost
password or TOTP device.

For first server installation, consistent backup and restore, credential rotation,
upgrades, rollback, log collection, and incident response, follow the
[operations runbook](docs/operations.md).

Before a release, `mail-sentinel soak` can run a deterministic, offline ten-account
workload through the real workers, classifier, SQLite state, digest, outbox, retry,
deduplication, and retention paths. It uses only synthetic data and never opens an
IMAP or notification-provider connection. Use `--accelerated` for a quick regression
test; accelerated output is explicitly ineligible as 24-hour release evidence. The
operations runbook contains the isolated VPS command and reconciliation steps.

Persistent metadata is stored in the `mailsentry-data` volume and managed
Configuration Revisions in `mailsentry-config`. Message bodies,
transient cloud excerpts, and attachment content are not persisted. Privacy
regression tests also enforce that transient content, mailbox passwords, and
notification tokens stay out of SQLite, logs, and default notification text.

## Development

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
```

MailSentry is licensed under AGPL-3.0-or-later.
