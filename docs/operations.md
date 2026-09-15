# MailSentry operations runbook

This runbook covers the single-host Docker Compose deployment. Run commands from
the directory containing `compose.yaml`; replace angle-bracket placeholders first.

## Install on a server

1. Install Docker Engine and the Compose plugin from Docker's official instructions.
   Confirm `docker version` and `docker compose version` succeed.
2. Clone a reviewed revision and create local-only configuration:

   ```bash
   git clone https://github.com/xiaoyangxya/MailSentry.git
   cd MailSentry
   git checkout --detach <commit-or-release-tag>
   cp config.example.yaml config.yaml
   cp .env.example .env
   chmod 600 config.yaml .env
   ```

3. Edit both files. Mailbox passwords, webhook URLs, and cloud API keys belong in
   `.env`; keep `.env`, `config.yaml`, databases, and real mail data out of Git and
   terminal output.
4. Build, validate, bootstrap the read-only cursor, and start:

   ```bash
   docker compose build --pull mailsentry
   docker compose run --rm --no-deps mailsentry doctor --no-connect
   docker compose run --rm --no-deps mailsentry doctor --notify
   docker compose run --rm --no-deps mailsentry bootstrap
   docker compose up -d --wait mailsentry
   docker compose run --rm --no-deps mailsentry status --connect
   docker compose logs --since 10m --timestamps mailsentry
   ```

Installation is complete when the container is healthy, all Monitored Accounts are
initialized and connect, and the notification test arrives.

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

The integrity command must print `ok`. Copy the database and checksum to encrypted,
access-controlled storage on another host. Back up `.env` and `config.yaml`
separately as secrets; never attach them to issues or log bundles.

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
docker compose run --rm --no-deps mailsentry doctor --no-connect
docker compose run --rm --no-deps mailsentry status --connect
```

Recovery is complete when checksum and integrity checks pass, the container is
healthy, saved cursors exist, and all read-only account connections succeed.

## Rotate credentials

Rotate one credential at a time:

1. Create the replacement mailbox app password, Apprise URL/token, or cloud API key.
2. Update only its named variable in `.env`, retaining mode `0600`.
3. Run `docker compose up -d --force-recreate --wait mailsentry`, followed by
   `doctor --no-connect`, `status --connect`, and recent logs.
4. For Apprise also run `doctor --notify`; for a cloud key run `explain --cloud`
   with synthetic content as shown in the README.
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
docker compose run --rm --no-deps mailsentry doctor --no-connect
docker compose run --rm --no-deps mailsentry status --connect
docker compose logs --since 10m --timestamps mailsentry
```

If health or behavior regresses, run the preserved image immediately:

```bash
MAILSENTRY_IMAGE="mailsentry:rollback-$STAMP" \
  docker compose up -d --no-build --force-recreate --wait mailsentry
docker compose run --rm --no-deps mailsentry status --connect
```

Fast rollback keeps the current database. Restore the pre-upgrade snapshot only if
release notes require it or the old image cannot read the migrated database. After
stabilization, check out the recorded old revision and rebuild it for a durable
rollback. Retain both snapshot and rollback image through the soak period.

## Collect logs and respond to incidents

Collect evidence without exporting `.env`, `config.yaml`, databases, or raw mail:

```bash
docker compose ps
docker compose run --rm --no-deps mailsentry status
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
3. **Diagnose:** use `status --connect` for IMAP, `doctor --notify` for delivery,
   and synthetic `explain --cloud` for cloud failures. Check pending and permanently
   failed notification counts before restarting.
4. **Recover:** rotate affected credentials, restore or roll back if required,
   recreate the service, and repeat health checks.
5. **Close:** confirm new mail advances every account cursor, expected notifications
   arrive, and sanitized logs contain no secrets or message bodies.

Never use `docker compose down -v` for routine operations: `-v` deletes the named
metadata volume.
