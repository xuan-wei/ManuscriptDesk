#!/usr/bin/env python3
"""Initialize a private deployment directory outside the Git worktree."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

SOURCE_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_UID = 10001
CONTAINER_GID = 10001

COMPOSE = """services:
  state-init:
    image: ${{MANUSCRIPT_DESK_IMAGE:-manuscript-desk:local}}
    build:
      context: {source_root}
      dockerfile: Dockerfile
    user: "0:0"
    command:
      - /bin/sh
      - -c
      - >-
        mkdir -p /managed-data/runtime /managed-data/secrets/data /managed-data/secrets/keys /managed-data/secrets/bootstrap /managed-backups &&
        chown -R 10001:10001 /managed-data/runtime /managed-data/secrets /managed-backups &&
        chmod 0711 /managed-data &&
        find /managed-data/runtime /managed-data/secrets /managed-backups -type d -exec chmod 0700 {{}} + &&
        find /managed-data/runtime /managed-data/secrets /managed-backups -type f -exec chmod 0600 {{}} +
    volumes:
      - ./data:/managed-data
      - ./backups:/managed-backups
    network_mode: none
    read_only: true
    cap_drop: ["ALL"]
    cap_add: ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    security_opt: ["no-new-privileges:true"]

  manuscript-desk:
    image: ${{MANUSCRIPT_DESK_IMAGE:-manuscript-desk:local}}
    build:
      context: {source_root}
      dockerfile: Dockerfile
    depends_on:
      state-init:
        condition: service_completed_successfully
    user: "10001:10001"
    restart: unless-stopped
    env_file:
      - production.env
    environment:
      WEB_CONCURRENCY: "1"
      MANUSCRIPT_DESK_DB_PATH: /runtime/manuscripts.db
      MANUSCRIPT_DESK_SECRET_ROOT: /run/manuscript-desk-secrets/data
      MANUSCRIPT_DESK_CHROMIUM: /usr/bin/chromium
      MANUSCRIPT_DESK_CHROMEDRIVER: /usr/bin/chromedriver
      MANUSCRIPT_DESK_OTP_KEY_FILE: /run/manuscript-desk-keys/otp.key
      MANUSCRIPT_DESK_RATE_LIMIT_KEY_FILE: /run/manuscript-desk-keys/rate.key
      MANUSCRIPT_DESK_CAPTCHA_KEY_FILE: /run/manuscript-desk-keys/captcha.key
      MANUSCRIPT_DESK_INITIAL_ADMIN_PASSWORD_FILE: /run/manuscript-desk-bootstrap/initial-admin-password
      HOME: /tmp/manuscript-desk-home
      XDG_CONFIG_HOME: /tmp/manuscript-desk-config
      XDG_CACHE_HOME: /tmp/manuscript-desk-cache
    volumes:
      - ./data/runtime:/runtime
      - ./data/secrets/data:/run/manuscript-desk-secrets/data
      - ./data/secrets/keys:/run/manuscript-desk-keys:ro
      - ./data/secrets/bootstrap/initial-admin-password:/run/manuscript-desk-bootstrap/initial-admin-password:ro
    ports:
      - "${{MANUSCRIPT_DESK_LISTEN_ADDRESS:-127.0.0.1}}:${{MANUSCRIPT_DESK_PORT:-8008}}:8008"
    shm_size: 2gb
    read_only: true
    tmpfs:
      - /tmp:size=512m,mode=1777
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    logging:
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/api/health', timeout=3).read()"]
      interval: 15s
      timeout: 5s
      retries: 6
      start_period: 30s
