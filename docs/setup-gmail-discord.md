# Gmail and Discord setup

[简体中文](setup-gmail-discord.zh-CN.md)

This guide connects one Gmail Monitored Account through read-only IMAP and sends
MailSentry notifications to a private Discord channel. MailSentry currently uses
an app password, not Gmail OAuth.

## 1. Confirm Gmail compatibility

Use a personal Gmail account, or a Google Workspace account whose administrator
permits both IMAP and app passwords.

- Personal Gmail has kept IMAP access on automatically since January 2025; there
  is no longer an Enable IMAP switch. See [Gmail client setup](https://support.google.com/mail/answer/7126229?hl=en).
- Workspace administrators control IMAP under **Admin console → Apps → Google
  Workspace → Gmail → End User Access → POP and IMAP access**. Policy changes can
  take up to 24 hours. See [Workspace IMAP administration](https://support.google.com/a/answer/105694?hl=en).
- If the account cannot create an app password, this MailSentry release cannot
  connect to it. Normal Google passwords are not a fallback; Gmail OAuth/XOAUTH2
  is a future provider option.

Compatibility is confirmed when the account has 2-Step Verification, exposes the
App Passwords page, and—on Workspace—its administrator confirms IMAP access.

## 2. Create a dedicated Gmail app password

1. Enable [Google 2-Step Verification](https://support.google.com/accounts/answer/185839).
2. Open [Google App Passwords](https://myaccount.google.com/apppasswords), name the
   credential `MailSentry`, and generate it.
3. Save the generated 16-digit value once. Use it only for this deployment; do not
   use the normal Google Account password.

Google may hide App Passwords for organization-managed accounts, Advanced
Protection, or some security-key-only configurations. These restrictions are
documented in [Google's app-password guidance](https://support.google.com/accounts/answer/185833?hl=en).

The app password is a sensitive account credential and is not scoped read-only.
MailSentry itself enforces read-only mailbox selection and never marks, moves, or
deletes source mail.

## 3. Create a Discord webhook

Use a private, dedicated text channel where possible. Your Discord role needs
**Manage Webhooks** permission.

1. Open **Server Settings → Integrations → Webhooks**.
2. Choose **New Webhook**, name it `MailSentry`, and select the destination channel.
3. Choose **Copy Webhook URL**. Discord's official walkthrough is
   [Intro to Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

The copied URL has this shape:

```text
https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

The Web wizard accepts this HTTPS URL directly and converts it to the
[Apprise Discord](https://appriseit.com/services/discord/) form internally.
Headless installations must convert its final two components:

```text
discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

Treat the complete Discord URL as a bearer secret. Do not add Apprise `ping` or
Markdown-format parameters: sender, subject, and Assessment reasons originate from
untrusted email and should not be allowed to trigger Discord mentions.

## 4. Configure MailSentry

In the control panel, choose the Gmail preset, enter the mailbox address and
dedicated app password, then paste the original Discord HTTPS webhook. Save a
Configuration Draft; secrets are not shown again. The model section remains
disabled unless cloud Assessment is explicitly selected.

For a headless installation, put only non-secret connection settings in
`config.yaml`:

```yaml
accounts:
  - id: personal-gmail
    host: imap.gmail.com
    port: 993
    username: owner@gmail.com
    password_env: MAILSENTRY_GMAIL_APP_PASSWORD
    folder: INBOX
    tls: true

notifications:
  urls_env: MAILSENTRY_APPRISE_URLS
  digest_times:
    - "09:00"
    - "18:00"
```

Google specifies `imap.gmail.com:993` with SSL/TLS in its [IMAP protocol reference](https://developers.google.com/workspace/gmail/imap/imap-smtp).

Put both secrets in `.env` and protect the file:

```dotenv
MAILSENTRY_GMAIL_APP_PASSWORD=replace-with-the-generated-app-password
MAILSENTRY_APPRISE_URLS=discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

```bash
chmod 600 .env config.yaml
```

Multiple Apprise destinations may be comma-separated in
`MAILSENTRY_APPRISE_URLS`; test results are sent to every configured destination.

## 5. Validate before continuous monitoring

In the Web draft, run the read-only mailbox test, explicitly confirm the real
Discord test, and review the 24-hour no-notification mailbox preview. Activate only
after all required test buttons show success. The dashboard then establishes the
cursor and starts the account worker.

For a headless installation, validate from the VPS:

```bash
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env doctor --config /config/legacy/config.yaml --no-connect
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env doctor --config /config/legacy/config.yaml
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env doctor --config /config/legacy/config.yaml --notify
```

`--no-connect` does not prove Google accepted the credential. The second command
must report the Gmail account as `read_only=true`; the third must deliver exactly
one test message to the intended Discord channel.

Initialize the cursor and start monitoring only after all three checks pass:

```bash
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env bootstrap --config /config/legacy/config.yaml
docker compose up -d --wait mailsentry
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env status --config /config/legacy/config.yaml --connect
```

Setup is complete when the container is healthy, the Gmail cursor is initialized,
the read-only connection succeeds, and the Discord test arrives.

## Troubleshooting and rotation

- **App Passwords is missing:** check 2-Step Verification, Advanced Protection,
  security-key-only setup, and Workspace policy. If policy requires OAuth, wait for
  MailSentry's Gmail OAuth provider rather than using the account password.
- **Authentication failed:** verify the full Gmail address, remove accidental
  whitespace from the generated app password, and generate a replacement if it was
  revoked. Changing the main Google password revokes existing app passwords.
- **Discord URL is invalid:** use `discord://ID/TOKEN/`, not a channel URL, bot
  token, or redacted webhook URL.
- **Test sends but no message appears:** confirm the webhook still targets the
  intended channel and that every configured Apprise destination is reachable.

For planned rotation, create and validate a replacement first, then revoke the old
app password or delete the old Discord webhook. For suspected exposure, revoke or
delete the exposed credential immediately and deploy a new one. See the
[operations runbook](operations.md) for the full rotation and incident sequence.
