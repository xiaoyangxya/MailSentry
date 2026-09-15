# MailSentry operations runbook

This runbook covers the single-host Docker Compose deployment. Run commands from
the directory containing `compose.yaml`; replace angle-bracket placeholders first.

## Install on a server

1. Install Docker Engine and the Compose plugin from Docker's official instructions.
   Confirm `docker version` and `docker compose version` succeed.
2. Clone a reviewed revision and create local-only legacy import templates:

   ```bash
   git clone https://github.com/xiaoyangxya/MailSentry.git
   cd MailSentry
   git checkout --detach <commit-or-release-tag>
   cp config.example.yaml config.yaml
   cp .env.example .env
   chmod 600 config.yaml .env
   ```

3. Protect both files. Existing deployments may leave their reviewed values in
   place for confirmed import; new deployments can keep the examples until the Web
   wizard creates the first Configuration Draft.
4. Build and start the localhost-only control panel:

   ```bash
   docker compose up -d --build --wait mailsentry
   docker compose logs --tail 20 mailsentry
   curl -fsS http://127.0.0.1:8080/healthz
   ```

5. Open the one-time setup path from the logs through `127.0.0.1:8080`, an SSH
   tunnel, or the configured HTTPS proxy. Create the administrator, confirm an
   import or enter settings, run each integration test, preview 24 hours of each
   new mailbox, and activate the tested revision.

Installation is complete when the dashboard reports the active revision and
running workers, each Monitored Account has a cursor, and the test notification
arrives.

For public access, point a DNS name at the host, set `MAILSENTRY_DOMAIN`, and
start the optional HTTPS profile:

```bash
export MAILSENTRY_DOMAIN=mail.example.com
docker compose --profile https up -d --build --wait
```

Alternatively keep the localhost binding and use an SSH tunnel, existing reverse
proxy, or private tunnel. Never expose port 8080 directly to the public Internet.
If the administrator password or TOTP device is lost, recover only from the server:

```bash
docker compose exec -T mailsentry mail-sentinel reset-admin \
  --state-database /app/data/mailsentry.db \
  --managed-dir /config/managed
```

## Create a consistent backup

Back up before upgrades or migrations and regularly within the retention window.
The SQLite Backup API includes committed WAL data and is consistent while the
service runs.

```bash
mkdir -p backups
chmod 700 backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
CONTAINER_BACKUP="/app/data/mailsentry-$STAMP.db"
docker compose exec -T -e BACKUP_PATH="$CONTAINER_BACKUP" mailsentry \
  python -c "import os, sqlite3; src=sqlite3.connect('/app/data/mailsentry.db'); dst=sqlite3.connect(os.environ['BACKUP_PATH']); src.backup(dst); print(dst.execute('PRAGMA integrity_check').fetchone()[0]); dst.close(); src.close()"
docker compose cp "mailsentry:$CONTAINER_BACKUP" "backups/mailsentry-$STAMP.db"
docker compose exec -T -e BACKUP_PATH="$CONTAINER_BACKUP" mailsentry \
  python -c "import os; os.remove(os.environ['BACKUP_PATH'])"
chmod 600 "backups/mailsentry-$STAMP.db"
sha256sum "backups/mailsentry-$STAMP.db" > "backups/mailsentry-$STAMP.db.sha256"
```

The integrity command must print `ok`. The `mailsentry-config` volume contains
restricted Configuration Revision secrets and must be backed up separately into
encrypted, access-controlled storage:

```bash
docker compose exec -T mailsentry tar -C /config/managed -czf - . \
  > "backups/mailsentry-managed-$STAMP.tgz"
chmod 600 "backups/mailsentry-managed-$STAMP.tgz"
sha256sum "backups/mailsentry-managed-$STAMP.tgz" \
  > "backups/mailsentry-managed-$STAMP.tgz.sha256"
```

Never attach the database, managed archive, `.env`, or `config.yaml` to Issues
or log bundles. A redacted Web export is not a secret backup.

## Restore a backup

Restore replaces current metadata, cursors, Feedback, digest state, and queued
notifications. Preserve the current state first unless incident handling forbids it.

```bash
BACKUP_FILE=<mailsentry-timestamp.db>
sha256sum -c "backups/$BACKUP_FILE.sha256"
docker compose stop mailsentry
docker compose run --rm --no-deps --user 0:0 \
  -v "$PWD/backups:/backup:ro" -e BACKUP_FILE="$BACKUP_FILE" \
  --entrypoint python mailsentry -c "import os, pathlib, shutil, sqlite3; source=pathlib.Path('/backup') / os.environ['BACKUP_FILE']; check=sqlite3.connect(f'file:{source}?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0]; assert check == 'ok', check; target=pathlib.Path('/app/data/mailsentry.db'); [path.unlink(missing_ok=True) for path in (target, pathlib.Path(str(target) + '-wal'), pathlib.Path(str(target) + '-shm'))]; shutil.copyfile(source, target); os.chown(target, 10001, 10001); os.chmod(target, 0o600)"
docker compose up -d --wait mailsentry
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env doctor --config /config/legacy/config.yaml --no-connect
docker compose run --rm --no-deps mailsentry --env-file /config/legacy/.env status --config /config/legacy/config.yaml --connect
```

Recovery is complete when checksum and integrity checks pass, the container is
healthy, saved cursors exist, and all read-only account connections succeed.

When restoring the matching managed archive, verify its checksum, stop the
application, and restore into the existing configuration volume without exposing
the archive contents:

