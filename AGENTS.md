# MailSentry agent guide

## Project orientation

MailSentry is a self-hosted, read-only email monitor. It observes incoming mail,
produces explainable importance Assessments, persists metadata in SQLite, and sends
immediate notifications through Apprise.

Before planning or implementing project work:

1. Read `CONTEXT.md` for canonical domain terminology.
2. Read `README.md` or `README.zh-CN.md` for current user-visible capabilities.
3. Inspect the relevant source and tests instead of relying on documentation alone.
4. List open GitHub Issues and read the relevant roadmap or task Issue. The current
   roadmap entry point is Issue #1; open Issues are authoritative for TODO status.

## Current structure

- `src/mailsentry/connectors/`: read-only mailbox provider adapters; IMAP is the
  currently implemented provider.
- `src/mailsentry/parser.py`: converts untrusted IMAP headers and BODYSTRUCTURE data
  into Mail Candidates without persisting message bodies or attachment content.
- `src/mailsentry/classifier.py`: deterministic, explainable rule assessment.
- `src/mailsentry/storage.py`: SQLite cursors, metadata, feedback, deduplication,
  digest scheduling state, and notification outbox.
- `src/mailsentry/notifier.py`: Apprise notification delivery.
- `src/mailsentry/service.py`: bootstrap, per-account IDLE workers, digest
  scheduling, and notification orchestration.
- `src/mailsentry/cli.py`: `doctor`, `bootstrap`, `run`, `feedback`, and `purge`.
- `tests/`: configuration, classification, parsing, and storage regression tests.
- `.github/workflows/ci.yml`: Python 3.11/3.12 tests, Ruff, package installation,
  and Docker image build checks for pushes and pull requests.
- `config.example.yaml`, `.env.example`: non-secret configuration contracts.
- `Dockerfile`, `compose.yaml`: single-container deployment packaging.

## Safety boundaries

- Preserve read-only mailbox behavior: select folders read-only and leave message
  state, labels, folders, and content unchanged.
- Keep account passwords, application passwords, webhook URLs, `.env`,
  `config.yaml`, local databases, and real personal data outside version control.
- Persist metadata and Assessment reasons only. Keep message bodies transient and
  treat all email content as untrusted input.
- Keep default notifications limited to account alias, sender, subject, score,
  reasons, and event ID.
- Maintain Python compatibility with the range declared in `pyproject.toml` and
  keep the Docker runtime aligned with it.

## Documentation freshness

Every task that changes tracked project files must end with an Agent documentation
freshness check before it is considered complete:

1. Compare the resulting behavior, commands, configuration, subsystem boundaries,
   and repository layout with this file and its referenced documents.
2. Update `AGENTS.md` in the same change whenever the project overview, structure,
   safety boundaries, workflow, authoritative pointers, or roadmap entry point has
   changed.
3. Update user guidance in the READMEs, canonical terminology in `CONTEXT.md`, and
   task status in GitHub Issues at their respective sources of truth.
4. Keep volatile checklists in GitHub Issues and keep only their discovery pointer
   here, so a new Agent can find current work without maintaining duplicate TODOs.

Completion criterion: a new Agent reading `AGENTS.md` can accurately identify the
project purpose, current high-level structure, safety constraints, authoritative
documentation, and current work discovery path without relying on stale claims.

## Agent skills

### Issue tracker

Issues and specs are tracked with GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default mattpocock/skills triage labels. See
`docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain documentation layout. See
`docs/agents/domain.md`.
