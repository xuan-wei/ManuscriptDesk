#!/usr/bin/env python3
"""Fail when public source contains private state, personal infrastructure, or secrets."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISALLOWED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".key", ".pem", ".p12", ".pfx", ".tar"}
DISALLOWED_PARTS = {
    "backups",
    "browser-profiles",
    "cutover",
    "data",
    "logs",
    "migration",
    "production",
    "runtime",
    "secret-store",
    "secrets",
    "screenshots",
    "state",
    "staging",
}
TEXT_PATTERNS = {
    "private absolute path": re.compile(r"/home/[A-Za-z0-9._-]+/"),
    "private IPv4 address": re.compile(
        r"(?<![0-9.])(?:10\.(?:\d{1,3}\.){2}\d{1,3}|169\.254\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9.])"
    ),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub personal token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
ALLOW_TEXT_SUFFIXES = {
    "",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}


def tracked_files() -> list[Path]:
    completed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True)
    return [ROOT / item.decode() for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        lowered = {part.casefold() for part in relative.parts}
        if lowered & DISALLOWED_PARTS:
            failures.append(f"private path: {relative}")
        if path.suffix.casefold() in DISALLOWED_SUFFIXES:
            failures.append(f"disallowed file type: {relative}")
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            failures.append(f"real environment file: {relative}")
        if not path.is_file() or path.suffix.casefold() not in ALLOW_TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"unexpected binary file: {relative}")
            continue
        for label, pattern in TEXT_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label}: {relative}")
    if failures:
        print("Public-tree check failed:", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Public-tree check passed for {len(tracked_files())} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
