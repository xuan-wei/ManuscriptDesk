from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
INIT = SOURCE_ROOT / "scripts/init_deployment.py"
FINALIZE = SOURCE_ROOT / "scripts/finalize_bootstrap.py"


def run_script(script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *(str(argument) for argument in arguments)],
        cwd=SOURCE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_initializer_creates_external_private_deployment(tmp_path):
    target = tmp_path / "deployment-root"
    completed = run_script(
        INIT,
        target,
        "--public-base-url",
        "http://127.0.0.1:8008",
        "--admin-email",
        "admin@example.com",
    )
    assert completed.returncode == 0, completed.stderr

    production_env = target / "production.env"
    compose = target / "compose.yml"
    bootstrap = target / "bootstrap.txt"
    assert production_env.is_file()
    assert compose.is_file()
    assert bootstrap.is_file()
    assert "MANUSCRIPT_DESK_CREATE_EMPTY_DB=1" in production_env.read_text()
    compose_text = compose.read_text()
    assert SOURCE_ROOT.as_posix() in compose_text
    assert "10001:10001" in compose_text
    assert "./data/runtime:/runtime" in compose_text
    assert "./backups:/managed-backups" in compose_text
    assert "MANUSCRIPT_DESK_CHROMIUM: /usr/bin/chromium" in compose_text
    assert "MANUSCRIPT_DESK_CHROMEDRIVER: /usr/bin/chromedriver" in compose_text
    assert "HOME: /tmp/manuscript-desk-home" in compose_text
    assert "XDG_CONFIG_HOME: /tmp/manuscript-desk-config" in compose_text
    assert 'cap_drop: ["ALL"]' in compose_text
    assert 'security_opt: ["no-new-privileges:true"]' in compose_text
    assert "privileged:" not in compose_text
    assert "../state" not in compose_text
    assert "../backups" not in compose_text
    assert "Initial password:" in bootstrap.read_text()

    private_files = [
        production_env,
        compose,
        bootstrap,
        target / "data/secrets/keys/otp.key",
        target / "data/secrets/keys/rate.key",
        target / "data/secrets/keys/captcha.key",
        target / "data/secrets/bootstrap/initial-admin-password",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in private_files)
    assert (target / "data").stat().st_mode & 0o777 == 0o711
    for directory in (
        target / "data/runtime",
        target / "data/secrets",
        target / "data/secrets/data",
        target / "data/secrets/keys",
        target / "data/secrets/bootstrap",
        target / "backups",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
    for key in private_files[3:6]:
        assert len(key.read_bytes().strip()) >= 32


def test_initializer_refuses_source_tree_and_nonempty_target(tmp_path):
    inside_source = SOURCE_ROOT / ".deployment-test"
    completed = run_script(
        INIT,
        inside_source,
        "--public-base-url",
        "https://desk.example.com",
        "--admin-email",
        "admin@example.com",
    )
    assert completed.returncode != 0
    assert "outside the Git worktree" in completed.stderr

    target = tmp_path / "nonempty"
    target.mkdir()
    (target / "keep").write_text("do not replace")
    completed = run_script(
        INIT,
        target,
        "--public-base-url",
        "https://desk.example.com",
        "--admin-email",
        "admin@example.com",
    )
    assert completed.returncode != 0
    assert (target / "keep").read_text() == "do not replace"


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://user@example.com", "https://example.com/path", "https://example.com#fragment"],
)
def test_initializer_rejects_unsafe_public_base_urls(tmp_path, url):
    completed = run_script(
        INIT,
        tmp_path / url.replace("/", "_"),
        "--public-base-url",
        url,
        "--admin-email",
        "admin@example.com",
    )
    assert completed.returncode != 0


def test_finalize_bootstrap_removes_plaintext_and_disables_creation(tmp_path):
    target = tmp_path / "deployment-root"
    initialized = run_script(
        INIT,
        target,
        "--public-base-url",
        "http://localhost:8008",
        "--admin-email",
        "admin@example.com",
    )
    assert initialized.returncode == 0, initialized.stderr

    completed = run_script(FINALIZE, target)
    assert completed.returncode == 0, completed.stderr
    environment = (target / "production.env").read_text()
    compose = (target / "compose.yml").read_text()
    assert "MANUSCRIPT_DESK_CREATE_EMPTY_DB=0" in environment
    assert "MANUSCRIPT_DESK_INITIAL_ADMIN_" not in environment
    assert "INITIAL_ADMIN_PASSWORD_FILE" not in compose
    assert "data/secrets/bootstrap/initial-admin-password" not in compose
    assert not (target / "bootstrap.txt").exists()
    assert not (target / "data/secrets/bootstrap/initial-admin-password").exists()
