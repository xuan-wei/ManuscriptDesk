# Docker deployment

The supported deployment path keeps source and private state physically separate. Run the initializer from a clean source checkout; do not copy a database, keys, or a real environment file into the repository.

## Requirements

- Linux host with Python 3.10 or later.
- Docker Engine and the Docker Compose plugin.
- Enough memory and disk for Chromium, SQLite, images, and backups.
- An HTTPS reverse proxy for any remotely reachable deployment.

The image runs as UID/GID `10001:10001`, uses a read-only root filesystem, drops all capabilities, and requires `WEB_CONCURRENCY=1` because the scheduler is embedded in the web process.

## Initialize

From the source checkout:

```bash
python scripts/init_deployment.py /srv/manuscript-desk \
  --public-base-url https://desk.example.com \
  --admin-email admin@example.com
```

The generated Compose file includes a short-lived, network-disabled `state-init` service. It runs with only the host capabilities needed to assign the private bind-mounted state to UID/GID 10001, then exits before the application starts. This avoids host-specific manual `chown` steps and does not make state world-readable or world-writable.

The command creates:

```text
/srv/manuscript-desk/
├── deployment/
│   ├── compose.yml
│   ├── production.env
│   └── bootstrap.txt
├── state/
│   ├── runtime/
│   ├── secrets/data/
│   ├── secrets/keys/{otp.key,rate.key,captcha.key}
│   └── bootstrap/initial-admin-password
└── backups/
```

Each key is independently generated. Private files use mode `0600`; sensitive directories use `0700`. The `state/` boundary itself is traversal-only (`0711`) so the host operator can reach the separately protected runtime and backup paths without being able to list state contents. The plaintext bootstrap password exists only in the private deployment tree and must be deleted after first login.

## Validate and start

```bash
cd /srv/manuscript-desk/deployment
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8008/api/health
```

If the container does not become healthy, inspect `docker compose logs manuscript-desk`. Do not paste logs publicly until you have checked them for sensitive data.

For production, configure HTTPS first and browse to the exact origin supplied as `--public-base-url`. For a localhost-only rehearsal, initialize with `http://127.0.0.1:8008`; this deliberately disables secure cookies and is not suitable for remote exposure.

## Complete first boot

Read `/srv/manuscript-desk/deployment/bootstrap.txt`, sign in, and confirm the administrator account works. Then run:

```bash
python /path/to/source/scripts/finalize_bootstrap.py /srv/manuscript-desk
cd /srv/manuscript-desk/deployment
docker compose config --quiet
docker compose up -d
```

The finalizer:

- changes `MANUSCRIPT_DESK_CREATE_EMPTY_DB` from `1` to `0`;
- removes the initial administrator username and email;
- removes the initial-password environment setting and bind mount;
- deletes the initial password and plaintext bootstrap record.

After recreation, verify health and sign in again. With empty-database creation disabled, accidental database loss fails closed instead of silently creating a new installation.

## Acceptance checks

Before entering real data:

1. Confirm `docker compose ps` reports `healthy`.
2. Confirm the service is reachable only through the intended interface.
3. Confirm the HTTPS origin matches `MANUSCRIPT_DESK_PUBLIC_BASE_URL`.
4. Restart and recreate the container; verify the administrator account persists.
5. Create and restore a paired backup as described in `backup-restore.md`.
6. Verify Chromium and ChromeDriver by running a controlled update with a test account when available.

Do not run two production containers or schedulers against the same state directory.