```bash
MANAGED_ARCHIVE=<mailsentry-managed-timestamp.tgz>
sha256sum -c "backups/$MANAGED_ARCHIVE.sha256"
docker compose stop mailsentry
docker compose run --rm --no-deps --user 10001:10001 \
  -v "$PWD/backups:/backup:ro" -e MANAGED_ARCHIVE="$MANAGED_ARCHIVE" \
  --entrypoint sh mailsentry \
  -c 'find /config/managed -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + &&
      tar -C /config/managed -xzf "/backup/$MANAGED_ARCHIVE"'
docker compose up -d --wait mailsentry
curl -fsS http://127.0.0.1:8080/healthz
```

This is intentionally a server-side disaster-recovery operation. The Web UI does
not provide restore, factory-reset, or arbitrary history deletion actions.

## Rotate credentials

Rotate one credential at a time:

1. Create the replacement mailbox app password, Apprise URL/token, or cloud API key.
2. In the Web configuration page, replace only that secret in a new Configuration
   Draft. Blank fields retain the current value.
3. Run the relevant read-only mailbox, real notification, or synthetic cloud test.
4. Activate the tested revision and confirm dashboard worker health.
5. Revoke the old credential only after validation. On failure, restore the prior
   environment value and recreate the service without printing either secret.

## Upgrade and roll back

Start from a clean deployment checkout. Record the revision, preserve the current
image, and run the backup procedure before changing code:

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
git status --short
git rev-parse HEAD | tee "backups/source-$STAMP.txt"
docker image tag mailsentry:local "mailsentry:rollback-$STAMP"
```

Upgrade to a reviewed tag or commit:

```bash
TARGET_REF=<commit-or-release-tag>
git fetch --tags --prune
git checkout --detach "$TARGET_REF"
docker compose build --pull mailsentry
docker compose up -d --wait mailsentry
curl -fsS http://127.0.0.1:8080/healthz
docker compose logs --since 10m --timestamps mailsentry
```

If health or behavior regresses, run the preserved image immediately:

```bash
MAILSENTRY_IMAGE="mailsentry:rollback-$STAMP" \
  docker compose up -d --no-build --force-recreate --wait mailsentry
curl -fsS http://127.0.0.1:8080/healthz
```

Fast rollback keeps the current database. Restore the pre-upgrade snapshot only if
release notes require it or the old image cannot read the migrated database. After
stabilization, check out the recorded old revision and rebuild it for a durable
rollback. Retain both snapshot and rollback image through the soak period.

## Run and reconcile the ten-account soak

The soak harness is offline: it creates synthetic headers, attachment metadata,
flags, Message-IDs, UIDVALIDITY changes, one disconnect, one notification retry,
and a service reconstruction. It does not read `config.yaml`, load credentials, or
connect to IMAP, cloud, or notification providers. Use a new output directory and
the exact image being considered for release:

```bash
RELEASE_SHA=$(git rev-parse HEAD)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SOAK_DIR="$PWD/soak-evidence/$RELEASE_SHA-$STAMP"
sudo install -d -m 0770 -o 10001 -g "$(id -g)" "$SOAK_DIR"
docker run --rm --name mailsentry-soak \
  --read-only --tmpfs /tmp:size=16m --user 10001:10001 \
  -v "$SOAK_DIR:/soak" mailsentry:local \
  soak --database /soak/mailsentry-soak.db \
  --metrics /soak/mailsentry-soak.jsonl \
  --duration-hours 24 --event-interval-seconds 300 \
  --accounts 10 --seed 20260915 --rss-limit-mib 512 \
  | tee "$SOAK_DIR/summary.json"
```

Leave the foreground process running for 24 hours. Stop an unwanted run with
Ctrl+C or `docker stop --time 30 mailsentry-soak`; interrupted output is not release
evidence, and the same paths cannot be reused. For a quick functional rehearsal,
add `--accelerated`; its summary always reports `release_evidence: false`.

The command exits nonzero on event loss, persisted or delivered duplicates, cursor
mismatch, an unhealthy outbox, a persisted privacy marker, or peak RSS above the
limit. A release-eligible result must exit zero and end with one JSON summary where
`passed` and `release_evidence` are both true, `generated` equals `persisted`,
`cursor_mismatches`, `duplicate_events`, `duplicate_notifications`, and
`notifications_pending` are zero, `disconnects` and `notification_failures` are
one, and `peak_rss_mib` is below 512. Preserve these three files with the frozen
source SHA:

```bash
tail -n 1 "$SOAK_DIR/mailsentry-soak.jsonl"
python -m json.tool "$SOAK_DIR/summary.json"
sha256sum "$SOAK_DIR"/* > "$SOAK_DIR/SHA256SUMS"
sudo chown -R "$(id -u):$(id -g)" "$SOAK_DIR"
```

The JSONL file contains timestamped phase samples with per-account generated and
observed counts, saved cursors, notification counts, and current/peak RSS, followed
by the final reconciliation. Attach only these synthetic artifacts to the release
checklist; never include production databases, configuration, or environment files.

## Collect logs and respond to incidents

Collect evidence without exporting `.env`, `config.yaml`, databases, or raw mail:

```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
docker compose logs --since 1h --timestamps --no-color mailsentry > mailsentry.log
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q mailsentry)"
git rev-parse HEAD
docker version
docker compose version
```

Use this order:

1. **Contain:** stop MailSentry if credentials may be exposed or outbound behavior
   is unexpected; revoke exposed mailbox, notification, or cloud credentials.
2. **Preserve:** record UTC times and versions; take a consistent backup unless the
   database itself is suspected. Keep evidence private.
3. **Diagnose:** use dashboard worker status and explicit integration tests. Check
   pending and permanently failed notification counts before restarting.
4. **Recover:** rotate affected credentials, restore or roll back if required,
   recreate the service, and repeat health checks.
5. **Close:** confirm new mail advances every account cursor, expected notifications
   arrive, and sanitized logs contain no secrets or message bodies.

Never use `docker compose down -v` for routine operations: `-v` deletes the named
metadata volume.
