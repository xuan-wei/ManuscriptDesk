# Public release checklist

A release candidate is not ready until all gates below pass.

## Source

- Wheel and sdist build from a clean checkout.
- Clean-environment wheel installation starts the application.
- All tests pass.
- Ruff, mypy, Bandit, dependency audit, license audit, and secret scanning pass.
- `scripts/check_public_tree.py` reports no private state, credentials, personal infrastructure, or disallowed binary files.

## Security-critical behavior

- Provider lookalike, redirect, Unicode, userinfo, non-HTTPS, and private-address tests pass.
- Browser tasks have hard deadlines and leave no Chrome/ChromeDriver processes.
- A failed manuscript in a journal batch rolls back the entire journal result.
- Unverified empty results do not alter manuscript state.
- Expired automation claims fence out late workers.
- Persisted and user-visible errors contain only allowlisted safe codes/messages.
- Shortcut Tokens fail closed for missing, expired, revoked, wrong-scope, or inactive-user cases.

## Container

- Build succeeds without private/local base images or runtime downloads.
- Process runs as a non-root user with one web worker.
- Healthcheck, read-only root filesystem, tmpfs, restart persistence, and browser/driver compatibility pass.
- Image vulnerability scan and SBOM are retained with the release manifest.

## Migration

- Fresh schema creation rehearsal passes.
- Two independent v7-to-current rehearsals pass using a WAL-consistent database backup and matching SecretStore copy.
- Integrity, foreign-key, secret-reference, aggregate-count, UI, and rollback checks pass.
- Existing production remains untouched until final cutover approval.
