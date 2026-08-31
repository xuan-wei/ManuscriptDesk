# Configuration

For Docker deployment, use `scripts/init_deployment.py` and the generated private `production.env`; see `docker-deployment.md`. The example below describes the same application settings for non-container development.

Manuscript Desk reads configuration from environment variables. Keep the real environment file outside the source repository and set it to mode `0600`.

## Required state

- `MANUSCRIPT_DESK_DB_PATH`: SQLite database path. Mount the containing directory, not only the file, so WAL and SHM sidecars remain available.
- `MANUSCRIPT_DESK_SECRET_ROOT`: writable SecretStore data directory, mode `0700`.
- `MANUSCRIPT_DESK_OTP_KEY_FILE`, `MANUSCRIPT_DESK_RATE_LIMIT_KEY_FILE`, `MANUSCRIPT_DESK_CAPTCHA_KEY_FILE`: three independent files, each containing at least 32 random bytes and mounted read-only.
- `MANUSCRIPT_DESK_PUBLIC_BASE_URL`: operator-controlled HTTPS origin used for application-generated links.

## First installation

Set `MANUSCRIPT_DESK_CREATE_EMPTY_DB=1` only for an empty data directory. Configure all three initial administrator values together. Prefer a password file. After the account is created and the first login succeeds, run `scripts/finalize_bootstrap.py` to clean the root-level bootstrap record and `data/secrets/bootstrap` password file.

## Worker constraint

Set `WEB_CONCURRENCY=1`. The current scheduler and automation queue are embedded in the web process. Future schema fencing improves correctness but does not make multi-worker deployment supported.

## Shortcut base URLs

Each user can set a separate externally reachable base URL on the Shortcut page. It is used only to generate Shortcut links; the server must never treat it as a trusted callback, provider destination, request origin, or invitation/email origin.
