#!/usr/bin/env python3
"""Create a WAL-consistent SQLite backup and matching SecretStore copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_secret_tree(source: Path, destination: Path) -> list[dict]:
    records = []
    source_root = source.resolve(strict=True)
    for item in sorted(source_root.rglob("*")):
        relative = item.relative_to(source_root)
        mode = stat.S_IMODE(item.lstat().st_mode)
        if item.is_symlink() or not (item.is_dir() or item.is_file()):
            raise RuntimeError(f"unsupported SecretStore entry: {relative}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(target, 0o700)
            continue
        if mode & 0o077:
            raise RuntimeError(f"SecretStore file permissions are too broad: {relative}")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(item, target, follow_symlinks=False)
        os.chmod(target, 0o600)
        records.append({"path": relative.as_posix(), "bytes": target.stat().st_size, "sha256": sha256(target)})
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--secrets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", default="state-backup")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = args.output / f"{args.label}-{timestamp}"
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=args.output))
    os.chmod(temporary, 0o700)
    try:
        database = temporary / "manuscripts.db"
        database.touch(mode=0o600, exist_ok=False)
        with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as source:
            with sqlite3.connect(database) as destination:
                source.backup(destination)
        os.chmod(database, 0o600)
        with sqlite3.connect(database) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            schema = connection.execute(
                "SELECT version,application_compatibility,migration_id FROM app_schema WHERE singleton=1"
            ).fetchone()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("database verification failed")
        secret_records = copy_secret_tree(args.secrets, temporary / "secrets")
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": {"bytes": database.stat().st_size, "sha256": sha256(database)},
            "schema": {"version": schema[0], "application_compatibility": schema[1], "migration_id": schema[2]},
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "secret_files": secret_records,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.chmod(manifest_path, 0o600)
        temporary.rename(final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
