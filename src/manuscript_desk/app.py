import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .account_mail import AccountMailer
from .automation_runtime import AutomationRuntime
from .repository import Repository
from .schema import create_database, migrate_database
from .secrets_store import SecretStore
from .security import hash_password, normalize_email, normalize_username
from .web import create_app


def key_from_env(name: str) -> bytes:
    value = os.environ.get(name, "")
    file_path = os.environ.get(f"{name}_FILE", "")
    if file_path:
        value = Path(file_path).read_text(encoding="utf-8").strip()
    if len(value.encode("utf-8")) < 32:
        raise RuntimeError(f"{name} must contain at least 32 bytes")
    return value.encode("utf-8")


def initial_admin_password() -> str:
    file_path = os.environ.get("MANUSCRIPT_DESK_INITIAL_ADMIN_PASSWORD_FILE", "")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
    return os.environ.get("MANUSCRIPT_DESK_INITIAL_ADMIN_PASSWORD", "")


def ensure_initial_admin(repository: Repository) -> None:
    username = os.environ.get("MANUSCRIPT_DESK_INITIAL_ADMIN_USERNAME", "").strip()
    email = os.environ.get("MANUSCRIPT_DESK_INITIAL_ADMIN_EMAIL", "").strip()
    password = initial_admin_password()
    configured = [bool(username), bool(email), bool(password)]
    with repository.connection() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not any(configured):
        return
    if not all(configured):
        raise RuntimeError("initial administrator username, email, and password must all be configured")
    if user_count:
        return
    admin_id = repository.create_user(
        normalize_email(email),
        "Administrator",
        hash_password(password),
        username=normalize_username(username),
        is_admin=True,
        verified=True,
    )
    repository.audit("bootstrap_admin", "success", target_user_id=admin_id)


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


def build_app():
    env_file = os.environ.get("MANUSCRIPT_DESK_ENV_FILE", "")
    if env_file:
        load_dotenv(env_file, override=False)
    db_path = Path(os.environ.get("MANUSCRIPT_DESK_DB_PATH", "/runtime/manuscripts-multitenant.db"))
    if not db_path.exists():
        if os.environ.get("MANUSCRIPT_DESK_CREATE_EMPTY_DB") != "1":
            raise RuntimeError("multi-tenant database does not exist")
        create_database(db_path)
    migrate_database(db_path)
    otp_key = key_from_env("MANUSCRIPT_DESK_OTP_KEY")
    rate_key = key_from_env("MANUSCRIPT_DESK_RATE_LIMIT_KEY")
    captcha_key = key_from_env("MANUSCRIPT_DESK_CAPTCHA_KEY")
    secret_root = Path(os.environ.get("MANUSCRIPT_DESK_SECRET_ROOT", "/run/manuscript-desk-secrets"))
    public_base_url = os.environ.get("MANUSCRIPT_DESK_PUBLIC_BASE_URL", "http://127.0.0.1:8008")
    repository = Repository(db_path)
    ensure_initial_admin(repository)
    secret_store = SecretStore(secret_root)
    mailer = AccountMailer(repository, secret_store, public_base_url)
    cookie_secure = os.environ.get("MANUSCRIPT_DESK_COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"}
    shortcut_wait = _bounded_float("MANUSCRIPT_DESK_SHORTCUT_WAIT_SECONDS", 110.0, 5.0, 7100.0)
    shortcut_poll = _bounded_float("MANUSCRIPT_DESK_SHORTCUT_POLL_SECONDS", 0.5, 0.1, 5.0)
    shortcut_ttl = int(
        _bounded_float(
            "MANUSCRIPT_DESK_SHORTCUT_CONTINUATION_TTL_SECONDS",
            10800.0,
            shortcut_wait,
            86400.0,
        )
    )
    app = create_app(
        db_path,
        otp_key,
        rate_key,
        captcha_key,
        mailer,
        cookie_secure=cookie_secure,
        secret_cleanup=secret_store.delete,
        secret_store=secret_store,
        public_base_url=public_base_url,
        shortcut_wait_seconds=shortcut_wait,
        shortcut_poll_seconds=shortcut_poll,
        shortcut_continuation_ttl_seconds=shortcut_ttl,
    )
    automation = AutomationRuntime(repository, secret_store, public_base_url)
    app.state.automation_runtime = automation

    @app.on_event("startup")
    async def start_automation_runtime():
        automation.start()

    @app.on_event("shutdown")
    async def stop_automation_runtime():
        automation.stop()

    return app


app = build_app()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()
    if int(os.environ.get("WEB_CONCURRENCY", "1")) != 1:
        raise RuntimeError("embedded scheduler deployment requires exactly one web worker")
    uvicorn.run(app, host=args.host, port=args.port, workers=1, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
