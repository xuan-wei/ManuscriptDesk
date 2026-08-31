# Backup and restore

A Manuscript Desk state set consists of the SQLite database and the matching dynamic SecretStore. Back up and restore them together. Long-term application keys are deployment configuration and must also be retained securely, but they are intentionally outside the dynamic SecretStore backup produced by this script.

Never copy a live SQLite database file directly. SQLite may be using WAL mode; use the provided online-backup script.

## Create a backup

```bash
cd /srv/manuscript-desk
docker compose stop manuscript-desk
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v /srv/manuscript-desk/data/runtime:/runtime \
  -v /srv/manuscript-desk/data/secrets/data:/secrets:ro \
  -v /srv/manuscript-desk/backups:/backups \
  -v /path/to/source/scripts/backup_state.py:/backup_state.py:ro \
  manuscript-desk:local python /backup_state.py \
  --database /runtime/manuscripts.db \
  --secrets /secrets \
  --output /backups \
  --label production
docker compose up -d
```

The backup script uses SQLite's online backup API, so a running service is safe; the documented stop makes the state set quieter and simplifies an immediately consistent operational snapshot. The helper container runs as UID/GID 10001 and can read the private state without broadening host permissions.

The script:

- uses SQLite's online backup API;
- verifies `integrity_check` and foreign keys;
- rejects SecretStore symlinks, unsupported file types, and broadly readable secret files;
- writes SHA-256 values and schema metadata to a private manifest.

Separately back up `/srv/manuscript-desk/data/secrets/keys` using an encrypted, access-controlled system. Do not put keys or backups in Git.

## Restore rehearsal

1. Stop any service that would write to the restore destination.
2. Create a new, empty private deployment directory on a separate localhost port.
3. Copy `manuscripts.db` from one backup directory into its `data/runtime/` directory.
4. Copy that same backup directory's `secrets/` tree into `data/secrets/data/`.
5. Install the matching long-term keys for that state set.
6. Set ownership to `10001:10001`, directories to `0700`, and private files to `0600`.
7. Keep `MANUSCRIPT_DESK_CREATE_EMPTY_DB=0`.
8. Start the restored service and verify health, login, records, and secret-backed configuration.

Do not combine a database from one backup with SecretStore data from another. Do not start an older application image against a database that has already migrated to a newer schema.
