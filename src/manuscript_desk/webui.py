"""HTML/UI and owner-scoped application routes for the multi-tenant Manuscript Desk.

This module turns the JSON account/auth core in :mod:`manuscript_desk.web` into a
full, runnable FastAPI/Jinja application:

* mounts the overlay + legacy Jinja templates and static assets,
* serves session-based HTML pages (login/register/recovery/profile/admin/config
  /automation/manuscripts) with CSRF-protected forms,
* wires tenant-scoped manuscript, journal-config and automation APIs so every
  read and write is bound to the authenticated owner,
* exposes a manual update enqueue/status surface (a real queue worker runs
  separately), and
* authenticates the public scoped-token API while failing closed on the legacy
  Shortcut "renew" webview that requires a live scraper worker.

Nothing here can be reached without an authenticated session except the auth
pages themselves and the scoped-token public API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from .accounts import AccountService, InvitationConflict
from .captcha import CaptchaService
from .provider_origins import get_provider_origin, normalize_provider_url
from .repository import Repository
from .schema import assert_schema
from .security import (
    csrf_token,
    hash_password,
    normalize_email,
    normalize_username,
    random_token,
    token_hash,
    verify_csrf,
    verify_password,
)
from .timezones import DEFAULT_TIMEZONE, profile_timezone_choices, validate_timezone

SESSION_COOKIE = "session_token"
ANON_CSRF_COOKIE = "anon_csrf"
PENDING_EMAIL_COOKIE = "pending_email_change"
SYSTEM_SECRET_UUID = "0"  # SecretStore accepts hex/dash identifiers; system scope uses "0".
TOKEN_SCOPES = {"starred:read", "starred:update", "working:read", "working:update"}


def normalize_shortcut_base_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("invalid shortcut base URL")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid shortcut base URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid shortcut base URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("invalid shortcut base URL")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid shortcut base URL")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_UPDATE_MESSAGES = {
    "no_enabled_journals": "尚未启用期刊配置，请先在期刊配置页面添加或启用期刊。",
    "browser_start_failed": "自动化浏览器无法启动，请联系管理员检查运行环境。",
    "browser_timeout": "期刊页面响应超时，请稍后重试。",
    "credentials_unavailable": "该期刊的登录凭据不可用，请检查期刊配置。",
    "provider_login_failed": "无法登录期刊系统，请检查账号、密码或期刊登录状态。",
    "provider_unavailable": "期刊系统当前不可用，请稍后重试。",
    "unsafe_provider_redirect": "期刊页面跳转到了不受信任的地址，本次更新已停止。",
    "empty_result_anomaly": "期刊返回了异常的空结果，原有稿件数据已保留。",
    "tracking_failed": "期刊数据更新失败，请稍后重试或联系管理员。",
    "automation_failed": "后台更新任务失败，请稍后重试或联系管理员。",
}


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _update_status_view(request_item: dict | None, last_db_update: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    if not request_item:
        return {
            "active": False,
            "phase": "idle",
            "outcome": None,
            "request_id": None,
            "started_at": None,
            "finished_at": None,
            "server_time": now.isoformat(),
            "elapsed_seconds": 0,
            "total_manuscripts": 0,
            "attempted_journals": 0,
            "successful_journals": 0,
            "failed_journals": 0,
            "error_code": None,
            "message": "",
            "last_db_update": last_db_update,
        }
    status = str(request_item.get("status") or "")
    summary = request_item.get("summary") or {}
    result = str(request_item.get("result") or summary.get("result") or "") or None
    active = status in {"queued", "claimed", "running"}
    phase = status if active or status in {"failed", "cancelled"} else "finished"
    if status == "finished" and result == "failed":
        phase = "failed"
    error_code = str(summary.get("error") or "").strip() or None
    if error_code not in _UPDATE_MESSAGES:
        error_code = "tracking_failed" if phase == "failed" else None
    started = _parse_utc(request_item.get("started_at"))
    finished = _parse_utc(request_item.get("finished_at"))
    base = started or _parse_utc(request_item.get("updated_at")) or _parse_utc(request_item.get("created_at"))
    end = finished or now
    elapsed = max(0, int((end - base).total_seconds())) if base else 0
    if phase == "queued":
        message = "更新已排队，等待后台任务处理。"
    elif phase == "claimed":
        message = "后台任务已领取，正在启动浏览器。"
    elif phase == "running":
        message = "正在抓取期刊数据。"
    elif phase == "cancelled":
        message = "更新已取消。"
    elif phase == "failed":
        message = _UPDATE_MESSAGES.get(error_code, _UPDATE_MESSAGES["tracking_failed"])
    elif result == "success":
        message = f"更新完成，找到 {int(summary.get('total_manuscripts') or 0)} 篇稿件。"
    elif result == "partial":
        message = "更新部分完成。"
    elif result == "no_changes":
        message = "更新完成，未发现新的稿件数据。"
    else:
        message = "更新已结束。"
    return {
        "active": active,
        "phase": phase,
        "outcome": result,
        "request_id": request_item.get("id"),
        "started_at": request_item.get("started_at"),
        "finished_at": request_item.get("finished_at"),
        "server_time": now.isoformat(),
        "elapsed_seconds": elapsed,
        "total_manuscripts": int(summary.get("total_manuscripts") or 0),
        "attempted_journals": int(summary.get("attempted_journals") or 0),
        "successful_journals": int(summary.get("successful_journals") or 0),
        "failed_journals": int(summary.get("failed_journals") or 0),
        "error_code": error_code,
        "message": message,
        "last_db_update": last_db_update,
    }


def _time_ago(value: str | None) -> str:
    """Format a stored manuscript date like the legacy mobile Shortcut view."""
    if not value:
        return ""
    text = str(value).strip()
    parsed = None
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            break
        except ValueError:
            continue
    if parsed is None:
        for fmt in (
            "%d-%b-%Y",
            "%d-%b-%y",
            "%Y-%m-%d",
            "%b %d, %Y",
            "%B %d, %Y",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    days = max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()) // 86400)
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


# ---------------------------------------------------------------------------
# Cron preview (used by the automation editor). Standard 5-field cron, evaluated
# in the job timezone. Day-of-month and day-of-week are ANDed which is adequate
# for a "next N runs" preview.
# ---------------------------------------------------------------------------


def _cron_field(expr: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError("invalid cron step")
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        for value in range(start, end + 1, step):
            if low <= value <= high:
                values.add(value)
    if not values:
        raise ValueError("empty cron field")
    return values


def cron_next_runs(expression: str, tz_name: str, count: int = 5) -> list[str]:
    fields = str(expression).split()
    if len(fields) != 5:
        raise ValueError("cron expression must have 5 fields")
    minutes = _cron_field(fields[0], 0, 59)
    hours = _cron_field(fields[1], 0, 23)
    doms = _cron_field(fields[2], 1, 31)
    months = _cron_field(fields[3], 1, 12)
    dows = _cron_field(fields[4], 0, 6)
    timezone_name = validate_timezone(tz_name)
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(timezone_name)
    cursor = datetime.now(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    runs: list[str] = []
    guard = 0
    while len(runs) < count and guard < 527040:  # ~1 year of minutes
        guard += 1
        if (
            cursor.minute in minutes
            and cursor.hour in hours
            and cursor.month in months
            and cursor.day in doms
            and (cursor.weekday() + 1) % 7 in dows
        ):
            runs.append(cursor.strftime("%Y-%m-%d %H:%M"))
        cursor += timedelta(minutes=1)
    return runs


def _is_nav_active(request: Request, target: str) -> bool:
    path = request.url.path
    view = request.query_params.get("view", "")
    if target == "manuscripts":
        return path == "/" and view not in {"starred", "ongoing", "hidden"}
    if target == "starred":
        return path == "/" and view == "starred"
    if target == "ongoing":
        return path == "/" and view == "ongoing"
    if target == "hidden":
        return path == "/" and view == "hidden"
    return path == target


def install_ui(
    app: FastAPI,
    *,
    repository: Repository,
    accounts: AccountService,
    captcha: CaptchaService,
    account_mailer: Any | None,
    secret_store: Any | None,
    cookie_secure: bool,
    public_base_url: str,
    overlay_dir: Path,
    legacy_template_dir: Path,
    app_static_dir: Path | None,
    tracking_service: Any | None = None,
    shortcut_continuation_key: bytes | None = None,
    shortcut_wait_seconds: float = 110.0,
    shortcut_poll_seconds: float = 0.5,
    shortcut_continuation_ttl_seconds: int = 10800,
) -> None:
    overlay_templates = overlay_dir / "templates"
    overlay_static = overlay_dir / "static"
    ui_static = overlay_static / "ui"

    templates = Jinja2Templates(directory=str(overlay_templates))
    loaders = [FileSystemLoader(str(overlay_templates))]
    if legacy_template_dir.exists():
        loaders.append(FileSystemLoader(str(legacy_template_dir)))
    templates.env.loader = ChoiceLoader(loaders)
    templates.env.globals["is_nav_active"] = _is_nav_active
    templates.env.globals["app_name"] = "Manuscript Desk"

    # Static mounts. Overlay UI + automation assets; legacy /static kept for the
    # image's bundled assets when present.
    if ui_static.exists():
        app.mount("/static/ui", StaticFiles(directory=str(ui_static)), name="ui-static")
    if overlay_static.exists():
        app.mount("/static/automation", StaticFiles(directory=str(overlay_static)), name="automation-static")
    if app_static_dir and app_static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(app_static_dir)), name="legacy-static")

    # ------------------------------------------------------------------ helpers
    def get_session(request: Request) -> dict | None:
        raw = request.cookies.get(SESSION_COOKIE, "")
        session = repository.get_active_session(raw) if raw else None
        if session:
            request.state.session_token = raw
        return session

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def session_csrf(request: Request, session: dict) -> str:
        return csrf_token(request.state.session_token, session["csrf_secret"])

    def form_csrf_ok(request: Request, session: dict | None, form) -> bool:
        presented = str(form.get("csrf_token") or "")
        if session:
            return verify_csrf(request.state.session_token, session["csrf_secret"], presented)
        cookie = request.cookies.get(ANON_CSRF_COOKIE, "")
        return bool(cookie) and bool(presented) and hmac.compare_digest(cookie, presented)

    def header_csrf_ok(request: Request, session: dict) -> bool:
        presented = request.headers.get("X-CSRF-Token") or request.headers.get("X-Automation-CSRF") or ""
        return verify_csrf(request.state.session_token, session["csrf_secret"], presented)

    def redirect(location: str, status: int = 303) -> RedirectResponse:
        return RedirectResponse(location, status_code=status)

    def api_guard(request: Request, *, write: bool = False):
        """Return (session, error_response). error_response is None when allowed."""
        session = get_session(request)
        if not session:
            return None, JSONResponse({"detail": "authentication required"}, status_code=401)
        if write and not header_csrf_ok(request, session):
            return None, JSONResponse({"detail": "invalid csrf token"}, status_code=403)
        return session, None

    def render(template: str, request: Request, session: dict, *, status_code: int = 200, **ctx) -> HTMLResponse:
        user = repository.get_user(session["user_id"]) or {}
        context = {
            "request": request,
            "current_user": user,
            "csrf_token": session_csrf(request, session),
        }
        context.update(ctx)
        return templates.TemplateResponse(template, context, status_code=status_code)

    def render_anon(template: str, request: Request, *, http_status: int = 200, **ctx) -> HTMLResponse:
        token = ctx.pop("csrf_token", None) or random_token()
        context = {"request": request, "current_user": None, "csrf_token": token}
        context.update(ctx)
        response = templates.TemplateResponse(template, context, status_code=http_status)
        response.set_cookie(
            ANON_CSRF_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            path="/",
            max_age=3600,
        )
        return response

    def set_session_cookie(response, raw_token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            raw_token,
            secure=cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=7 * 86400,
        )

    def start_session(user_id: int, response) -> None:
        raw_token, csrf_secret = random_token(), random_token()
        repository.create_session(user_id, raw_token, csrf_secret)
        set_session_cookie(response, raw_token)

    def changed_ids_for(owner: int, manuscripts: list[dict]) -> set[str]:
        with repository.connection() as conn:
            acks = {
                row["original_id"]: row
                for row in conn.execute(
                    "SELECT original_id,ack_status,ack_detailed_status FROM manuscript_acks WHERE owner_user_id=?",
                    (owner,),
                )
            }
        changed = set()
        for item in manuscripts:
            ack = acks.get(item["original_id"])
            if (
                not ack
                or ack["ack_status"] != item.get("status", "")
                or ack["ack_detailed_status"] != item.get("detailed_status", "")
            ):
                changed.add(item["original_id"])
        return changed

    def validate_provider_url(provider: str, journal_url: str) -> str:
        return normalize_provider_url(provider, journal_url)

    # ===================================================================
    # Authentication pages
    # ===================================================================
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if get_session(request):
            return redirect("/")
        return render_anon(
            "login.html",
            request,
            registration_enabled=repository.registration_enabled(),
        )

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        if not form_csrf_ok(request, None, form):
            return render_anon(
                "login.html",
                request,
                http_status=403,
                error="会话已过期，请重试。",
                registration_enabled=repository.registration_enabled(),
            )
        identifier = str(form.get("identifier") or form.get("email") or "").strip()
        password = str(form.get("password") or "")
        try:
            accounts.check_rate_limit("login_ip", client_ip(request), 50, 900)
            accounts.check_rate_limit("login_email", identifier.casefold(), 10, 900)
        except PermissionError:
            return render_anon(
                "login.html",
                request,
                http_status=429,
                error="尝试过于频繁，请稍后再试。",
                identifier=identifier,
                registration_enabled=repository.registration_enabled(),
            )
        user = repository.get_user_for_login(identifier)
        check = verify_password(password, user["password_hash"], user["password_scheme"]) if user else None
        if not user or not user["is_active"] or not check or not check.valid:
            repository.audit("login", "failure", target_user_id=user["id"] if user else None)
            return render_anon(
                "login.html",
                request,
                http_status=401,
                error="用户名、邮箱或密码错误。",
                identifier=identifier,
                registration_enabled=repository.registration_enabled(),
            )
        if check.upgraded_hash:
            repository.update_password(user["id"], check.upgraded_hash)
        response = redirect("/admin/users" if user["is_admin"] else "/")
        start_session(user["id"], response)
        repository.audit("login", "success", actor_user_id=user["id"])
        response.delete_cookie(ANON_CSRF_COOKIE, path="/")
        return response

    @app.post("/logout")
    async def logout_submit(request: Request):
        session = get_session(request)
        response = redirect("/login")
        if session:
            form = await request.form()
            if form_csrf_ok(request, session, form):
                repository.revoke_session(request.state.session_token)
                repository.audit("logout", "success", actor_user_id=session["user_id"])
        response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return response

    @app.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request):
        if not repository.registration_enabled():
            return redirect("/login")
        token, _answer, svg = captcha.issue()
        return render_anon("register.html", request, captcha_token=token, captcha_svg=svg)

    @app.post("/register")
    async def register_submit(request: Request):
        form = await request.form()
        token, _answer, svg = captcha.issue()
        rerender = lambda **kw: render_anon(
            "register.html", request, captcha_token=token, captcha_svg=svg, form_data=dict(form), **kw
        )
        if not repository.registration_enabled():
            return redirect("/login")
        if not form_csrf_ok(request, None, form):
            return rerender(status=403, error="会话已过期，请重试。")
        password = str(form.get("password") or "")
        if password != str(form.get("confirm_password") or ""):
            return rerender(status=400, error="两次输入的密码不一致。")
        try:
            email = normalize_email(str(form.get("email") or ""))
            username = normalize_username(str(form.get("username") or ""))
            password_hash = hash_password(password)
        except ValueError:
            return rerender(status=400, error="请输入有效的邮箱；密码至少 8 位，并且至少包含一个字母和一个数字。")
        try:
            accounts.check_rate_limit("register_complete_ip", client_ip(request), 20, 3600)
        except PermissionError:
            return rerender(status=429, error="尝试过于频繁，请稍后再试。")
        code = str(form.get("code") or "")
        if repository.get_user_by_email(email) or not accounts.consume_challenge(email, "register", code):
            return rerender(status=400, error="验证码无效或已过期，请重新获取。")
        user_id = repository.create_user(
            email, str(form.get("display_name") or ""), password_hash, username=username, verified=True
        )
        repository.audit("register", "success", target_user_id=user_id)
        response = render_anon(
            "login.html",
            request,
            success="账号已创建，请登录。",
            registration_enabled=repository.registration_enabled(),
        )
        return response

    @app.get("/forgot-password", response_class=HTMLResponse)
    async def forgot_page(request: Request):
        return render_anon("forgot_password.html", request)

    @app.post("/forgot-password")
    async def forgot_submit(request: Request):
        form = await request.form()
        if not form_csrf_ok(request, None, form):
            return render_anon("forgot_password.html", request, http_status=403, error="会话已过期，请重试。")
        try:
            email = normalize_email(str(form.get("email") or ""))
        except ValueError:
            return render_anon("forgot_password.html", request, submitted=True, email="")
        try:
            accounts.check_rate_limit("password_forgot_ip", client_ip(request), 10, 3600)
            accounts.check_rate_limit("password_forgot_email", email, 5, 3600)
        except PermissionError:
            return render_anon("forgot_password.html", request, submitted=True, email=email)
        user = repository.get_user_by_email(email)
        if user and user["is_active"] and account_mailer is not None:
            try:
                challenge = accounts.issue_challenge(email, "password_reset")
                account_mailer.send_code(email, user["display_name"], challenge.code, "password_reset")
            except Exception:
                pass
        return render_anon("forgot_password.html", request, submitted=True, email=email)

    @app.get("/reset-password", response_class=HTMLResponse)
    async def reset_page(request: Request):
        return render_anon("reset_password.html", request, email=request.query_params.get("email", ""))

    @app.post("/reset-password")
    async def reset_submit(request: Request):
        form = await request.form()
        email_raw = str(form.get("email") or "")
        if not form_csrf_ok(request, None, form):
            return render_anon(
                "reset_password.html", request, http_status=403, error="会话已过期，请重试。", email=email_raw
            )
        password = str(form.get("password") or "")
        if password != str(form.get("confirm_password") or ""):
            return render_anon(
                "reset_password.html", request, http_status=400, error="两次输入的密码不一致。", email=email_raw
            )
        try:
            email = normalize_email(email_raw)
            password_hash = hash_password(password)
        except ValueError:
            return render_anon(
                "reset_password.html", request, http_status=400, error="请输入有效的邮箱和密码。", email=email_raw
            )
        try:
            accounts.check_rate_limit("password_reset_ip", client_ip(request), 20, 3600)
        except PermissionError:
            return render_anon(
                "reset_password.html", request, http_status=429, error="尝试过于频繁，请稍后再试。", email=email_raw
            )
        user = repository.get_user_by_email(email)
        code = str(form.get("code") or "")
        if not user or not user["is_active"] or not accounts.consume_challenge(email, "password_reset", code):
            return render_anon(
                "reset_password.html", request, http_status=400, error="验证码无效或已过期。", email=email_raw
            )
        repository.update_password(user["id"], password_hash)
        repository.revoke_sessions(user["id"])
        repository.audit("password_reset", "success", target_user_id=user["id"])
        return render_anon(
            "login.html",
            request,
            success="密码已更新，请重新登录。",
            registration_enabled=repository.registration_enabled(),
        )

    @app.get("/accept-invite", response_class=HTMLResponse)
    async def accept_invite_bridge(request: Request):
        # The invitation email carries the token in the URL fragment (never sent to
        # the server); bridge it to the path-based activation form client-side.
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>接受邀请</title>"
            "<script>var t=(location.hash.match(/token=([^&]+)/)||[])[1];"
            "location.replace(t?'/invite/'+encodeURIComponent(t):'/login');</script>"
            "<noscript>请在支持 JavaScript 的浏览器中打开邀请链接。</noscript>"
        )

    @app.get("/invite/{token}", response_class=HTMLResponse)
    async def invite_page(request: Request, token: str):
        return render_anon("invitation_accept.html", request, token=token)

    @app.post("/invite/{token}")
    async def invite_submit(request: Request, token: str):
        form = await request.form()
        if not form_csrf_ok(request, None, form):
            return render_anon(
                "invitation_accept.html", request, http_status=403, token=token, error="会话已过期，请重试。"
            )
        password = str(form.get("password") or "")
        if password != str(form.get("confirm_password") or ""):
            return render_anon(
                "invitation_accept.html", request, http_status=400, token=token, error="两次输入的密码不一致。"
            )
        try:
            password_hash = hash_password(password)
        except ValueError:
            return render_anon(
                "invitation_accept.html",
                request,
                http_status=400,
                token=token,
                error="密码至少 8 位，并且至少包含一个字母和一个数字。",
            )
        user_id = accounts.activate_invitation(token, password_hash)
        if not user_id:
            return render_anon("invitation_accept.html", request, http_status=400, token=token, invitation_valid=False)
        repository.audit("activate_invitation", "success", target_user_id=user_id)
        response = redirect("/")
        start_session(user_id, response)
        return response

    @app.get("/verify-email", response_class=HTMLResponse)
    async def verify_email_page(request: Request):
        # Email verification for this build happens inline via one-time codes during
        # registration/change; a standalone link is informational only.
        return render_anon(
            "email_verification.html", request, status="already_verified", message="邮箱已在注册或变更流程中完成验证。"
        )

    @app.get("/email-change/confirm", response_class=HTMLResponse)
    async def email_change_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        new_email = request.cookies.get(PENDING_EMAIL_COOKIE, "")
        return render("email_change.html", request, session, new_email=new_email)

    @app.post("/email-change/confirm")
    async def email_change_submit(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        form = await request.form()
        new_email = request.cookies.get(PENDING_EMAIL_COOKIE, "")
        if not form_csrf_ok(request, session, form):
            return render(
                "email_change.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                new_email=new_email,
            )
        try:
            email = normalize_email(new_email)
        except ValueError:
            return render("email_change.html", request, session, error="没有待确认的邮箱变更。", new_email=new_email)
        code = str(form.get("code") or "")
        if repository.get_user_by_email(email) or not accounts.consume_challenge(email, "email_change", code):
            return render("email_change.html", request, session, error="验证码无效或已过期。", new_email=new_email)
        with repository.connection() as conn:
            conn.execute(
                "UPDATE users SET email=?,email_verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (email, session["user_id"]),
            )
        repository.revoke_sessions(session["user_id"])
        repository.audit("email_change", "success", actor_user_id=session["user_id"], target_user_id=session["user_id"])
        response = templates.TemplateResponse(
            "email_change.html", {"request": request, "current_user": None, "csrf_token": "", "completed": True}
        )
        response.delete_cookie(SESSION_COOKIE, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        response.delete_cookie(PENDING_EMAIL_COOKIE, path="/")
        return response

    # ===================================================================
    # Profile (tenant self-service)
    # ===================================================================
    def profile_context(session: dict) -> dict:
        selected = repository.get_user(session["user_id"]).get("timezone") or DEFAULT_TIMEZONE
        choices = list(profile_timezone_choices())
        choice_values = {value for value, _ in choices}
        if selected not in choice_values:
            choices.append((selected, f"{selected}（当前已保存）"))
        return {"timezone_choices": choices}

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        return render("profile.html", request, session, **profile_context(session))

    @app.post("/profile")
    async def profile_update(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "profile.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **profile_context(session),
            )
        display_name = str(form.get("display_name") or "").strip()[:100]
        try:
            tz = validate_timezone(form.get("timezone") or DEFAULT_TIMEZONE)
        except ValueError:
            return render(
                "profile.html",
                request,
                session,
                status_code=400,
                error="请选择有效的 IANA 时区。",
                profile_display_name=display_name,
                profile_timezone=str(form.get("timezone") or ""),
                **profile_context(session),
            )
        with repository.connection() as conn:
            conn.execute(
                "UPDATE users SET display_name=?,timezone=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (display_name, tz, session["user_id"]),
            )
        return render("profile.html", request, session, success="资料已保存。", **profile_context(session))

    @app.post("/profile/password")
    async def profile_password(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "profile.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **profile_context(session),
            )
        user = repository.get_user(session["user_id"])
        check = verify_password(str(form.get("current_password") or ""), user["password_hash"], user["password_scheme"])
        if not check.valid:
            return render("profile.html", request, session, error="当前密码不正确。", **profile_context(session))
        new_password = str(form.get("new_password") or "")
        if new_password != str(form.get("confirm_password") or ""):
            return render(
                "profile.html", request, session, error="两次输入的新密码不一致。", **profile_context(session)
            )
        try:
            new_hash = hash_password(new_password)
        except ValueError:
            return render(
                "profile.html",
                request,
                session,
                error="新密码至少 8 位，并且至少包含一个字母和一个数字。",
                **profile_context(session),
            )
        repository.update_password(session["user_id"], new_hash)
        if form.get("revoke_other_sessions"):
            with repository.connection() as conn:
                conn.execute(
                    "UPDATE user_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND token_hash<>? AND revoked_at IS NULL",
                    (session["user_id"], token_hash(request.state.session_token)),
                )
        repository.audit("password_change", "success", actor_user_id=session["user_id"])
        return render("profile.html", request, session, success="密码已更新。", **profile_context(session))

    @app.post("/profile/email/send-code")
    async def profile_email_send(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "profile.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **profile_context(session),
            )
        if account_mailer is None:
            return render(
                "profile.html", request, session, error="系统邮件未配置，暂时无法变更邮箱。", **profile_context(session)
            )
        user = repository.get_user(session["user_id"])
        check = verify_password(str(form.get("current_password") or ""), user["password_hash"], user["password_scheme"])
        if not check.valid:
            return render("profile.html", request, session, error="当前密码不正确。", **profile_context(session))
        try:
            email = normalize_email(str(form.get("new_email") or ""))
        except ValueError:
            return render("profile.html", request, session, error="请输入有效的新邮箱。", **profile_context(session))
        try:
            accounts.check_rate_limit("email_change_user", str(session["user_id"]), 5, 3600)
        except PermissionError:
            return render(
                "profile.html", request, session, error="尝试过于频繁，请稍后再试。", **profile_context(session)
            )
        if not repository.get_user_by_email(email):
            try:
                challenge = accounts.issue_challenge(email, "email_change")
                account_mailer.send_code(email, user["display_name"], challenge.code, "email_change")
            except Exception:
                return render(
                    "profile.html", request, session, error="验证码发送失败，请稍后再试。", **profile_context(session)
                )
        response = redirect("/email-change/confirm")
        response.set_cookie(
            PENDING_EMAIL_COOKIE, email, httponly=True, samesite="lax", secure=cookie_secure, path="/", max_age=900
        )
        return response

    @app.post("/profile/email/resend")
    async def profile_email_resend(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        return render("profile.html", request, session, success="邮箱已在注册流程中验证。", **profile_context(session))

    def shortcuts_context(session: dict) -> dict:
        settings = repository.get_shortcut_settings(session["user_id"])
        base_url = str((settings or {}).get("base_url") or "")
        with repository.connection() as conn:
            tokens = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,label,token_prefix,secret_ref,scopes,created_at,last_used_at,expires_at "
                    "FROM api_tokens WHERE owner_user_id=? AND revoked_at IS NULL ORDER BY id DESC",
                    (session["user_id"],),
                )
            ]
        for token in tokens:
            scopes = set(str(token.get("scopes") or "").split())
            raw = ""
            if token.get("secret_ref") and secret_store is not None:
                try:
                    raw = secret_store.read(token["secret_ref"])
                except (OSError, ValueError, PermissionError):
                    raw = ""
            token["has_working_scopes"] = {"working:read", "working:update"} <= scopes
            token["starred_url"] = (
                f"{base_url}/starred?api={raw}" if raw and base_url and "starred:read" in scopes else ""
            )
            token["update_starred_url"] = (
                f"{base_url}/update_starred?api={raw}" if raw and base_url and "starred:update" in scopes else ""
            )
            token["working_url"] = (
                f"{base_url}/working?api={raw}" if raw and base_url and "working:read" in scopes else ""
            )
            token["update_working_url"] = (
                f"{base_url}/update_working?api={raw}" if raw and base_url and "working:update" in scopes else ""
            )
        return {"api_tokens": tokens, "shortcut_base_url": base_url}

    @app.get("/shortcuts", response_class=HTMLResponse)
    async def shortcuts_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        return render("shortcuts.html", request, session, **shortcuts_context(session))

    @app.post("/shortcuts/base-url")
    async def shortcuts_save_base_url(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "shortcuts.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **shortcuts_context(session),
            )
        try:
            base_url = normalize_shortcut_base_url(form.get("base_url"))
        except ValueError:
            context = shortcuts_context(session)
            context["shortcut_base_url"] = str(form.get("base_url") or "")
            return render(
                "shortcuts.html",
                request,
                session,
                status_code=400,
                error="请输入有效的 http 或 https 网站地址，不能包含账号密码、查询参数或片段。",
                **context,
            )
        repository.save_shortcut_base_url(session["user_id"], base_url)
        repository.audit(
            "shortcut_base_url_update",
            "success",
            actor_user_id=session["user_id"],
            metadata={"scheme": urlsplit(base_url).scheme},
        )
        return render("shortcuts.html", request, session, success="网站地址已保存。", **shortcuts_context(session))

    async def create_shortcut_token(request: Request, *, legacy_profile: bool = False):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        form = await request.form()
        template = "profile.html" if legacy_profile else "shortcuts.html"
        context = profile_context(session) if legacy_profile else shortcuts_context(session)
        if not form_csrf_ok(request, session, form):
            return render(template, request, session, status_code=403, error="会话已过期，请重试。", **context)
        if not legacy_profile and not repository.get_shortcut_settings(session["user_id"]):
            return render(template, request, session, status_code=400, error="请先保存网站地址。", **context)
        label = str(form.get("label") or "").strip()[:100] or "Shortcut Token"
        scopes = ["starred:read", "starred:update", "working:read", "working:update"]
        expires_raw = str(form.get("expires_at") or "").strip()
        expires_at = f"{expires_raw}T23:59:59+00:00" if expires_raw else None
        if secret_store is None:
            return render(template, request, session, error="密钥存储不可用，无法创建快捷指令 Token。", **context)
        raw_token = random_token()
        secret_ref = secret_store.write(str(session["user_id"]), "tokens", raw_token)
        try:
            repository.create_api_token(session["user_id"], raw_token, label, scopes, expires_at, secret_ref=secret_ref)
        except Exception:
            secret_store.delete(secret_ref)
            raise
        repository.audit(
            "api_token_create", "success", actor_user_id=session["user_id"], metadata={"scope_count": len(scopes)}
        )
        target_context = profile_context(session) if legacy_profile else shortcuts_context(session)
        return render(template, request, session, raw_token=raw_token, success="Token 已创建。", **target_context)

    async def revoke_shortcut_token(request: Request, token_id: int, *, legacy_profile: bool = False):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        form = await request.form()
        template = "profile.html" if legacy_profile else "shortcuts.html"
        context = profile_context(session) if legacy_profile else shortcuts_context(session)
        if not form_csrf_ok(request, session, form):
            return render(template, request, session, status_code=403, error="会话已过期，请重试。", **context)
        secret_ref = repository.revoke_api_token(session["user_id"], token_id)
        if secret_ref and secret_store is not None:
            secret_store.delete(secret_ref)
        return render(
            template,
            request,
            session,
            success="Token 已撤销。",
            **(profile_context(session) if legacy_profile else shortcuts_context(session)),
        )

    @app.post("/shortcuts/tokens")
    async def shortcuts_create_token(request: Request):
        return await create_shortcut_token(request)

    @app.post("/shortcuts/tokens/{token_id}/revoke")
    async def shortcuts_revoke_token(request: Request, token_id: int):
        return await revoke_shortcut_token(request, token_id)

    @app.post("/profile/tokens", include_in_schema=False)
    async def profile_create_token(request: Request):
        return await create_shortcut_token(request, legacy_profile=True)

    @app.post("/profile/tokens/{token_id}/revoke", include_in_schema=False)
    async def profile_revoke_token(request: Request, token_id: int):
        return await revoke_shortcut_token(request, token_id, legacy_profile=True)

    # ===================================================================
    # Admin control plane
    # ===================================================================
    def require_admin_page(request: Request):
        session = get_session(request)
        if not session:
            return None, redirect("/login")
        if not session["is_admin"]:
            return None, redirect("/")
        return session, None

    def admin_users_context() -> dict:
        now = _now_iso()
        with repository.connection() as conn:
            users = []
            for row in conn.execute("SELECT * FROM users ORDER BY id"):
                item = dict(row)
                item["purge_eligible"] = bool(item.get("purge_eligible_at") and item["purge_eligible_at"] <= now)
                users.append(item)
        return {"users": users}

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users_page(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        return render("admin_users.html", request, session, **admin_users_context())

    @app.post("/admin/users/invite")
    async def admin_invite(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_users.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_users_context(),
            )
        email = str(form.get("email") or "")
        display_name = str(form.get("display_name") or "")
        username = str(form.get("username") or "")
        try:
            invitation = accounts.create_invitation(session["user_id"], email, display_name, username)
        except InvitationConflict as exc:
            message = "该邮箱已有账号。" if exc.field == "email" else "该用户名已被使用，请更换用户名。"
            return render("admin_users.html", request, session, error=message, **admin_users_context())
        except ValueError:
            return render("admin_users.html", request, session, error="邮箱或用户名格式无效。", **admin_users_context())
        except PermissionError:
            return render(
                "admin_users.html", request, session, status_code=403, error="无权创建邀请。", **admin_users_context()
            )
        if invitation.status == "already_pending":
            return render(
                "admin_users.html",
                request,
                session,
                success="该用户的邀请已存在，无需重复创建。",
                **admin_users_context(),
            )
        try:
            if account_mailer is None:
                raise RuntimeError("mailer unavailable")
            account_mailer.send_invitation(normalize_email(email), display_name, invitation.token)
        except Exception:
            return render(
                "admin_users.html",
                request,
                session,
                error="邀请已创建，但邮件发送状态无法确认。请让收件人先检查邮箱；如需重发，请使用“重新发送邀请”。",
                **admin_users_context(),
            )
        return render("admin_users.html", request, session, success="邀请已发送。", **admin_users_context())

    @app.post("/admin/users/{user_id}/resend-invitation")
    async def admin_resend_invitation(request: Request, user_id: int):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_users.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_users_context(),
            )
        try:
            invitation = accounts.reissue_invitation(session["user_id"], user_id)
        except (LookupError, ValueError, PermissionError):
            return render("admin_users.html", request, session, error="无法重新发送该邀请。", **admin_users_context())
        try:
            if account_mailer is None:
                raise RuntimeError("mailer unavailable")
            account_mailer.send_invitation(invitation.email, invitation.display_name, invitation.token)
        except Exception:
            return render(
                "admin_users.html",
                request,
                session,
                error="新邀请已创建，但邮件发送状态无法确认。再次重发会使当前链接失效。",
                **admin_users_context(),
            )
        return render(
            "admin_users.html", request, session, success="邀请已重新发送，旧链接已失效。", **admin_users_context()
        )

    @app.post("/admin/users/{user_id}/disable")
    async def admin_disable(request: Request, user_id: int):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_users.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_users_context(),
            )
        try:
            accounts.disable_user(session["user_id"], user_id)
            message, error_message = "账号已停用。", None
        except (LookupError, ValueError, PermissionError) as exc:
            message, error_message = None, str(exc)
        return render(
            "admin_users.html", request, session, success=message, error=error_message, **admin_users_context()
        )

    @app.post("/admin/users/{user_id}/reactivate")
    async def admin_reactivate(request: Request, user_id: int):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_users.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_users_context(),
            )
        try:
            accounts.reactivate_user(session["user_id"], user_id)
            message, error_message = "账号已恢复。", None
        except (LookupError, ValueError, PermissionError) as exc:
            message, error_message = None, str(exc)
        return render(
            "admin_users.html", request, session, success=message, error=error_message, **admin_users_context()
        )

    @app.post("/admin/users/{user_id}/purge")
    async def admin_purge(request: Request, user_id: int):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_users.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_users_context(),
            )
        cleanup = secret_store.delete if secret_store is not None else None
        try:
            accounts.purge_user(session["user_id"], user_id, secret_cleanup=cleanup)
            message, error_message = "账号已永久清理。", None
        except (ValueError, PermissionError) as exc:
            message, error_message = None, str(exc)
        return render(
            "admin_users.html", request, session, success=message, error=error_message, **admin_users_context()
        )

    def admin_settings_context() -> dict:
        with repository.connection() as conn:
            settings = dict(conn.execute("SELECT * FROM app_settings WHERE singleton=1").fetchone())
            row = conn.execute("SELECT * FROM system_smtp_config WHERE singleton=1").fetchone()
        system_smtp = dict(row) if row else None
        return {
            "settings": settings,
            "system_smtp": system_smtp,
            "smtp_password_configured": bool(system_smtp and system_smtp.get("credential_ref")),
            "pending_admin_email": "",
        }

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def admin_settings_page(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        return render("admin_settings.html", request, session, **admin_settings_context())

    @app.post("/admin/settings/email/send-code")
    async def admin_email_send_code(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        ctx = admin_settings_context()
        email_raw = str(form.get("email") or "")
        ctx["pending_admin_email"] = email_raw
        if not form_csrf_ok(request, session, form):
            return render("admin_settings.html", request, session, status_code=403, error="会话已过期，请重试。", **ctx)
        try:
            email = normalize_email(email_raw)
            existing = repository.get_user_by_email(email)
            if existing and existing["id"] != session["user_id"]:
                raise ValueError("email unavailable")
            accounts.check_rate_limit("admin_email_setup", str(session["user_id"]), 5, 3600)
            if account_mailer is None:
                raise RuntimeError("mailer unavailable")
            challenge = accounts.issue_challenge(email, "admin_email_setup")
            account_mailer.send_code(
                email, session.get("display_name") or "Administrator", challenge.code, "admin_email_setup"
            )
        except PermissionError:
            return render("admin_settings.html", request, session, error="发送过于频繁，请稍后再试。", **ctx)
        except Exception:
            return render(
                "admin_settings.html", request, session, error="验证码发送失败，请检查邮箱或系统 SMTP。", **ctx
            )
        return render("admin_settings.html", request, session, success="验证码已发送，请在 10 分钟内完成验证。", **ctx)

    @app.post("/admin/settings/email/complete")
    async def admin_email_complete(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        ctx = admin_settings_context()
        email_raw = str(form.get("email") or "")
        ctx["pending_admin_email"] = email_raw
        if not form_csrf_ok(request, session, form):
            return render("admin_settings.html", request, session, status_code=403, error="会话已过期，请重试。", **ctx)
        try:
            email = normalize_email(email_raw)
        except ValueError:
            return render("admin_settings.html", request, session, error="请输入有效的邮箱地址。", **ctx)
        existing = repository.get_user_by_email(email)
        if (existing and existing["id"] != session["user_id"]) or not accounts.consume_challenge(
            email, "admin_email_setup", str(form.get("code") or "")
        ):
            return render("admin_settings.html", request, session, error="验证码无效或已过期。", **ctx)
        with repository.connection() as conn:
            conn.execute(
                "UPDATE users SET email=?,email_verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND is_admin=1",
                (email, session["user_id"]),
            )
        repository.audit(
            "admin_email_setup", "success", actor_user_id=session["user_id"], target_user_id=session["user_id"]
        )
        return render(
            "admin_settings.html",
            request,
            session,
            success="管理员邮箱已验证。以后请使用该邮箱登录。",
            **admin_settings_context(),
        )

    @app.post("/admin/settings/registration")
    async def admin_registration(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_settings.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_settings_context(),
            )
        enabled = bool(form.get("self_registration_enabled"))
        repository.set_registration_enabled(session["user_id"], enabled)
        repository.audit("set_registration", "success", actor_user_id=session["user_id"], metadata={"enabled": enabled})
        return render("admin_settings.html", request, session, success="注册设置已保存。", **admin_settings_context())

    @app.post("/admin/settings/smtp")
    async def admin_smtp_save(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_settings.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_settings_context(),
            )
        ctx = admin_settings_context()
        password = str(form.get("password") or "")
        if not ctx["smtp_password_configured"] and not password:
            return render("admin_settings.html", request, session, error="首次配置必须填写 SMTP 密码。", **ctx)
        if password and secret_store is None:
            return render("admin_settings.html", request, session, error="密钥存储不可用，无法保存密码。", **ctx)
        credential_ref = ctx["system_smtp"]["credential_ref"] if ctx["system_smtp"] else None
        if password:
            credential_ref = secret_store.write(SYSTEM_SECRET_UUID, "smtp", password)
        try:
            port = int(str(form.get("port") or "587"))
        except ValueError:
            return render("admin_settings.html", request, session, error="端口无效。", **ctx)
        security_mode = str(form.get("security_mode") or "starttls")
        if security_mode not in {"starttls", "ssl"}:
            security_mode = "starttls"
        with repository.connection() as conn:
            conn.execute(
                """INSERT INTO system_smtp_config(singleton,host,port,security_mode,username,sender_address,sender_name,credential_ref,updated_by_user_id)
                   VALUES(1,?,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET host=excluded.host,port=excluded.port,security_mode=excluded.security_mode,
                   username=excluded.username,sender_address=excluded.sender_address,sender_name=excluded.sender_name,
                   credential_ref=excluded.credential_ref,updated_by_user_id=excluded.updated_by_user_id,updated_at=CURRENT_TIMESTAMP""",
                (
                    str(form.get("host") or ""),
                    port,
                    security_mode,
                    str(form.get("username") or ""),
                    str(form.get("sender_address") or ""),
                    str(form.get("sender_name") or "Manuscript Desk"),
                    credential_ref,
                    session["user_id"],
                ),
            )
        repository.audit("system_smtp_update", "success", actor_user_id=session["user_id"])
        return render("admin_settings.html", request, session, success="系统 SMTP 已保存。", **admin_settings_context())

    @app.post("/admin/settings/smtp/test")
    async def admin_smtp_test(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        form = await request.form()
        if not form_csrf_ok(request, session, form):
            return render(
                "admin_settings.html",
                request,
                session,
                status_code=403,
                error="会话已过期，请重试。",
                **admin_settings_context(),
            )
        user = repository.get_user(session["user_id"])
        try:
            if account_mailer is None or not account_mailer.configured():
                raise RuntimeError("系统 SMTP 未配置")
            account_mailer._send(user["email"], "Manuscript Desk · 测试邮件", "这是一封系统 SMTP 测试邮件。")
            return render(
                "admin_settings.html",
                request,
                session,
                success=f"测试邮件已发送至 {user['email']}。",
                **admin_settings_context(),
            )
        except Exception as exc:
            return render(
                "admin_settings.html", request, session, error=f"测试邮件发送失败：{exc}", **admin_settings_context()
            )

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def admin_audit_page(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        with repository.connection() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    """SELECT e.action,e.outcome,e.created_at,a.email AS actor_email,t.email AS target_email
                   FROM audit_events e LEFT JOIN users a ON a.id=e.actor_user_id
                   LEFT JOIN users t ON t.id=e.target_user_id ORDER BY e.id DESC LIMIT 200""",
                )
            ]
        return render("admin_audit.html", request, session, audit_events=events)

    @app.get("/admin/health", response_class=HTMLResponse)
    async def admin_health_page(request: Request):
        session, error = require_admin_page(request)
        if error:
            return error
        database_ok = True
        schema_version = None
        try:
            with repository.connection() as conn:
                assert_schema(conn)
                schema_version = conn.execute("SELECT version FROM app_schema WHERE singleton=1").fetchone()[0]
        except Exception:
            database_ok = False
        mail = "configured" if (account_mailer is not None and account_mailer.configured()) else "not configured"
        health = {
            "status": "healthy" if database_ok else "unhealthy",
            "database": "ok" if database_ok else "error",
            "schema": schema_version,
            "mail": mail,
            "scheduler": "external worker",
            "checked_at": _now_iso(),
        }
        return render("admin_health.html", request, session, health=health)

    # ===================================================================
    # Tenant manuscript workspace (HTML)
    # ===================================================================
    @app.get("/", response_class=HTMLResponse)
    async def index_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        owner = session["user_id"]
        view = request.query_params.get("view", "")
        projection_view = "hidden" if view == "hidden" else "visible"
        cards = repository.manuscript_cards(owner, view=projection_view)
        if view == "starred":
            cards = [card for card in cards if card["starred_count"]]
        elif view == "ongoing":
            cards = [card for card in cards if card["representative"].get("status") in {"Under Review", "Revision"}]
        manuscripts = []
        for card in cards:
            item = dict(card["representative"])
            item.setdefault("SE_or_DE", item.get("editor", ""))
            item.update(
                card_key=card["card_key"],
                card_kind=card["kind"],
                journey_id=card["journey_id"],
                journey_revision=card["revision"],
                member_count=card["member_count"],
                journey_members=card["members"],
                starred_count=card["starred_count"],
                unread_count=card["unread_count"],
                is_hidden=card["hidden"],
                search_text=card["search_text"],
            )
            manuscripts.append(item)
        starred = repository.starred_ids(owner)
        changed = repository.changed_status_ids(owner, repository._latest_submission_rows(owner))
        journal_configs = repository.list_journal_configs_for_browser(owner, enabled_only=True)
        scheduler_status = {"is_running": False, "next_run_time": "由独立调度器管理"}
        return render(
            "index.html",
            request,
            session,
            manuscripts=manuscripts,
            starred_ids=starred,
            changed_ids=changed,
            all_change_count=len(changed),
            journal_configs=journal_configs,
            scheduler_status=scheduler_status,
            workspace_view=view or "all",
            available_manuscripts=repository.ungrouped_manuscripts(owner),
            current_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        configs = repository.list_journal_configs_for_browser(session["user_id"])
        return render("config.html", request, session, journal_configs=configs)

    @app.get("/automation", response_class=HTMLResponse)
    async def automation_page(request: Request):
        session = get_session(request)
        if not session:
            return redirect("/login")
        if session["is_admin"]:
            return redirect("/admin/users")
        return render("automation.html", request, session)

    # ===================================================================
    # Tenant manuscript JSON API (owner-scoped)
    # ===================================================================
    @app.get("/api/manuscripts")
    async def api_manuscripts(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        journal = request.query_params.get("journal")
        status = request.query_params.get("status")
        detailed = request.query_params.get("detailed_status")
        items = repository.latest_manuscripts(session["user_id"])
        if journal:
            items = [m for m in items if m.get("journal") == journal]
        if status:
            items = [m for m in items if status.lower() == (m.get("status") or "").lower()]
        if detailed:
            items = [m for m in items if detailed.lower() in (m.get("detailed_status") or "").lower()]
        return {"success": True, "count": len(items), "manuscripts": items}

    @app.get("/api/manuscript-cards")
    async def api_manuscript_cards(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        view = request.query_params.get("view", "visible")
        try:
            cards = repository.manuscript_cards(session["user_id"], view=view)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return {
            "success": True,
            "count": len(cards),
            "cards": cards,
            "statistics": repository.manuscript_card_statistics(session["user_id"], view=view),
        }

    @app.put("/api/manuscripts/{original_id}/hidden")
    async def api_hide_manuscript(request: Request, original_id: str):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            repository.set_hidden(session["user_id"], original_id, True)
        except LookupError:
            return JSONResponse({"detail": "manuscript not found"}, status_code=404)
        repository.audit(
            "manuscript_hide",
            "success",
            actor_user_id=session["user_id"],
            target_user_id=session["user_id"],
            metadata={"original_id": original_id},
        )
        return {"success": True, "hidden": True}

    @app.delete("/api/manuscripts/{original_id}/hidden")
    async def api_unhide_manuscript(request: Request, original_id: str):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            repository.set_hidden(session["user_id"], original_id, False)
        except LookupError:
            return JSONResponse({"detail": "manuscript not found"}, status_code=404)
        repository.audit(
            "manuscript_unhide",
            "success",
            actor_user_id=session["user_id"],
            target_user_id=session["user_id"],
            metadata={"original_id": original_id},
        )
        return {"success": True, "hidden": False}

    @app.post("/api/journeys")
    async def api_create_journey(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        try:
            journey = repository.create_submission_journey(
                session["user_id"], list(data.get("member_original_ids") or [])
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except sqlite3.IntegrityError:
            return JSONResponse({"detail": "manuscript already belongs to a journey"}, status_code=409)
        return JSONResponse({"success": True, "journey": journey}, status_code=201)

    @app.get("/api/journeys/{journey_id}")
    async def api_get_journey(request: Request, journey_id: int):
        session, error = api_guard(request)
        if error:
            return error
        journey = repository.submission_journey(session["user_id"], journey_id)
        if not journey:
            return JSONResponse({"detail": "submission journey not found"}, status_code=404)
        return {"success": True, "journey": journey}

    @app.put("/api/journeys/{journey_id}/order")
    async def api_replace_journey_members(request: Request, journey_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        try:
            journey = repository.replace_submission_journey_members(
                session["user_id"],
                journey_id,
                list(data.get("ordered_original_ids") or []),
                int(data.get("expected_revision") or 0),
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        except (RuntimeError, sqlite3.IntegrityError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return {"success": True, "journey": journey}

    @app.delete("/api/journeys/{journey_id}/members/{original_id}")
    async def api_unlink_journey_member(request: Request, journey_id: int, original_id: str):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            journey = repository.unlink_submission_journey_member(session["user_id"], journey_id, original_id)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return {"success": True, "journey": journey, "dissolved": journey is None}

    @app.delete("/api/journeys/{journey_id}")
    async def api_delete_journey(request: Request, journey_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        if not repository.delete_submission_journey(session["user_id"], journey_id):
            return JSONResponse({"detail": "submission journey not found"}, status_code=404)
        return {"success": True}

    @app.put("/api/journeys/{journey_id}/hidden")
    async def api_hide_journey(request: Request, journey_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            repository.set_submission_journey_hidden(session["user_id"], journey_id, True)
        except LookupError:
            return JSONResponse({"detail": "submission journey not found"}, status_code=404)
        return {"success": True, "hidden": True}

    @app.delete("/api/journeys/{journey_id}/hidden")
    async def api_unhide_journey(request: Request, journey_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            repository.set_submission_journey_hidden(session["user_id"], journey_id, False)
        except LookupError:
            return JSONResponse({"detail": "submission journey not found"}, status_code=404)
        return {"success": True, "hidden": False}

    @app.post("/api/journeys/{journey_id}/acknowledge")
    async def api_acknowledge_journey(request: Request, journey_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        journey = repository.submission_journey(session["user_id"], journey_id)
        if not journey:
            return JSONResponse({"detail": "submission journey not found"}, status_code=404)
        acknowledged = sum(
            repository.acknowledge(session["user_id"], member["original_id"])
            for member in journey["members"]
            if not member["is_hidden"]
        )
        return {"success": True, "acknowledged": acknowledged}

    @app.get("/api/manuscripts/{original_id}/history")
    async def api_history(request: Request, original_id: str):
        session, error = api_guard(request)
        if error:
            return error
        return {
            "success": True,
            "original_id": original_id,
            "history": repository.manuscript_history(session["user_id"], original_id),
        }

    @app.get("/api/manuscripts/versions/{original_id}")
    async def api_versions(request: Request, original_id: str):
        session, error = api_guard(request)
        if error:
            return error
        return {
            "success": True,
            "original_id": original_id,
            "versions": repository.manuscript_history(session["user_id"], original_id),
        }

    @app.post("/api/manuscripts/{original_id}/star")
    async def api_star(request: Request, original_id: str):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            starred = repository.toggle_star(session["user_id"], original_id)
        except LookupError:
            return JSONResponse({"detail": "manuscript not found"}, status_code=404)
        return {"success": True, "starred": starred}

    @app.post("/api/manuscripts/{original_id}/acknowledge")
    async def api_acknowledge(request: Request, original_id: str):
        session, error = api_guard(request, write=True)
        if error:
            return error
        return {"success": repository.acknowledge(session["user_id"], original_id)}

    @app.post("/api/manuscripts/acknowledge-all")
    async def api_acknowledge_all(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        scope = request.query_params.get("scope", "all")
        items = repository.email_items(owner, "starred" if scope == "starred" else "all")
        for item in items:
            repository.acknowledge(owner, item["original_id"])
        return {"success": True, "acknowledged": len(items)}

    @app.get("/api/statistics")
    async def api_statistics(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        return {"success": True, "statistics": repository.statistics(session["user_id"])}

    # -------------------------------------------------- journal configs (owner)
    @app.get("/api/journal-configs")
    async def api_list_configs(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        return {"success": True, "configs": repository.list_journal_configs_for_browser(session["user_id"])}

    @app.post("/api/journal-configs")
    async def api_create_config(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        provider = str(data.get("provider") or "scholarone")
        try:
            journal_url = validate_provider_url(provider, str(data.get("journal_url") or ""))
        except ValueError:
            return JSONResponse({"detail": "期刊地址与投稿系统不匹配"}, status_code=400)
        password = str(data.get("password") or "")
        if not password:
            return JSONResponse({"detail": "请配置登录密码"}, status_code=400)
        if secret_store is None:
            return JSONResponse({"detail": "密钥存储不可用"}, status_code=503)
        credential_ref = secret_store.write(str(session["user_id"]), "credentials", password)
        payload = {
            "journal_name": str(data.get("journal_name") or "").strip(),
            "journal_url": journal_url,
            "short_name": str(data.get("short_name") or "").strip(),
            "description": str(data.get("description") or "").strip(),
            "username": str(data.get("username") or "").strip(),
            "provider": provider,
            "site_key": get_provider_origin(provider).derive_site_key(journal_url),
            "credential_ref": credential_ref,
            "enabled": bool(data.get("enabled", True)),
        }
        try:
            repository.create_journal_config(session["user_id"], payload)
        except sqlite3.IntegrityError:
            return JSONResponse({"detail": "同名期刊配置已存在"}, status_code=400)
        return {"success": True, "message": "保存成功"}

    @app.put("/api/journal-configs/{config_id}")
    async def api_update_config(request: Request, config_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        data = await request.json()
        provider = str(data.get("provider") or "scholarone")
        try:
            journal_url = validate_provider_url(provider, str(data.get("journal_url") or ""))
        except ValueError:
            return JSONResponse({"detail": "期刊地址与投稿系统不匹配"}, status_code=400)
        payload = {
            "journal_name": str(data.get("journal_name") or "").strip(),
            "journal_url": journal_url,
            "short_name": str(data.get("short_name") or "").strip(),
            "description": str(data.get("description") or "").strip(),
            "username": str(data.get("username") or "").strip(),
            "provider": provider,
            "site_key": get_provider_origin(provider).derive_site_key(journal_url),
            "enabled": bool(data.get("enabled", True)),
        }
        if not repository.update_journal_config(owner, config_id, payload):
            return JSONResponse({"detail": "期刊配置不存在"}, status_code=404)
        password = str(data.get("password") or "")
        if password:
            if secret_store is None:
                return JSONResponse({"detail": "密钥存储不可用"}, status_code=503)
            credential_ref = secret_store.write(str(owner), "credentials", password)
            with repository.connection() as conn:
                conn.execute(
                    "UPDATE journal_configs SET credential_ref=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
                    (credential_ref, config_id, owner),
                )
        return {"success": True, "message": "更新成功"}

    @app.patch("/api/journal-configs/{config_id}/enabled")
    async def api_toggle_config(request: Request, config_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        enabled = bool(data.get("enabled"))
        with repository.connection() as conn:
            result = conn.execute(
                "UPDATE journal_configs SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
                (int(enabled), config_id, session["user_id"]),
            )
        if result.rowcount != 1:
            return JSONResponse({"detail": "期刊配置不存在"}, status_code=404)
        return {"success": True, "enabled": enabled}

    @app.delete("/api/journal-configs/{config_id}")
    async def api_delete_config(request: Request, config_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        try:
            with repository.connection() as conn:
                result = conn.execute(
                    "DELETE FROM journal_configs WHERE id=? AND owner_user_id=?", (config_id, session["user_id"])
                )
        except sqlite3.IntegrityError:
            return JSONResponse({"detail": "该期刊仍有稿件记录，无法删除"}, status_code=400)
        if result.rowcount != 1:
            return JSONResponse({"detail": "期刊配置不存在"}, status_code=404)
        return {"success": True, "message": "删除成功"}

    # -------------------------------------------------- manual update queue
    @app.post("/api/update")
    async def api_manual_update(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        if not repository.list_journal_configs(owner, enabled_only=True):
            return JSONResponse(
                {
                    "active": False,
                    "phase": "failed",
                    "outcome": "failed",
                    "error_code": "no_enabled_journals",
                    "message": _UPDATE_MESSAGES["no_enabled_journals"],
                },
                status_code=409,
            )
        scheduled_for = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        enqueue = repository.enqueue_run(owner, "update", scheduled_for, source="manual", requested_by_user_id=owner)
        item = repository.get_run_request(owner, enqueue["request"]["id"])
        response = _update_status_view(item)
        response["created"] = enqueue["created"]
        return JSONResponse(response, status_code=202 if enqueue["created"] else 200)

    @app.get("/api/update/status")
    async def api_update_status(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        owner = session["user_id"]
        active = repository.active_update_request(owner)
        with repository.connection() as conn:
            row = conn.execute(
                "SELECT id FROM automation_run_requests WHERE owner_user_id=? AND kind='update' ORDER BY id DESC LIMIT 1",
                (owner,),
            ).fetchone()
            last_update = conn.execute(
                "SELECT MAX(recorded_at) FROM manuscript_records WHERE owner_user_id=?", (owner,)
            ).fetchone()[0]
        latest = repository.get_run_request(owner, row["id"]) if row else None
        return _update_status_view(active or latest, last_update)

    @app.get("/api/scheduler/status")
    async def api_scheduler_status(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        return {"success": True, "scheduler": {"is_running": False, "next_run_time": "由独立调度器管理"}}

    @app.post("/api/scheduler/start")
    async def api_scheduler_start(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        return {"success": True, "message": "调度由独立 worker 管理，此实例不内嵌调度器。"}

    @app.post("/api/scheduler/stop")
    async def api_scheduler_stop(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        return {"success": True, "message": "调度由独立 worker 管理，此实例不内嵌调度器。"}

    # ===================================================================
    # Tenant automation API (owner-scoped)
    # ===================================================================
    def job_view(job: dict, active_update: dict | None = None, active_emails: dict[int, dict] | None = None) -> dict:
        payload = job.get("payload") or {}
        active_emails = active_emails or {}
        with repository.connection() as conn:
            last = conn.execute(
                "SELECT status FROM automation_job_runs WHERE owner_user_id=? AND job_id=? ORDER BY id DESC LIMIT 1",
                (job["owner_user_id"], job["id"]),
            ).fetchone()
        next_run = ""
        try:
            next_run = (cron_next_runs(job["cron_expression"], job["timezone"], 1) or [""])[0]
        except Exception:
            next_run = "无法解析"
        active_request = active_update if job["kind"] == "update" else active_emails.get(job["id"])
        return {
            "id": job["id"],
            "name": job["name"],
            "kind": job["kind"],
            "cron_expression": job["cron_expression"],
            "timezone": job["timezone"],
            "enabled": bool(job["enabled"]),
            "manuscript_scope": payload.get("manuscript_scope", "starred"),
            "delivery_policy": payload.get("delivery_policy", "changes_only"),
            "recipients": job.get("recipients", []),
            "last_status": last["status"] if last else None,
            "active_request": active_request,
            "next_run_time": next_run,
        }

    @app.get("/api/automation/jobs")
    async def api_automation_jobs(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        owner = session["user_id"]
        active_update = repository.active_update_request(owner)
        active_emails = repository.active_email_requests(owner)
        jobs = [job_view(job, active_update, active_emails) for job in repository.list_automation_jobs(owner)]
        return {
            "jobs": jobs,
            "active_update": active_update,
            "scheduler": {
                "is_running": False,
                "is_check_running": active_update is not None,
            },
        }

    def _job_payload(data: dict) -> dict:
        kind = str(data.get("kind") or "")
        payload = {}
        if kind == "email":
            manuscript_scope = data.get("manuscript_scope", "starred")
            if manuscript_scope not in {"all", "starred", "working"}:
                raise ValueError("invalid manuscript scope")
            payload = {
                "manuscript_scope": manuscript_scope,
                "delivery_policy": "always" if data.get("delivery_policy") == "always" else "changes_only",
            }
        return {
            "kind": kind,
            "name": str(data.get("name") or "").strip(),
            "cron_expression": str(data.get("cron_expression") or "").strip(),
            "timezone": str(data.get("timezone") or "Asia/Shanghai").strip(),
            "enabled": bool(data.get("enabled", True)),
            "payload": payload,
            "recipients": list(data.get("recipients") or []),
        }

    @app.post("/api/automation/jobs")
    async def api_create_job(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        try:
            cron_next_runs(str(data.get("cron_expression") or ""), str(data.get("timezone") or "Asia/Shanghai"), 1)
        except Exception:
            return JSONResponse({"detail": "Cron 表达式或时区无效"}, status_code=400)
        try:
            job_id = repository.save_automation_job(session["user_id"], _job_payload(data))
        except (ValueError, KeyError):
            return JSONResponse({"detail": "任务参数无效"}, status_code=400)
        return {"success": True, "id": job_id}

    @app.put("/api/automation/jobs/{job_id}")
    async def api_update_job(request: Request, job_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        try:
            cron_next_runs(str(data.get("cron_expression") or ""), str(data.get("timezone") or "Asia/Shanghai"), 1)
        except Exception:
            return JSONResponse({"detail": "Cron 表达式或时区无效"}, status_code=400)
        try:
            repository.save_automation_job(session["user_id"], _job_payload(data), job_id=job_id)
        except LookupError:
            return JSONResponse({"detail": "任务不存在"}, status_code=404)
        except (ValueError, KeyError):
            return JSONResponse({"detail": "任务参数无效"}, status_code=400)
        return {"success": True, "id": job_id}

    @app.patch("/api/automation/jobs/{job_id}/enabled")
    async def api_toggle_job(request: Request, job_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        job = repository.get_automation_job(owner, job_id)
        if not job:
            return JSONResponse({"detail": "任务不存在"}, status_code=404)
        data = await request.json()
        job_data = {
            "kind": job["kind"],
            "name": job["name"],
            "cron_expression": job["cron_expression"],
            "timezone": job["timezone"],
            "enabled": bool(data.get("enabled")),
            "payload": job.get("payload") or {},
            "recipients": job.get("recipients", []),
        }
        repository.save_automation_job(owner, job_data, job_id=job_id)
        return {"success": True, "enabled": bool(data.get("enabled"))}

    @app.post("/api/automation/jobs/{job_id}/run")
    async def api_run_job(request: Request, job_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        job = repository.get_automation_job(owner, job_id)
        if not job:
            return JSONResponse({"detail": "任务不存在"}, status_code=404)
        scheduled_for = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        enqueue = repository.enqueue_run(
            owner,
            job["kind"],
            scheduled_for,
            source="manual",
            job_id=job_id,
            requested_by_user_id=owner,
        )
        item = enqueue["request"]
        return {
            "success": True,
            "status": "queued" if enqueue["created"] else "already_active",
            "request_id": item["id"],
            "request": repository.get_run_request(owner, item["id"]),
        }

    @app.get("/api/automation/requests/{request_id}")
    async def api_run_request(request: Request, request_id: int):
        session, error = api_guard(request)
        if error:
            return error
        item = repository.get_run_request(session["user_id"], request_id)
        if not item:
            return JSONResponse({"detail": "运行请求不存在"}, status_code=404)
        return {"request": item}

    @app.delete("/api/automation/jobs/{job_id}")
    async def api_delete_job(request: Request, job_id: int):
        session, error = api_guard(request, write=True)
        if error:
            return error
        if not repository.delete_automation_job(session["user_id"], job_id):
            return JSONResponse({"detail": "任务不存在"}, status_code=404)
        return {"success": True}

    @app.post("/api/automation/cron-preview")
    async def api_cron_preview(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        data = await request.json()
        try:
            runs = cron_next_runs(str(data.get("cron_expression") or ""), str(data.get("timezone") or "Asia/Shanghai"))
        except Exception:
            return JSONResponse({"detail": "Cron 表达式或时区无效"}, status_code=400)
        return {"next_runs": runs}

    @app.post("/api/automation/reload")
    async def api_reload(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        return {"success": True, "message": "任务由独立调度 worker 加载。"}

    @app.get("/api/automation/runs")
    async def api_runs(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        with repository.connection() as conn:
            rows = conn.execute(
                """SELECT COALESCE(j.kind,q.kind) AS kind,r.status,r.started_at,r.summary_json,j.name AS job_name
                   FROM automation_job_runs r
                   LEFT JOIN automation_jobs j ON j.id=r.job_id AND j.owner_user_id=r.owner_user_id
                   LEFT JOIN automation_run_requests q ON q.id=r.request_id AND q.owner_user_id=r.owner_user_id
                   WHERE r.owner_user_id=? ORDER BY r.id DESC LIMIT 50""",
                (session["user_id"],),
            ).fetchall()
        runs = [
            {
                "job_name": row["job_name"] or "手动",
                "kind": row["kind"],
                "started_at": row["started_at"],
                "status": row["status"],
                "summary_json": row["summary_json"],
            }
            for row in rows
        ]
        return {"runs": runs}

    @app.get("/api/automation/smtp")
    async def api_get_smtp(request: Request):
        session, error = api_guard(request)
        if error:
            return error
        smtp = repository.get_user_smtp(session["user_id"]) or {}
        return {
            "host": smtp.get("host", ""),
            "port": smtp.get("port", 587),
            "security_mode": smtp.get("security_mode", "starttls"),
            "username": smtp.get("username", ""),
            "sender_address": smtp.get("sender_address", ""),
            "sender_name": smtp.get("sender_name", ""),
            "connect_timeout_seconds": 20,
            "password_configured": bool(smtp.get("credential_ref")),
        }

    @app.put("/api/automation/smtp")
    async def api_put_smtp(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        data = await request.json()
        existing = repository.get_user_smtp(owner) or {}
        try:
            port = int(data.get("port") or 587)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "端口无效"}, status_code=400)
        security_mode = data.get("security_mode") if data.get("security_mode") in {"starttls", "ssl"} else "starttls"
        if not str(data.get("host") or "").strip() or not str(data.get("sender_address") or "").strip():
            return JSONResponse({"detail": "主机与发件地址必填"}, status_code=400)
        repository.save_user_smtp(
            owner,
            {
                "host": str(data.get("host")).strip(),
                "port": port,
                "security_mode": security_mode,
                "username": str(data.get("username") or "").strip(),
                "sender_address": str(data.get("sender_address")).strip(),
                "sender_name": str(data.get("sender_name") or "").strip(),
                "credential_ref": existing.get("credential_ref"),
            },
        )
        return {"success": True}

    @app.put("/api/automation/smtp/password")
    async def api_put_smtp_password(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        owner = session["user_id"]
        data = await request.json()
        password = str(data.get("password") or "")
        if not password:
            return JSONResponse({"detail": "密码不能为空"}, status_code=400)
        if secret_store is None:
            return JSONResponse({"detail": "密钥存储不可用"}, status_code=503)
        existing = repository.get_user_smtp(owner)
        if not existing:
            return JSONResponse({"detail": "请先保存 SMTP 基本设置"}, status_code=400)
        credential_ref = secret_store.write(str(owner), "smtp", password)
        merged = dict(existing)
        merged["credential_ref"] = credential_ref
        repository.save_user_smtp(owner, merged)
        return {"success": True}

    @app.post("/api/automation/smtp/test")
    async def api_test_smtp(request: Request):
        session, error = api_guard(request, write=True)
        if error:
            return error
        # A live personal-SMTP send is performed by the automation worker, not this
        # web instance. Report configuration readiness so the UI can guide the user.
        smtp = repository.get_user_smtp(session["user_id"])
        if not smtp or not smtp.get("credential_ref"):
            return JSONResponse({"detail": "请先配置 SMTP 主机与密码"}, status_code=400)
        return {"success": True, "message": "配置有效，实际投递由自动化 worker 执行。"}

    # ===================================================================
    # Public scoped-token API + legacy Shortcut compatibility (fail closed)
    # ===================================================================
    def authenticate_token(request: Request, required_scope: str, *, query_only: bool = True) -> dict | None:
        raw = ""
        if not query_only:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                raw = auth[7:].strip()
        if query_only and not raw:
            raw = request.query_params.get("api", "")
        if not raw:
            return None
        now = _now_iso()
        with repository.connection() as conn:
            row = conn.execute(
                """SELECT t.id,t.owner_user_id,t.scopes FROM api_tokens t JOIN users u ON u.id=t.owner_user_id
                   WHERE t.token_hash=? AND t.revoked_at IS NULL AND u.is_active=1
                     AND (t.expires_at IS NULL OR t.expires_at>?)""",
                (token_hash(raw), now),
            ).fetchone()
            if not row or required_scope not in set((row["scopes"] or "").split()):
                return None
            conn.execute("UPDATE api_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        return dict(row)

    @app.get("/starred/email-ack", response_class=HTMLResponse)
    async def email_ack_page(request: Request):
        return templates.TemplateResponse("email_ack.html", {"request": request})

    @app.post("/starred/email-ack/consume")
    async def consume_email_ack(request: Request):
        try:
            data = await request.json()
            raw = str(data.get("token") or "")
        except Exception:
            raw = ""
        if not raw:
            return JSONResponse({"detail": "链接无效或已过期"}, status_code=400)
        now = _now_iso()
        with repository.connection() as conn:
            row = conn.execute(
                "SELECT owner_user_id FROM automation_ack_tokens WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?",
                (token_hash(raw), now),
            ).fetchone()
        if not row:
            return JSONResponse({"detail": "链接无效或已过期"}, status_code=400)
        result = repository.consume_ack_token(row["owner_user_id"], raw)
        if not result:
            return JSONResponse({"detail": "链接无效或已过期"}, status_code=400)
        return result

    continuation_key = hmac.new(
        shortcut_continuation_key or b"",
        b"manuscript-desk/shortcut-continuation/v1",
        hashlib.sha256,
    ).digest()

    def shortcut_response(request: Request, owner: int, manuscript_scope: str = "starred", update_message: str = ""):
        items = repository.email_items(owner, manuscript_scope)
        unread_ids = repository.unread_status_ids(owner, items)
        for item in items:
            item["status_changed"] = item["original_id"] in unread_ids
            first_seen = item.get("status_pair_first_seen_at") or ""
            item["status_pair_first_seen_ago"] = _time_ago(first_seen)
        label = "标星稿件" if manuscript_scope == "starred" else "进行中稿件"
        response = templates.TemplateResponse(
            "shortcut_starred.html",
            {
                "request": request,
                "manuscripts": items,
                "changed_count": len(unread_ids),
                "update_message": update_message,
                "waiting_timeout": False,
                "continuation_url": "",
                "shortcut_title": "Starred" if manuscript_scope == "starred" else "Working",
                "shortcut_label": label,
                "shortcut_empty_message": f"没有{label}",
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def make_continuation(owner: int, request_id: int, token_id: int, update_scope: str) -> str:
        payload = {
            "v": 2,
            "o": owner,
            "r": request_id,
            "t": token_id,
            "s": update_scope,
            "e": int(time.time()) + shortcut_continuation_ttl_seconds,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(continuation_key, encoded, hashlib.sha256).digest()
        return b".".join((encoded, base64.urlsafe_b64encode(signature).rstrip(b"="))).decode("ascii")

    def read_continuation(raw: str, expected_scope: str) -> tuple[int, int, int, str] | None:
        if not raw or len(raw) > 512 or "." not in raw:
            return None
        encoded_text, signature_text = raw.split(".", 1)
        try:
            encoded = encoded_text.encode("ascii")
            expected = hmac.new(continuation_key, encoded, hashlib.sha256).digest()
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded_text + "=" * (-len(encoded_text) % 4)))
            version = payload.get("v")
            if int(payload.get("e", 0)) < int(time.time()):
                return None
            if version == 1:
                scope = "starred"
            elif version == 2 and payload.get("s") in {"starred", "working"}:
                scope = str(payload["s"])
            else:
                return None
            if scope != expected_scope:
                return None
            owner, request_id, token_id = int(payload["o"]), int(payload["r"]), int(payload["t"])
            if owner <= 0 or request_id <= 0 or token_id <= 0:
                return None
            return owner, request_id, token_id, scope
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    async def wait_for_update(request: Request, owner: int, request_id: int) -> tuple[dict | None, bool]:
        deadline = time.monotonic() + shortcut_wait_seconds
        while True:
            item = await asyncio.to_thread(repository.get_run_request, owner, request_id)
            if not item or item.get("kind") != "update":
                return None, False
            if not item.get("active"):
                return item, False
            if time.monotonic() >= deadline:
                return item, True
            if await request.is_disconnected():
                return None, False
            await asyncio.sleep(shortcut_poll_seconds)

    def update_result_message(item: dict) -> str:
        status = str(item.get("status") or "")
        result = str(item.get("result") or "")
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}

        def count(name: str) -> int:
            value = summary.get(name, 0)
            return value if isinstance(value, int) and value >= 0 else 0

        attempted = count("attempted_journals")
        successful = count("successful_journals")
        failed = count("failed_journals")
        update_scope = str(summary.get("update_scope") or "all")
        noun = (
            "个标星期刊"
            if update_scope == "starred"
            else ("个进行中稿件期刊" if update_scope == "working" else "个期刊")
        )
        if summary.get("reason") == "no_enabled_starred_journals":
            return "标星稿件所在期刊未启用"
        if summary.get("reason") == "no_enabled_working_journals":
            return "进行中稿件所在期刊未启用"
        if status == "cancelled":
            return "更新未完成"
        if status == "failed" or result == "failed":
            if attempted == 0:
                return (
                    "没有标星期刊需要更新"
                    if update_scope == "starred"
                    else ("没有进行中稿件期刊需要更新" if update_scope == "working" else "没有启用的期刊配置")
                )
            return f"更新失败，已完成 {successful}/{attempted} {noun}"
        if result == "partial":
            return f"已更新 {successful}/{attempted} {noun}，{failed} 个失败"
        if result == "no_changes":
            return f"已更新 {successful}/{attempted} {noun}，没有发现变化"
        if result == "success":
            return f"已更新 {successful}/{attempted} {noun}"
        return "更新已结束"

    def shortcut_waiting_response(request: Request, continuation: str, update_scope: str) -> HTMLResponse:
        label = "标星稿件" if update_scope == "starred" else "进行中稿件"
        response = templates.TemplateResponse(
            "shortcut_starred.html",
            {
                "request": request,
                "manuscripts": [],
                "changed_count": 0,
                "update_message": "更新仍在进行",
                "waiting_timeout": True,
                "continuation_url": f"/update_{update_scope}?continue={continuation}",
                "shortcut_title": "Update Starred" if update_scope == "starred" else "Update Working",
                "shortcut_label": label,
                "shortcut_empty_message": f"没有{label}",
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/starred", response_class=HTMLResponse)
    async def shortcut_starred(request: Request):
        token = authenticate_token(request, "starred:read", query_only=True)
        if token is None:
            return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
        return shortcut_response(request, token["owner_user_id"])

    @app.get("/working", response_class=HTMLResponse)
    async def shortcut_working(request: Request):
        token = authenticate_token(request, "working:read", query_only=True)
        if token is None:
            return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
        return shortcut_response(request, token["owner_user_id"], "working")

    async def shortcut_update(request: Request, update_scope: str):
        required_scope = f"{update_scope}:update"
        raw_api = request.query_params.get("api", "")
        raw_continuation = request.query_params.get("continue", "")
        if bool(raw_api) == bool(raw_continuation):
            return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
        if raw_continuation:
            target = read_continuation(raw_continuation, update_scope)
            if target is None:
                return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
            owner, request_id, token_id, _ = target
            with repository.connection() as conn:
                token_row = conn.execute(
                    """SELECT 1 FROM api_tokens t JOIN users u ON u.id=t.owner_user_id
                       WHERE t.id=? AND t.owner_user_id=? AND t.revoked_at IS NULL
                         AND (t.expires_at IS NULL OR t.expires_at>?)
                         AND (' ' || t.scopes || ' ') LIKE ? AND u.is_active=1""",
                    (token_id, owner, _now_iso(), f"% {required_scope} %"),
                ).fetchone()
            current = await asyncio.to_thread(repository.get_run_request, owner, request_id)
            if (
                not token_row
                or not current
                or current.get("kind") != "update"
                or (current.get("payload") or {}).get("update_scope") != update_scope
            ):
                return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
        else:
            token = authenticate_token(request, required_scope, query_only=True)
            if token is None:
                return HTMLResponse("<h3>链接无效或已失效</h3>", status_code=401)
            owner = token["owner_user_id"]
            target_method = (
                repository.starred_update_target if update_scope == "starred" else repository.working_update_target
            )
            target = await asyncio.to_thread(target_method, owner)
            count_key = "starred_manuscript_count" if update_scope == "starred" else "working_manuscript_count"
            label = "标星" if update_scope == "starred" else "进行中"
            if target[count_key] == 0:
                return shortcut_response(request, owner, update_scope, f"没有{label}稿件需要更新")
            config_ids = target["enabled_journal_config_ids"]
            if not config_ids:
                return shortcut_response(request, owner, update_scope, f"{label}稿件所在期刊未启用")
            scheduled_for = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            enqueue = await asyncio.to_thread(
                repository.enqueue_run,
                owner,
                "update",
                scheduled_for,
                "manual",
                None,
                owner,
                payload={"update_scope": update_scope, "journal_config_ids": config_ids},
            )
            if not enqueue["created"] and enqueue.get("coverage") == "different":
                return shortcut_response(request, owner, update_scope, "另一项更新正在进行，请稍后重试")
            request_id = enqueue["request"]["id"]
            token_id = token["id"]
        item, timed_out = await wait_for_update(request, owner, request_id)
        if item is None:
            return HTMLResponse("<h3>更新状态不可用</h3>", status_code=503)
        continuation = raw_continuation or make_continuation(owner, request_id, token_id, update_scope)
        if timed_out:
            return shortcut_waiting_response(request, continuation, update_scope)
        message = update_result_message(item)
        if not raw_continuation and "enqueue" in locals() and enqueue.get("coverage") == "superset":
            message = f"已加入正在进行的全量更新；{message}"
        return shortcut_response(request, owner, update_scope, message)

    @app.get("/update_starred", response_class=HTMLResponse)
    @app.get("/renew_starred", response_class=HTMLResponse, include_in_schema=False)
    async def shortcut_update_starred(request: Request):
        return await shortcut_update(request, "starred")

    @app.get("/update_working", response_class=HTMLResponse)
    async def shortcut_update_working(request: Request):
        return await shortcut_update(request, "working")

    # The dedicated Shortcut capability intentionally excludes acknowledgement.
    @app.get("/starred/ack", response_class=HTMLResponse)
    async def legacy_starred_ack(request: Request):
        return HTMLResponse("<h3>此快捷指令没有标记已读权限</h3>", status_code=403)

    @app.get("/starred/ack-all", response_class=HTMLResponse)
    async def legacy_starred_ack_all(request: Request):
        return HTMLResponse("<h3>此快捷指令没有标记已读权限</h3>", status_code=403)
