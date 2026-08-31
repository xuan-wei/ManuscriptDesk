#!/usr/bin/env python3
"""Remove first-boot credentials after the administrator has logged in."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

BOOTSTRAP_ENV_PREFIXES = (
    "MANUSCRIPT_DESK_INITIAL_ADMIN_USERNAME=",
    "MANUSCRIPT_DESK_INITIAL_ADMIN_EMAIL=",
)
BOOTSTRAP_COMPOSE_LINES = (
    "MANUSCRIPT_DESK_INITIAL_ADMIN_PASSWORD_FILE:",
    "./data/secrets/bootstrap/initial-admin-password:",
)


def replace_private(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove Manuscript Desk first-boot credentials.")
    parser.add_argument("directory", type=Path, help="Deployment root created by init_deployment.py")
    args = parser.parse_args()

    target = args.directory.expanduser().resolve()
    env_path = target / "production.env"
    compose_path = target / "compose.yml"
    bootstrap_record = target / "bootstrap.txt"
    if not env_path.is_file() or not compose_path.is_file():
        raise SystemExit("Deployment files were not found")

    env_lines = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(BOOTSTRAP_ENV_PREFIXES):
            continue
        if line.startswith("MANUSCRIPT_DESK_CREATE_EMPTY_DB="):
            line = "MANUSCRIPT_DESK_CREATE_EMPTY_DB=0"
        env_lines.append(line)
    replace_private(env_path, "\n".join(env_lines) + "\n")

    compose_lines = [
        line
        for line in compose_path.read_text(encoding="utf-8").splitlines()
        if not any(marker in line for marker in BOOTSTRAP_COMPOSE_LINES)
    ]
    replace_private(compose_path, "\n".join(compose_lines) + "\n")

    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "-v",
            f"{target / 'data/secrets/bootstrap'}:/bootstrap",
            "python:3.12-slim-bookworm",
            "rm",
            "-f",
            "/bootstrap/initial-admin-password",
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Bootstrap configuration was disabled, but the password file could not be deleted. "
            "Remove data/secrets/bootstrap/initial-admin-password before restarting."
        )
    bootstrap_record.unlink(missing_ok=True)
    bootstrap_directory = target / "data/secrets/bootstrap"
    try:
        bootstrap_directory.rmdir()
    except OSError:
        pass

    print("Bootstrap credentials removed.")
    print(f"Recreate the service: cd '{target}' && docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