"""

ENVIRONMENT = """MANUSCRIPT_DESK_PUBLIC_BASE_URL={public_base_url}
MANUSCRIPT_DESK_COOKIE_SECURE={cookie_secure}
MANUSCRIPT_DESK_CREATE_EMPTY_DB=1
MANUSCRIPT_DESK_INITIAL_ADMIN_USERNAME={admin_username}
MANUSCRIPT_DESK_INITIAL_ADMIN_EMAIL={admin_email}
MANUSCRIPT_DESK_SHORTCUT_WAIT_SECONDS=110
MANUSCRIPT_DESK_SHORTCUT_POLL_SECONDS=0.5
MANUSCRIPT_DESK_SHORTCUT_CONTINUATION_TTL_SECONDS=10800
MANUSCRIPT_DESK_LISTEN_ADDRESS=127.0.0.1
MANUSCRIPT_DESK_PORT=8008
"""


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def git_worktree(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(completed.stdout.strip()).resolve() if completed.returncode == 0 else None


def validate_input(label: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(character in cleaned for character in "\r\n\0"):
        raise SystemExit(f"Invalid {label}")
    return cleaned


def validate_public_base_url(value: str) -> str:
    cleaned = validate_input("public base URL", value).rstrip("/")
    parsed = urlsplit(cleaned)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if parsed.scheme != "https" and not local_http:
        raise SystemExit("Use HTTPS, except for localhost-only development")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise SystemExit("Invalid public base URL")
    if parsed.path not in {"", "/"}:
        raise SystemExit("The public base URL must be an origin without a path")
    return cleaned


def secure_state_ownership(target: Path) -> bool:
    data = target / "data"
    managed = [
        data / "runtime",
        data / "secrets",
        target / "backups",
    ]
    try:
        os.chmod(data, 0o711)
        for root in managed:
            for path in [root, *sorted(root.rglob("*"))]:
                os.chown(path, CONTAINER_UID, CONTAINER_GID)
    except PermissionError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a safe Manuscript Desk deployment directory outside Git.")
    parser.add_argument("directory", type=Path, help="New deployment root, for example /srv/manuscript-desk")
    parser.add_argument("--public-base-url", required=True, help="Externally reachable HTTPS URL")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-username", default="admin")
    args = parser.parse_args()

    target = args.directory.expanduser().resolve()
    source = SOURCE_ROOT.resolve()
    worktree = git_worktree(source)
    if inside(target, source) or (worktree and inside(target, worktree)):
        raise SystemExit("Deployment state must be outside the Git worktree")
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"Refusing to overwrite non-empty directory: {target}")

    public_base_url = validate_public_base_url(args.public_base_url)
    admin_email = validate_input("administrator email", args.admin_email)
    admin_username = validate_input("administrator username", args.admin_username)

    paths = [
        target,
        target / "data",
        target / "data/runtime",
        target / "data/secrets",
        target / "data/secrets/data",
        target / "data/secrets/keys",
        target / "data/secrets/bootstrap",
        target / "backups",
    ]
    for path in paths:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    keys = target / "data/secrets/keys"
    for name in ("otp.key", "rate.key", "captcha.key"):
        write_private(keys / name, secrets.token_urlsafe(48) + "\n")
    initial_password = secrets.token_urlsafe(30)
    write_private(target / "data/secrets/bootstrap/initial-admin-password", initial_password + "\n")

    cookie_secure = "0" if public_base_url.startswith("http://") else "1"
    write_private(
        target / "production.env",
        ENVIRONMENT.format(
            public_base_url=public_base_url,
            cookie_secure=cookie_secure,
            admin_username=admin_username,
            admin_email=admin_email,
        ),
    )
    write_private(target / "compose.yml", COMPOSE.format(source_root=source.as_posix()))
    write_private(
        target / "bootstrap.txt",
        (
            f"Initial administrator: {admin_username} ({admin_email})\n"
            f"Initial password: {initial_password}\n"
            "Delete this file after the first successful login.\n"
        ),
    )

    ownership_ready = secure_state_ownership(target)
    print("Deployment files created.")
    print(f"Directory: {target}")
    print(f"Bootstrap credentials: {target / 'bootstrap.txt'}")
    if not ownership_ready:
        print("Container state ownership will be prepared automatically on startup.")
    print()
    print("Validate and start Manuscript Desk:")
    print(f"  cd '{target}'")
    print("  docker compose config --quiet")
    print("  docker compose up -d --build")
    print("  docker compose ps")
    print()
    print("After the first successful login, run:")
    print(f"  python '{source / 'scripts/finalize_bootstrap.py'}' '{target}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
