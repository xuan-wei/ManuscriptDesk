# Manuscript Desk

Manuscript Desk is a self-hosted, multi-user dashboard for tracking journal manuscript submissions, preserving status history, grouping cross-journal submission journeys, and running controlled browser-based updates.

> Manuscript Desk is an independent community project. It is not affiliated with, endorsed by, or sponsored by Clarivate, ScholarOne, Aries Systems, or Editorial Manager.

## Release status

Version `0.1.0` is an alpha release. Rehearse deployment and restoration with synthetic data before entering real journal credentials or manuscript data.

## Quick Docker deployment

Requirements: Linux, Python 3.10+, Docker Engine, and the Docker Compose plugin. Keep deployment state outside the cloned repository.

```bash
git clone <repository-url> ManuscriptDesk
cd ManuscriptDesk
python scripts/init_deployment.py /srv/manuscript-desk \
  --public-base-url https://desk.example.com \
  --admin-email admin@example.com

cd /srv/manuscript-desk/deployment
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The initializer generates three independent application keys and a strong one-time administrator password. Read the credentials from `/srv/manuscript-desk/deployment/bootstrap.txt`, log in, and then remove all bootstrap material:

```bash
python /path/to/ManuscriptDesk/scripts/finalize_bootstrap.py /srv/manuscript-desk
cd /srv/manuscript-desk/deployment
docker compose up -d
```

The service listens on `127.0.0.1:8008` by default. Put an HTTPS reverse proxy in front of it for remote use. For a localhost-only rehearsal, `--public-base-url http://127.0.0.1:8008` is accepted and secure cookies are disabled for that rehearsal only.

See [Docker deployment](docs/docker-deployment.md), [reverse proxy](docs/reverse-proxy.md), [backup and restore](docs/backup-restore.md), and [upgrading](docs/upgrading.md).

## Repository boundary

This Git repository contains source code only. Production databases, SQLite WAL/SHM files, SecretStore data, keys, browser profiles, logs, screenshots, backups, and deployment-specific environment files must remain outside the Git worktree.

Recommended layout:

```text
ManuscriptDesk/             # public source checkout
/srv/manuscript-desk/
├── deployment/             # private Compose and environment files
├── state/                  # database, SecretStore, and long-term keys
└── backups/                # paired database + SecretStore backups
```

Only the source checkout belongs in Git. The initializer refuses to create state inside its Git worktree and refuses to overwrite a nonempty deployment directory.

## Development

Requires Python 3.10 or later.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

Supported application entrypoints:

```bash
python -m manuscript_desk --port 8008
manuscript-desk --port 8008
```

Application startup requires three independent keys containing at least 32 bytes, a writable SecretStore directory, and either an existing database or explicit empty-database creation. See `.env.example` and `docs/configuration.md`.

## Security model

- Owner-scoped data access for tenant records.
- Hashed database sessions and Argon2id passwords.
- CSRF protection for cookie-authenticated mutations.
- Raw Shortcut Tokens remain in SecretStore; SQLite stores their hashes and metadata.
- Journal credentials are entered only on validated provider origins.
- Databases and SecretStore content are backed up and restored as a matching pair.
- The supported embedded scheduler deployment uses exactly one web worker.

Report vulnerabilities privately using the process in `SECURITY.md`. Never include real credentials, tokens, unpublished manuscript data, or database files in a report.

## License

MIT. See `LICENSE`.
