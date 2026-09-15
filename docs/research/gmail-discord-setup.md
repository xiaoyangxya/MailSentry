# Gmail IMAP and Discord notification setup research

Reviewed: 2026-09-15

This note records the current first-party requirements for connecting MailSentry
to Gmail over read-only IMAP and delivering Apprise notifications to a Discord
incoming webhook. It is research input for the user guide, not a substitute for
the configuration contract in `config.example.yaml`.

## Recommended support boundary

The immediately supportable path is:

1. A personal Gmail account, or a Google Workspace account whose administrator
   permits IMAP and whose security policy permits an app password.
2. Gmail IMAP at `imap.gmail.com`, port `993`, with TLS enabled.
3. The full Gmail address as the IMAP username and a dedicated 16-digit Google
   app password in a named environment variable.
4. A Discord incoming webhook represented as the Apprise URL
   `discord://WEBHOOK_ID/WEBHOOK_TOKEN/`, also stored in an environment variable.

MailSentry currently authenticates with IMAP `LOGIN` and selects the configured
folder with `readonly=True`; it does not implement Gmail XOAUTH2. See the current
[`ImapConnector`](../../src/mailsentry/connectors/imap.py). Consequently, a Gmail
or Workspace account that prohibits app passwords is not currently compatible,
even though OAuth is Google's preferred authentication method.

## Gmail requirements

### Server settings

