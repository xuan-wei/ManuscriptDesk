# Security Policy

## Supported versions

No public stable release is supported yet. Security fixes will be applied to the latest development branch until the first tagged release.

## Reporting a vulnerability

Open a private GitHub security advisory for the repository maintainer. Do not open a public issue containing exploit details.

Never submit:

- Real journal, SMTP, administrator, or user credentials.
- Raw Session, Shortcut, API, CAPTCHA, OTP, or rate-limit keys.
- Production databases, SQLite WAL/SHM files, SecretStore files, logs, or screenshots.
- Unpublished manuscript titles, identifiers, status histories, or personal email addresses.

Use synthetic `.test` accounts and redacted logs. Include the affected version, a minimal reproduction, expected behavior, and the security impact.

## Deployment expectations

Operators are responsible for HTTPS termination, restrictive filesystem permissions, key rotation, backups, and limiting administrative access. Browser automation should run with a single web worker until documented otherwise.
