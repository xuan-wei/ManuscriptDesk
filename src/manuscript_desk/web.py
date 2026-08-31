import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import APP_SCHEMA_VERSION
from .account_mail import AccountMailer
from .accounts import AccountService
from .captcha import CaptchaService
from .repository import Repository
from .schema import assert_schema, connect
from .security import (
    csrf_token,
    hash_password,
    normalize_email,
    random_token,
    verify_csrf,
    verify_password,
)
from .webui import install_ui

GENERIC_AUTH_ERROR = "request could not be completed"
SESSION_COOKIE = "session_token"


class EmailRequest(BaseModel):
    email: str
    captcha_token: str = ""
    captcha_answer: str = ""


class CodeRequest(BaseModel):
    email: str
    code: str


class RegisterRequest(CodeRequest):
    username: str
    display_name: str
    password: str


class LoginRequest(BaseModel):
    identifier: str | None = None
    email: str | None = None
    password: str


class PasswordResetRequest(CodeRequest):
    password: str


class EmailChangeRequest(CodeRequest):
    password: str


class ToggleRequest(BaseModel):
    enabled: bool


class InviteRequest(BaseModel):
    email: str
    username: str
    display_name: str = ""


class ActivateRequest(BaseModel):
    token: str
    password: str


def create_app(
    db_path: str | Path,
    otp_key: bytes,
    rate_limit_key: bytes,
    captcha_key: bytes,
    account_mailer: AccountMailer | None = None,
    *,
    cookie_secure: bool = True,
    secret_cleanup: Callable[[str], None] | None = None,
    secret_store: Any | None = None,
    public_base_url: str = "http://127.0.0.1:8008",
    tracking_service: Any | None = None,
    shortcut_wait_seconds: float = 110.0,
    shortcut_poll_seconds: float = 0.5,
    shortcut_continuation_ttl_seconds: int = 10800,
    enable_ui: bool = True,
    overlay_dir: str | Path | None = None,
    legacy_template_dir: str | Path | None = None,
    app_static_dir: str | Path | None = None,
) -> FastAPI:
    repository = Repository(db_path)
    accounts = AccountService(repository, otp_key, rate_limit_key)
    captcha = CaptchaService(captcha_key)
    app = FastAPI(title="Manuscript Desk", version="multitenant-accounts")

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def generic_error(status: int = 400) -> HTTPException:
        return HTTPException(status_code=status, detail=GENERIC_AUTH_ERROR)

    def current_session(request: Request) -> dict:
        raw = request.cookies.get(SESSION_COOKIE, "")
        session = repository.get_active_session(raw) if raw else None
        if not session:
            raise generic_error(401)
        request.state.session_token = raw
        return session

    def current_admin(session: dict = Depends(current_session)) -> dict:
        if not session["is_admin"]:
            raise generic_error(403)
        return session

    def require_csrf(request: Request, session: dict = Depends(current_session)) -> dict:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not verify_csrf(
            request.state.session_token, session["csrf_secret"], request.headers.get("X-CSRF-Token", "")
        ):
            raise generic_error(403)
        return session

    def require_admin_csrf(session: dict = Depends(require_csrf)) -> dict:
        if not session["is_admin"]:
            raise generic_error(403)
        return session

    def send_code(email: str, purpose: str, display_name: str = "") -> None:
        if account_mailer is None:
            raise generic_error(503)
        challenge = accounts.issue_challenge(email, purpose)
        try:
            account_mailer.send_code(email, display_name, challenge.code, purpose)
        except Exception:
            raise generic_error(503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"detail": GENERIC_AUTH_ERROR})

    @app.exception_handler(PermissionError)
    async def permission_error(_request: Request, _exc: PermissionError):
        return JSONResponse(status_code=429, content={"detail": GENERIC_AUTH_ERROR})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/auth"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
                "font-src 'self' https://cdnjs.cloudflare.com data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        if request.url.path.startswith("/api/auth") or request.url.path in {
            "/starred",
            "/update_starred",
            "/renew_starred",
            "/working",
            "/update_working",
        }:
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "private, no-cache"
        return response

    @app.get("/api/health")
    async def health():
        with connect(db_path) as conn:
            assert_schema(conn)
        return {"status": "healthy", "schema": APP_SCHEMA_VERSION}

    @app.get("/api/auth/captcha")
    async def issue_captcha():
        token, _answer, svg = captcha.issue()
        return {"token": token, "svg": svg}

    @app.get("/api/auth/registration")
    async def registration_status():
        return {"enabled": repository.registration_enabled()}

    @app.post("/api/auth/register/send-code")
    async def send_registration_code(data: EmailRequest, request: Request):
        if not repository.registration_enabled():
            raise generic_error(404)
        if not captcha.verify(data.captcha_token, data.captcha_answer):
            raise generic_error()
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("register_send_ip", client_ip(request), 10, 3600)
        accounts.check_rate_limit("register_send_email", email, 5, 3600)
        if not repository.get_user_by_email(email):
            send_code(email, "register")
        return {"ok": True}

    @app.post("/api/auth/register")
    async def register(data: RegisterRequest, request: Request):
        if not repository.registration_enabled():
            raise generic_error(404)
        try:
            email = normalize_email(data.email)
            password_hash = hash_password(data.password)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("register_complete_ip", client_ip(request), 20, 3600)
        if repository.get_user_by_email(email) or not accounts.consume_challenge(email, "register", data.code):
            raise generic_error()
        user_id = repository.create_user(email, data.display_name, password_hash, username=data.username, verified=True)
        repository.audit("register", "success", target_user_id=user_id)
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login(data: LoginRequest, request: Request):
        identifier = str(data.identifier or data.email or "").strip()
        email_subject = identifier.casefold()
        accounts.check_rate_limit("login_ip", client_ip(request), 50, 900)
        accounts.check_rate_limit("login_email", email_subject, 10, 900)
        user = repository.get_user_for_login(identifier)
        check = verify_password(data.password, user["password_hash"], user["password_scheme"]) if user else None
        if not user or not user["is_active"] or not check or not check.valid:
            repository.audit("login", "failure", target_user_id=user["id"] if user else None)
            raise generic_error(401)
        if check.upgraded_hash:
            repository.update_password(user["id"], check.upgraded_hash)
        raw_token, csrf_secret = random_token(), random_token()
        repository.create_session(user["id"], raw_token, csrf_secret)
        repository.audit("login", "success", actor_user_id=user["id"])
        response = JSONResponse({"ok": True, "csrf_token": csrf_token(raw_token, csrf_secret)})
        response.set_cookie(
            SESSION_COOKIE, raw_token, secure=cookie_secure, httponly=True, samesite="lax", path="/", max_age=7 * 86400
        )
        return response

    @app.get("/api/auth/me")
    async def me(session: dict = Depends(current_session)):
        return {
            "id": session["user_id"],
            "username": session["username"],
            "email": session["email"],
            "display_name": session["display_name"],
            "is_admin": bool(session["is_admin"]),
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request, session: dict = Depends(require_csrf)):
        repository.revoke_session(request.state.session_token)
        repository.audit("logout", "success", actor_user_id=session["user_id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return response

    @app.post("/api/auth/password/forgot")
    async def forgot_password(data: EmailRequest, request: Request):
        if not captcha.verify(data.captcha_token, data.captcha_answer):
            raise generic_error()
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("password_forgot_ip", client_ip(request), 10, 3600)
        accounts.check_rate_limit("password_forgot_email", email, 5, 3600)
        user = repository.get_user_by_email(email)
        if user and user["is_active"] and user["password_scheme"] != "activation_pending":
            send_code(email, "password_reset", user["display_name"])
        return {"ok": True}

    @app.post("/api/auth/password/reset")
    async def reset_password(data: PasswordResetRequest, request: Request):
        try:
            email = normalize_email(data.email)
            password_hash = hash_password(data.password)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("password_reset_ip", client_ip(request), 20, 3600)
        user = repository.get_user_by_email(email)
        if not user or not user["is_active"] or not accounts.consume_challenge(email, "password_reset", data.code):
            raise generic_error()
        repository.update_password(user["id"], password_hash)
        repository.revoke_sessions(user["id"])
        repository.audit("password_reset", "success", target_user_id=user["id"])
        return {"ok": True}

    @app.post("/api/account/email/send-code")
    async def send_email_change_code(data: EmailRequest, request: Request, session: dict = Depends(require_csrf)):
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("email_change_user", str(session["user_id"]), 5, 3600)
        if repository.get_user_by_email(email):
            return {"ok": True}
        send_code(email, "email_change", session["display_name"])
        return {"ok": True}

    @app.post("/api/account/email/change")
    async def change_email(data: EmailChangeRequest, request: Request, session: dict = Depends(require_csrf)):
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        user = repository.get_user(session["user_id"])
        check = verify_password(data.password, user["password_hash"], user["password_scheme"])
        if (
            not check.valid
            or repository.get_user_by_email(email)
            or not accounts.consume_challenge(email, "email_change", data.code)
        ):
            raise generic_error()
        with repository.connection() as conn:
            conn.execute(
                "UPDATE users SET email=?,email_verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (email, user["id"]),
            )
        repository.revoke_sessions(user["id"])
        repository.audit("email_change", "success", actor_user_id=user["id"], target_user_id=user["id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return response

    @app.put("/api/admin/registration")
    async def set_registration(data: ToggleRequest, session: dict = Depends(require_admin_csrf)):
        repository.set_registration_enabled(session["user_id"], data.enabled)
        repository.audit(
            "set_registration", "success", actor_user_id=session["user_id"], metadata={"enabled": data.enabled}
        )
        return {"enabled": data.enabled}

    @app.post("/api/admin/email-setup/send-code")
    async def admin_email_send(data: EmailRequest, session: dict = Depends(require_admin_csrf)):
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        accounts.check_rate_limit("admin_email_setup", str(session["user_id"]), 5, 3600)
        existing = repository.get_user_by_email(email)
        if existing and existing["id"] != session["user_id"]:
            return {"ok": True}
        send_code(email, "admin_email_setup", session["display_name"])
        return {"ok": True}

    @app.post("/api/admin/email-setup/complete")
    async def admin_email_complete(data: CodeRequest, session: dict = Depends(require_admin_csrf)):
        try:
            email = normalize_email(data.email)
        except ValueError:
            raise generic_error()
        existing = repository.get_user_by_email(email)
        if (existing and existing["id"] != session["user_id"]) or not accounts.consume_challenge(
            email, "admin_email_setup", data.code
        ):
            raise generic_error()
        with repository.connection() as conn:
            conn.execute(
                "UPDATE users SET email=?,email_verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (email, session["user_id"]),
            )
        repository.audit(
            "admin_email_setup", "success", actor_user_id=session["user_id"], target_user_id=session["user_id"]
        )
        return {"ok": True}

    @app.post("/api/admin/invitations")
    async def create_invitation(data: InviteRequest, session: dict = Depends(require_admin_csrf)):
        if account_mailer is None:
            raise generic_error(503)
        user_id = None
        try:
            user_id, token = accounts.create_invitation(
                session["user_id"], data.email, data.display_name, data.username
            )
            account_mailer.send_invitation(normalize_email(data.email), data.display_name, token)
        except (ValueError, PermissionError):
            raise generic_error()
        except Exception:
            # Do not leave an unusable pending account if delivery failed.
            if user_id is not None:
                with repository.connection() as conn:
                    conn.execute("DELETE FROM users WHERE id=? AND password_scheme='activation_pending'", (user_id,))
            raise generic_error(503)
        return {"ok": True}

    @app.post("/api/auth/invitations/activate")
    async def activate_invitation(data: ActivateRequest):
        try:
            password_hash = hash_password(data.password)
        except ValueError:
            raise generic_error()
        user_id = accounts.activate_invitation(data.token, password_hash)
        if not user_id:
            raise generic_error()
        repository.audit("activate_invitation", "success", target_user_id=user_id)
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/disable")
    async def disable_user(user_id: int, session: dict = Depends(require_admin_csrf)):
        try:
            purge_at = accounts.disable_user(session["user_id"], user_id)
        except (LookupError, ValueError, PermissionError):
            raise generic_error()
        return {"ok": True, "purge_eligible_at": purge_at}

    @app.post("/api/admin/users/{user_id}/reactivate")
    async def reactivate_user(user_id: int, session: dict = Depends(require_admin_csrf)):
        try:
            accounts.reactivate_user(session["user_id"], user_id)
        except (LookupError, ValueError, PermissionError):
            raise generic_error()
        return {"ok": True}

    @app.get("/api/admin/purge-preview")
    async def purge_preview(session: dict = Depends(current_admin)):
        return {"accounts": accounts.purge_preview(session["user_id"])}

    @app.post("/api/admin/users/{user_id}/purge")
    async def purge_user(user_id: int, session: dict = Depends(require_admin_csrf)):
        try:
            result = accounts.purge_user(session["user_id"], user_id, secret_cleanup=secret_cleanup)
        except (ValueError, PermissionError):
            raise generic_error()
        return {"ok": True, "account_hash": result["account_hash"]}

    if enable_ui:
        package_root = Path(__file__).resolve().parent
        resolved_overlay = Path(overlay_dir or os.environ.get("MANUSCRIPT_DESK_OVERLAY_DIR") or package_root)
        resolved_legacy = Path(
            legacy_template_dir or os.environ.get("MANUSCRIPT_DESK_LEGACY_TEMPLATES") or package_root / "templates"
        )
        resolved_static = Path(
            app_static_dir or os.environ.get("MANUSCRIPT_DESK_APP_STATIC") or package_root / "static"
        )
        if not (resolved_overlay / "templates").is_dir():
            raise RuntimeError("packaged UI templates are missing")
        install_ui(
            app,
            repository=repository,
            accounts=accounts,
            captcha=captcha,
            account_mailer=account_mailer,
            secret_store=secret_store,
            cookie_secure=cookie_secure,
            public_base_url=public_base_url,
            overlay_dir=resolved_overlay,
            legacy_template_dir=resolved_legacy,
            app_static_dir=resolved_static,
            tracking_service=tracking_service,
            shortcut_continuation_key=rate_limit_key,
            shortcut_wait_seconds=shortcut_wait_seconds,
            shortcut_poll_seconds=shortcut_poll_seconds,
            shortcut_continuation_ttl_seconds=shortcut_continuation_ttl_seconds,
        )

    return app
