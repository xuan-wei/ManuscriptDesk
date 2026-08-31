import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .repository import Repository, require_owner
from .security import (
    generate_otp,
    normalize_email,
    normalize_username,
    otp_hmac,
    random_token,
    token_hash,
    verify_otp,
)

OTP_PURPOSES = {"register", "password_reset", "email_change", "admin_email_setup", "invite"}


@dataclass(frozen=True)
class IssuedChallenge:
    challenge_id: int
    code: str
    expires_at: str


@dataclass(frozen=True)
class InvitationResult:
    status: str
    user_id: int
    token: str | None = None


@dataclass(frozen=True)
class ReissuedInvitation:
    user_id: int
    email: str
    display_name: str
    token: str


class InvitationConflict(ValueError):
    def __init__(self, field: str):
        super().__init__(f"{field} already in use")
        self.field = field


class AccountService:
    def __init__(self, repository: Repository, otp_key: bytes, rate_limit_key: bytes):
        if len(otp_key) < 32 or len(rate_limit_key) < 32:
            raise ValueError("account service keys must be at least 32 bytes")
        self.repository = repository
        self.otp_key = otp_key
        self.rate_limit_key = rate_limit_key

    def _subject_hash(self, value: str) -> str:
        return hmac.new(self.rate_limit_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def check_rate_limit(self, action: str, subject: str, maximum: int, window_seconds: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        digest = self._subject_hash(subject)
        with self.repository.connection(immediate=True) as conn:
            conn.execute("DELETE FROM rate_limit_events WHERE occurred_at < ?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE action=? AND subject_hash=? AND occurred_at>=?",
                (action, digest, cutoff),
            ).fetchone()[0]
            if count >= maximum:
                conn.rollback()
                raise PermissionError("rate limit exceeded")
            conn.execute("INSERT INTO rate_limit_events(action,subject_hash) VALUES(?,?)", (action, digest))
            conn.commit()

    def issue_challenge(self, email: str, purpose: str, ttl_minutes: int = 10) -> IssuedChallenge:
        email = normalize_email(email)
        if purpose not in OTP_PURPOSES:
            raise ValueError("invalid challenge purpose")
        code = generate_otp()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
        with self.repository.connection(immediate=True) as conn:
            conn.execute(
                "UPDATE email_challenges SET used_at=CURRENT_TIMESTAMP WHERE email=? AND purpose=? AND used_at IS NULL",
                (email, purpose),
            )
            cursor = conn.execute(
                "INSERT INTO email_challenges(email,purpose,code_hmac,expires_at) VALUES(?,?,?,?)",
                (email, purpose, otp_hmac(self.otp_key, email, purpose, code), expires_at),
            )
            conn.commit()
            return IssuedChallenge(cursor.lastrowid, code, expires_at)

    def consume_challenge(self, email: str, purpose: str, code: str) -> bool:
        email = normalize_email(email)
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM email_challenges
                   WHERE email=? AND purpose=? AND used_at IS NULL AND expires_at>?
                   ORDER BY id DESC LIMIT 1""",
                (email, purpose, now),
            ).fetchone()
            if not row or row["attempts"] >= 5:
                conn.rollback()
                return False
            valid = verify_otp(self.otp_key, email, purpose, str(code or ""), row["code_hmac"])
            if not valid:
                attempts = row["attempts"] + 1
                used_at = datetime.now(timezone.utc).isoformat() if attempts >= 5 else None
                conn.execute(
                    "UPDATE email_challenges SET attempts=?,used_at=? WHERE id=?", (attempts, used_at, row["id"])
                )
                conn.commit()
                return False
            result = conn.execute(
                "UPDATE email_challenges SET used_at=?,attempts=attempts+1 WHERE id=? AND used_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
            return result.rowcount == 1

    def _invitation_conflict(self, conn, email: str, username: str) -> InvitationResult | InvitationConflict | None:
        email_user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        username_user = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if (
            email_user
            and username_user
            and email_user["id"] == username_user["id"]
            and email_user["password_scheme"] == "activation_pending"
        ):
            return InvitationResult("already_pending", email_user["id"])
        if email_user:
            return InvitationConflict("email")
        if username_user:
            return InvitationConflict("username")
        return None

    def create_invitation(
        self, admin_user_id: int, email: str, display_name: str, username: str | None = None, ttl_hours: int = 48
    ) -> InvitationResult:
        require_owner(admin_user_id)
        email = normalize_email(email)
        username = normalize_username(username or email.split("@", 1)[0])
        display_name = display_name.strip()
        raw_token = random_token()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            conflict = self._invitation_conflict(conn, email, username)
            if isinstance(conflict, InvitationResult):
                conn.rollback()
                return conflict
            if conflict:
                conn.rollback()
                raise conflict
            try:
                user_id = conn.execute(
                    """INSERT INTO users(email,username,display_name,password_hash,password_scheme,is_admin,is_active)
                       VALUES(?,?,?,?,'activation_pending',0,0)""",
                    (email, username, display_name, "!pending"),
                ).lastrowid
                conn.execute(
                    """INSERT INTO activation_tokens(target_user_id,token_hash,purpose,expires_at,created_by_user_id)
                       VALUES(?,?,'invite',?,?)""",
                    (user_id, token_hash(raw_token), expires_at, admin_user_id),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                with self.repository.connection() as retry_conn:
                    conflict = self._invitation_conflict(retry_conn, email, username)
                if isinstance(conflict, InvitationResult):
                    return conflict
                if conflict:
                    raise conflict
                raise
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome,metadata_json) VALUES(?,?,'invite_user','success',?)",
                (admin_user_id, user_id, json.dumps({"email_hash": self._subject_hash(email)}, separators=(",", ":"))),
            )
            conn.commit()
        return InvitationResult("created", user_id, raw_token)

    def reissue_invitation(self, admin_user_id: int, target_user_id: int, ttl_hours: int = 48) -> ReissuedInvitation:
        require_owner(admin_user_id)
        require_owner(target_user_id)
        raw_token = random_token()
        now = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            target = conn.execute(
                "SELECT id,email,display_name,password_scheme,is_active FROM users WHERE id=?", (target_user_id,)
            ).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            if not target:
                conn.rollback()
                raise LookupError("user not found")
            if target["password_scheme"] != "activation_pending" or target["is_active"]:
                conn.rollback()
                raise ValueError("invitation is not pending")
            conn.execute(
                """UPDATE activation_tokens SET revoked_at=?
                   WHERE target_user_id=? AND purpose='invite' AND consumed_at IS NULL AND revoked_at IS NULL""",
                (now, target_user_id),
            )
            conn.execute(
                """INSERT INTO activation_tokens(target_user_id,token_hash,purpose,expires_at,created_by_user_id)
                   VALUES(?,?,'invite',?,?)""",
                (target_user_id, token_hash(raw_token), expires_at, admin_user_id),
            )
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome) VALUES(?,?,'resend_invitation','success')",
                (admin_user_id, target_user_id),
            )
            conn.commit()
        return ReissuedInvitation(target_user_id, target["email"], target["display_name"], raw_token)

    def activate_invitation(self, raw_token: str, password_hash: str) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.connection(immediate=True) as conn:
            token = conn.execute(
                """SELECT t.*,u.email FROM activation_tokens t JOIN users u ON u.id=t.target_user_id
                   WHERE t.token_hash=? AND t.purpose='invite' AND t.consumed_at IS NULL AND t.revoked_at IS NULL AND t.expires_at>?""",
                (token_hash(raw_token), now),
            ).fetchone()
            if not token:
                conn.rollback()
                return None
            result = conn.execute(
                "UPDATE activation_tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, token["id"]),
            )
            if result.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """UPDATE users SET password_hash=?,password_scheme='argon2id',is_active=1,email_verified_at=?,updated_at=?
                   WHERE id=? AND password_scheme='activation_pending'""",
                (password_hash, now, now, token["target_user_id"]),
            )
            conn.commit()
            return token["target_user_id"]

    def disable_user(self, admin_user_id: int, target_user_id: int) -> str:
        require_owner(admin_user_id)
        require_owner(target_user_id)
        now = datetime.now(timezone.utc)
        purge_at = now + timedelta(days=30)
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            target = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (target_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            if not target:
                conn.rollback()
                raise LookupError("user not found")
            if target["is_admin"]:
                active_admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1").fetchone()[
                    0
                ]
                if active_admins <= 1:
                    conn.rollback()
                    raise ValueError("cannot disable the last active administrator")
            conn.execute(
                "UPDATE users SET is_active=0,disabled_at=?,purge_eligible_at=?,updated_at=? WHERE id=?",
                (now.isoformat(), purge_at.isoformat(), now.isoformat(), target_user_id),
            )
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now.isoformat(), target_user_id),
            )
            conn.execute(
                "UPDATE api_tokens SET revoked_at=? WHERE owner_user_id=? AND revoked_at IS NULL",
                (now.isoformat(), target_user_id),
            )
            conn.execute(
                "UPDATE automation_jobs SET enabled=0,updated_at=? WHERE owner_user_id=?",
                (now.isoformat(), target_user_id),
            )
            conn.execute(
                "UPDATE automation_run_requests SET status='cancelled',updated_at=? WHERE owner_user_id=? AND status='queued'",
                (now.isoformat(), target_user_id),
            )
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome) VALUES(?,?,'disable_user','success')",
                (admin_user_id, target_user_id),
            )
            conn.commit()
        return purge_at.isoformat()

    def reactivate_user(self, admin_user_id: int, target_user_id: int) -> None:
        require_owner(admin_user_id)
        require_owner(target_user_id)
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            target = conn.execute("SELECT id,password_scheme FROM users WHERE id=?", (target_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            if not target:
                conn.rollback()
                raise LookupError("user not found")
            if target["password_scheme"] == "activation_pending":
                conn.rollback()
                raise ValueError("invitation has not been activated")
            conn.execute(
                "UPDATE users SET is_active=1,disabled_at=NULL,purge_eligible_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target_user_id,),
            )
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome) VALUES(?,?,'reactivate_user','success')",
                (admin_user_id, target_user_id),
            )
            conn.commit()

    def purge_preview(self, admin_user_id: int) -> list[dict]:
        require_owner(admin_user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.connection() as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                raise PermissionError("active administrator required")
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT id,email,display_name,disabled_at,purge_eligible_at
                   FROM users WHERE is_active=0 AND disabled_at IS NOT NULL AND purge_eligible_at<=?
                   ORDER BY purge_eligible_at,id""",
                    (now,),
                )
            ]

    def purge_user(self, admin_user_id: int, target_user_id: int, secret_cleanup=None) -> dict:
        require_owner(admin_user_id)
        require_owner(target_user_id)
        now = datetime.now(timezone.utc).isoformat()
        refs: list[str] = []
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            target = conn.execute("SELECT * FROM users WHERE id=?", (target_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            if (
                not target
                or target["is_active"]
                or not target["purge_eligible_at"]
                or target["purge_eligible_at"] > now
            ):
                conn.rollback()
                raise ValueError("account is not eligible for purge")
            refs.extend(
                row[0]
                for row in conn.execute(
                    "SELECT credential_ref FROM journal_configs WHERE owner_user_id=? AND credential_ref IS NOT NULL",
                    (target_user_id,),
                )
            )
            refs.extend(
                row[0]
                for row in conn.execute(
                    "SELECT secret_ref FROM api_tokens WHERE owner_user_id=? AND secret_ref IS NOT NULL",
                    (target_user_id,),
                )
            )
            row = conn.execute(
                "SELECT credential_ref FROM user_smtp_configs WHERE owner_user_id=?", (target_user_id,)
            ).fetchone()
            if row and row[0]:
                refs.append(row[0])
            account_hash = self._subject_hash(f"account:{target['id']}:{target['email']}")
            conn.execute("UPDATE audit_events SET actor_user_id=NULL WHERE actor_user_id=?", (target_user_id,))
            conn.execute("UPDATE audit_events SET target_user_id=NULL WHERE target_user_id=?", (target_user_id,))
            conn.execute(
                "UPDATE app_settings SET updated_by_user_id=NULL WHERE updated_by_user_id=?", (target_user_id,)
            )
            conn.execute(
                "UPDATE system_smtp_config SET updated_by_user_id=NULL WHERE updated_by_user_id=?", (target_user_id,)
            )
            conn.execute(
                "UPDATE activation_tokens SET created_by_user_id=NULL WHERE created_by_user_id=?", (target_user_id,)
            )
            conn.execute(
                "UPDATE automation_run_requests SET requested_by_user_id=NULL WHERE requested_by_user_id=?",
                (target_user_id,),
            )
            conn.execute(
                "INSERT INTO deleted_user_tombstones(account_hash,purged_by_user_id,disabled_at,app_version) VALUES(?,?,?,?)",
                (account_hash, admin_user_id, target["disabled_at"], "1"),
            )
            conn.execute("DELETE FROM users WHERE id=?", (target_user_id,))
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,action,outcome,metadata_json) VALUES(?,'purge_user','success',?)",
                (admin_user_id, json.dumps({"account_hash": account_hash}, separators=(",", ":"))),
            )
            conn.commit()
        if secret_cleanup:
            for reference in refs:
                secret_cleanup(reference)
        return {"user_id": target_user_id, "account_hash": account_hash, "secret_refs": refs}

    def begin_admin_email_setup(self, admin_user_id: int, email: str, ttl_hours: int = 24) -> str:
        require_owner(admin_user_id)
        email = normalize_email(email)
        raw_token = random_token()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        with self.repository.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin,is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                conn.rollback()
                raise PermissionError("active administrator required")
            existing = conn.execute("SELECT id FROM users WHERE email=? AND id<>?", (email, admin_user_id)).fetchone()
            if existing:
                conn.rollback()
                raise ValueError("email unavailable")
            conn.execute(
                "UPDATE activation_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE target_user_id=? AND purpose='admin_email_setup' AND consumed_at IS NULL AND revoked_at IS NULL",
                (admin_user_id,),
            )
            conn.execute(
                "INSERT INTO activation_tokens(target_user_id,token_hash,purpose,expires_at,created_by_user_id) VALUES(?,?,'admin_email_setup',?,?)",
                (admin_user_id, token_hash(raw_token), expires_at, admin_user_id),
            )
            # Store only a normalized address alongside this short-lived token; the token itself remains hashed.
            conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome,metadata_json) VALUES(?,?,'admin_email_setup_begin','success',?)",
                (admin_user_id, admin_user_id, json.dumps({"pending_email": email}, separators=(",", ":"))),
            )
            conn.commit()
        return raw_token

    def complete_admin_email_setup(self, raw_token: str) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.connection(immediate=True) as conn:
            token = conn.execute(
                """SELECT * FROM activation_tokens WHERE token_hash=? AND purpose='admin_email_setup'
                   AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
                (token_hash(raw_token), now),
            ).fetchone()
            if not token:
                conn.rollback()
                return None
            event = conn.execute(
                """SELECT metadata_json FROM audit_events WHERE actor_user_id=? AND target_user_id=?
                   AND action='admin_email_setup_begin' ORDER BY id DESC LIMIT 1""",
                (token["target_user_id"], token["target_user_id"]),
            ).fetchone()
            try:
                email = normalize_email(json.loads(event[0])["pending_email"])
            except (TypeError, KeyError, ValueError, json.JSONDecodeError):
                conn.rollback()
                return None
            if conn.execute("SELECT 1 FROM users WHERE email=? AND id<>?", (email, token["target_user_id"])).fetchone():
                conn.rollback()
                return None
            conn.execute("UPDATE activation_tokens SET consumed_at=? WHERE id=?", (now, token["id"]))
            conn.execute(
                "UPDATE users SET email=?,email_verified_at=?,updated_at=? WHERE id=?",
                (email, now, now, token["target_user_id"]),
            )
            conn.commit()
            return token["target_user_id"]