Google's Gmail protocol reference specifies that incoming IMAP connections use
`imap.gmail.com:993` and require SSL. In MailSentry terminology, use TLS from the
start rather than a plaintext connection upgraded with STARTTLS. Gmail limits a
continuous IMAP session to about 24 hours, so reconnects are normal operating
behavior rather than proof of a credential failure. [Google Gmail IMAP protocol
reference](https://developers.google.com/workspace/gmail/imap/imap-smtp)

```yaml
accounts:
  - id: personal-gmail
    host: imap.gmail.com
    port: 993
    username: owner@gmail.com
    password_env: MAILSENTRY_GMAIL_APP_PASSWORD
    folder: INBOX
    tls: true
```

MailSentry's `readonly=True` folder selection protects the mailbox from changes
made by this program. The app password itself is **not** a read-only-scoped
credential: Google describes it as permission for an app or device to access the
Google Account. Treat compromise of it as account credential compromise, not only
as compromise of a MailSentry setting. [Google app-password
documentation](https://support.google.com/accounts/answer/185833?hl=en)

### Personal Gmail

For a personal Google Account, Gmail removed the user-facing Enable/Disable IMAP
choice starting in January 2025 and states that IMAP access is always on. A user
should therefore not be told to search for a now-removed "Enable IMAP" switch.
[Gmail Help: add Gmail to another email
client](https://support.google.com/mail/answer/7126229?hl=en)

The current password-based setup is:

1. Enable 2-Step Verification on the Google Account.
2. Open the Google Account's App Passwords page and create a password dedicated
   to MailSentry.
3. Put the generated 16-digit app password in `.env`, under the exact variable
   named by `accounts[].password_env`. Do not use the normal Google Account
   password.
4. Put only the environment-variable name in `config.yaml`.

Google requires 2-Step Verification before an app password can be created and
recommends Sign in with Google/OAuth instead whenever the client supports it.
Google also says the App Passwords option might be unavailable when 2-Step
Verification is configured only with security keys, for some work/school or other
organization accounts, or when Advanced Protection is enabled. [Google
app-password documentation](https://support.google.com/accounts/answer/185833?hl=en)
Advanced Protection explicitly blocks app-password clients and revokes existing
app passwords on enrollment. [Google Advanced Protection
FAQ](https://support.google.com/accounts/answer/7539956?hl=en)

### Google Workspace: administrator and edition/policy differences

Workspace is not equivalent to personal Gmail for IMAP enablement. A Workspace
administrator must enable IMAP for the applicable organization, organizational
unit, or access group at **Admin console → Apps → Google Workspace → Gmail → End
User Access → POP and IMAP access**. Google says a change can take up to 24 hours,
although it usually applies sooner. An administrator may also restrict access to
specified OAuth clients. [Google Workspace Admin Help: turn POP and IMAP on or
off](https://support.google.com/a/answer/105694?hl=en)

Google's Workspace migration guidance says that, as of 1 May 2025, a normal
username and account password no longer works for IMAP and other "less secure
app" access; OAuth is the preferred replacement. The same first-party update also
says an application without OAuth can use an app password. [Google Workspace
Updates](https://workspaceupdates.googleblog.com/2023/09/winding-down-google-sync-and-less-secure-apps-support.html)
The Workspace client-setup page likewise recommends OAuth while retaining a step
for app passwords for clients that cannot accept verification codes. [Google
Workspace third-party client
setup](https://support.google.com/a/answer/9003945?hl=en)

These pages do not guarantee that every Workspace tenant exposes app passwords.
The general Google Account documentation explicitly lists organization accounts
as a reason the option might be missing. Therefore the support guidance must be
conditional:

- Ask the Workspace administrator to confirm both IMAP access and app-password
  policy for the target user or organizational unit.
- Never try the user's normal Workspace password as a fallback.
- If an app password cannot be created, MailSentry cannot connect to that mailbox
  today. The product would need an XOAUTH2 implementation. Gmail's official IMAP
  OAuth mechanism uses SASL XOAUTH2; its documentation also describes the
  `https://mail.google.com/` OAuth scope and the separate domain-wide delegation
  case. [Gmail XOAUTH2 protocol](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol)

This is a tenant-policy difference, not a reliable Google Workspace edition
matrix: the official pages reviewed here do not publish an edition-by-edition
guarantee for app-password availability.

### Gmail validation, rotation, and revocation

Use a dedicated environment variable:

```dotenv
MAILSENTRY_GMAIL_APP_PASSWORD=replace-with-the-generated-app-password
```

Run `mail-sentinel doctor` (or the equivalent Compose command) to perform a real
TLS IMAP login and read-only folder selection. `doctor --no-connect` verifies only
local configuration/secrets and is not proof that Google accepted the credential.

For planned rotation:

1. Create a new dedicated app password.
2. Replace the value in the VPS `.env`, restart/recreate the MailSentry service,
   and run `mail-sentinel doctor`.
3. After the new credential succeeds, remove the old app password from the Google
   Account's App Passwords page.

For suspected disclosure, revoke the exposed app password first, then create and
deploy a replacement. Google recommends removing app passwords for lost or unused
devices/apps, and changing the main Google Account password automatically revokes
all app passwords. An app password can be viewed only once; generate a replacement
instead of attempting to recover it. [Google app-password
documentation](https://support.google.com/accounts/answer/185833?hl=en)

## Discord and Apprise requirements

### Create the incoming webhook

A Discord account with the **Manage Webhooks** permission in the target channel is
required to create, edit, or delete a webhook. Discord's permission guide defines
that permission explicitly, and its API also requires `MANAGE_WEBHOOKS` when a
webhook is created. [Discord channel permission
guide](https://support.discord.com/hc/en-us/articles/10543994968087-Channel-Permissions-Settings-101),
[Discord webhook API](https://docs.discord.com/developers/resources/webhook)

In the Discord desktop/web UI:

1. Open **Server Settings → Integrations**.
2. Select **Create Webhook**.
3. Give it a recognizable name such as `MailSentry` and select the intended text
   channel.
4. Select **Copy Webhook URL**.

These are Discord's published UI steps. [Discord: Intro to
Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
A full bot/application is unnecessary for this one-way notification use case;
Discord describes incoming webhooks as a simple way to post to a channel without
a bot user or separate authentication. [Discord webhook
API](https://docs.discord.com/developers/resources/webhook)

### Convert the Discord URL to the Apprise URL

Discord supplies a URL shaped like:

```text
https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

Use its final two path components in MailSentry's recommended Apprise form:

```text
discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

Apprise officially supports both `discord://{WebhookID}/{WebhookToken}/` and
`discord://{botname}@{WebhookID}/{WebhookToken}/`, where `botname` optionally
overrides the displayed sender name. Apprise's page also lists a raw Discord HTTPS
webhook form. [Apprise Discord service
documentation](https://appriseit.com/services/discord/)

Prefer the `discord://` form in MailSentry documentation. Apprise's current page
still illustrates the legacy `discordapp.com` hostname while modern Discord UI
normally returns `discord.com`; extracting the ID and token avoids making the
guide depend on that hostname difference.

Store the Apprise URL in the environment variable named by
`notifications.urls_env`:

```yaml
notifications:
  urls_env: MAILSENTRY_APPRISE_URLS
```

```dotenv
MAILSENTRY_APPRISE_URLS=discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

MailSentry accepts comma-separated Apprise URLs in that environment variable, so
other notification destinations may be added alongside Discord.

### Discord validation, rotation, and revocation

Run `mail-sentinel doctor --notify`. It sends a real test notification through
every configured channel; confirm that exactly one test arrives in the intended
Discord channel. Apprise also documents a standalone CLI test with
`apprise -t "Hello" -b "World" "discord://webhook_id/webhook_token"` if isolating
MailSentry from Apprise is useful. [Apprise quick-start
documentation](https://appriseit.com/getting-started/quick-start/)

Discord does not document an in-place token-rotation workflow on the reviewed
pages. Use replacement rather than assuming a copied URL can be safely regenerated:

1. Create a new webhook for the intended channel.
2. Update the VPS `.env`, restart/recreate MailSentry, and validate it with
   `mail-sentinel doctor --notify`.
3. Delete the old webhook under Server Settings → Integrations.

For suspected disclosure, delete the old webhook immediately, then create and
deploy a replacement. Discord defines webhook deletion as permanent and supports
deletion either with `MANAGE_WEBHOOKS` or with the webhook token itself. [Discord
webhook API](https://docs.discord.com/developers/resources/webhook)

## Privacy and security caveats

- Keep both secrets only in `.env` or another protected secret store. Do not put
  them in `config.yaml`, screenshots, shell transcripts, support tickets, Issues,
  or commits.
- A Discord webhook URL is a bearer secret in practice: Discord labels its token
  secure, and token-bearing webhook operations require no additional
  authentication. Possession permits posting, querying/modifying the webhook, and
  deleting it; it does not by itself grant general read access to the channel.
  [Discord webhook API](https://docs.discord.com/developers/resources/webhook)
- Use a private, dedicated Discord channel where practical, and grant **Manage
  Webhooks** only to administrators who need it.
- Sender names, subjects, and Assessment reasons originate in or are influenced
  by untrusted email. Discord recommends sanitizing user-generated strings and
  using `allowed_mentions` to prevent unexpected mentions. Apprise also exposes
  an optional `ping` parameter. Until MailSentry has an explicit mention-safety
  regression test, avoid configuring `ping` and do not promise that hostile
  `@everyone`/role-like content is inert. [Discord execute-webhook
  guidance](https://docs.discord.com/developers/resources/webhook), [Apprise
  Discord service documentation](https://appriseit.com/services/discord/)
- Keep TLS certificate verification enabled. Apprise documents `verify=yes` as
  the default for secure destinations; do not add `verify=no` to work around a
  certificate or interception problem. [Apprise Discord service
  documentation](https://appriseit.com/services/discord/)
- Gmail app-password access is broader than MailSentry's read-only behavior. Use
  one dedicated app password per MailSentry deployment so it can be revoked
  independently, and rotate it after a VPS or backup exposure.

## User-guide claims that are safe to make

- Personal Gmail: IMAP is already enabled; use `imap.gmail.com:993` with TLS and a
  dedicated app password after enabling 2-Step Verification.
- Workspace: an administrator must enable IMAP, and app-password availability is
  policy-dependent. If the tenant requires OAuth, the current MailSentry release
  is not compatible.
- Discord: create an incoming webhook with Manage Webhooks permission, store
  `discord://ID/TOKEN/` in the Apprise environment variable, and test with
  `doctor --notify`.
- Never describe either credential as read-only. MailSentry's IMAP implementation
  is read-only; the credential remains sensitive and should be independently
  revocable.
