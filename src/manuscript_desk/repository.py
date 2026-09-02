import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .automation_summaries import project_automation_summary
from .schema import assert_schema, connect
from .security import normalize_email, normalize_username, token_hash
from .timezones import DEFAULT_TIMEZONE, validate_timezone


def require_owner(owner_user_id: int) -> int:
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id <= 0:
        raise ValueError("owner_user_id must be a positive integer")
    return owner_user_id


def _safe_tracking_error(value: object) -> str:
    allowed = {
        "browser_start_failed": "browser_start_failed",
        "browser_timeout": "browser_timeout",
        "credentials_unavailable": "credentials_unavailable",
        "empty_result_anomaly": "empty_result_anomaly",
        "provider_login_failed": "provider_login_failed",
        "provider_unavailable": "provider_unavailable",
        "unsafe_provider_redirect": "unsafe_provider_redirect",
    }
    code = str(value or "").strip().casefold()
    return allowed.get(code, "tracking_failed") if code else ""


class SmtpUnavailableError(RuntimeError):
    def __init__(self, error_code: str, source: str):
        super().__init__(error_code)
        self.error_code = error_code
        self.source = source


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        with self.connection() as conn:
            assert_schema(conn)

    @contextmanager
    def connection(self, immediate: bool = False):
        conn = connect(self.db_path)
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        username: str | None = None,
        password_scheme: str = "argon2id",
        is_admin: bool = False,
        verified: bool = False,
    ) -> int:
        verified_at = datetime.now(timezone.utc).isoformat() if verified else None
        normalized_email = normalize_email(email)
        normalized_username = normalize_username(username or normalized_email.split("@", 1)[0])
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO users
                   (email, username, display_name, password_hash, password_scheme, is_admin, email_verified_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    normalized_email,
                    normalized_username,
                    display_name.strip(),
                    password_hash,
                    password_scheme,
                    int(is_admin),
                    verified_at,
                ),
            )
            return cursor.lastrowid

    def get_user_by_email(self, email: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
            return dict(row) if row else None

    def get_user_for_login(self, identifier: str):
        value = str(identifier or "").strip()
        with self.connection() as conn:
            if "@" in value:
                try:
                    email = normalize_email(value)
                except ValueError:
                    return None
                row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            else:
                try:
                    username = normalize_username(value)
                except ValueError:
                    return None
                row = conn.execute(
                    "SELECT * FROM users WHERE username=? COLLATE NOCASE",
                    (username,),
                ).fetchone()
            return dict(row) if row else None

    def get_user(self, user_id: int):
        require_owner(user_id)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def update_password(self, user_id: int, password_hash: str, scheme: str = "argon2id") -> bool:
        require_owner(user_id)
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE users SET password_hash=?, password_scheme=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (password_hash, scheme, user_id),
            )
            return result.rowcount == 1

    def registration_enabled(self) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT self_registration_enabled FROM app_settings WHERE singleton=1").fetchone()
            return bool(row[0])

    def set_registration_enabled(self, admin_user_id: int, enabled: bool) -> None:
        require_owner(admin_user_id)
        with self.connection(immediate=True) as conn:
            admin = conn.execute("SELECT is_admin, is_active FROM users WHERE id=?", (admin_user_id,)).fetchone()
            if not admin or not admin["is_admin"] or not admin["is_active"]:
                raise PermissionError("active administrator required")
            conn.execute(
                "UPDATE app_settings SET self_registration_enabled=?,updated_by_user_id=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1",
                (int(enabled), admin_user_id),
            )
            conn.commit()

    def create_session(
        self, user_id: int, raw_token: str, csrf_secret: str, idle_hours: int = 24, absolute_days: int = 7
    ) -> int:
        require_owner(user_id)
        now = datetime.now(timezone.utc)
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO user_sessions
                   (user_id,token_hash,csrf_secret,idle_expires_at,absolute_expires_at)
                   VALUES(?,?,?,?,?)""",
                (
                    user_id,
                    token_hash(raw_token),
                    csrf_secret,
                    (now + timedelta(hours=idle_hours)).isoformat(),
                    (now + timedelta(days=absolute_days)).isoformat(),
                ),
            )
            return cursor.lastrowid

    def get_active_session(self, raw_token: str, touch: bool = True, idle_hours: int = 24):
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connection(immediate=touch) as conn:
            row = conn.execute(
                """SELECT s.*,u.email,u.username,u.display_name,u.is_admin,u.is_active,u.email_verified_at
                   FROM user_sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL AND u.is_active=1
                     AND s.idle_expires_at>? AND s.absolute_expires_at>?""",
                (token_hash(raw_token), now, now),
            ).fetchone()
            if row and touch:
                new_idle = min(now_dt + timedelta(hours=idle_hours), datetime.fromisoformat(row["absolute_expires_at"]))
                conn.execute(
                    "UPDATE user_sessions SET last_seen_at=?,idle_expires_at=? WHERE id=?",
                    (now, new_idle.isoformat(), row["id"]),
                )
                conn.commit()
            elif touch:
                conn.rollback()
            return dict(row) if row else None

    def revoke_session(self, raw_token: str) -> bool:
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE user_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE token_hash=? AND revoked_at IS NULL",
                (token_hash(raw_token),),
            )
            return result.rowcount == 1

    def revoke_sessions(self, user_id: int) -> int:
        require_owner(user_id)
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE user_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL",
                (user_id,),
            )
            return result.rowcount

    def get_shortcut_settings(self, owner_user_id: int) -> dict[str, Any] | None:
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_shortcut_settings WHERE owner_user_id=?",
                (owner_user_id,),
            ).fetchone()
            return dict(row) if row else None

    def save_shortcut_base_url(self, owner_user_id: int, base_url: str) -> None:
        require_owner(owner_user_id)
        value = str(base_url or "").strip()
        if not value:
            raise ValueError("shortcut base URL is required")
        with self.connection(immediate=True) as conn:
            conn.execute(
                """INSERT INTO user_shortcut_settings(owner_user_id,base_url)
                   VALUES(?,?) ON CONFLICT(owner_user_id) DO UPDATE SET
                   base_url=excluded.base_url,updated_at=CURRENT_TIMESTAMP""",
                (owner_user_id, value),
            )

    def create_api_token(
        self,
        owner_user_id: int,
        raw_token: str,
        label: str,
        scopes: list[str],
        expires_at: str | None = None,
        secret_ref: str | None = None,
    ) -> int:
        require_owner(owner_user_id)
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO api_tokens(owner_user_id,token_hash,token_prefix,secret_ref,label,scopes,expires_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    owner_user_id,
                    token_hash(raw_token),
                    raw_token[:8],
                    secret_ref,
                    label.strip(),
                    " ".join(sorted(set(scopes))),
                    expires_at,
                ),
            )
            return cursor.lastrowid

    def revoke_api_token(self, owner_user_id: int, token_id: int) -> str | None:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            token = conn.execute(
                "SELECT secret_ref FROM api_tokens WHERE id=? AND owner_user_id=? AND revoked_at IS NULL",
                (token_id, owner_user_id),
            ).fetchone()
            if not token:
                return None
            conn.execute(
                "UPDATE api_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
                (token_id, owner_user_id),
            )
            return token["secret_ref"] or ""

    def create_journal_config(self, owner_user_id: int, data: dict[str, Any]) -> int:
        require_owner(owner_user_id)
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO journal_configs
                   (owner_user_id,journal_name,journal_url,short_name,description,username,provider,site_key,credential_ref,enabled)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    owner_user_id,
                    data["journal_name"].strip(),
                    data["journal_url"].strip(),
                    data.get("short_name", "").strip(),
                    data.get("description", "").strip(),
                    data.get("username", "").strip(),
                    data.get("provider", "scholarone").strip(),
                    data.get("site_key", "").strip(),
                    data.get("credential_ref"),
                    int(data.get("enabled", True)),
                ),
            )
            return cursor.lastrowid

    def list_journal_configs(self, owner_user_id: int, enabled_only: bool = False) -> list[dict]:
        require_owner(owner_user_id)
        sql = "SELECT * FROM journal_configs WHERE owner_user_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY journal_name"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, (owner_user_id,))]

    def list_journal_configs_for_browser(self, owner_user_id: int, enabled_only: bool = False) -> list[dict]:
        """Return only fields safe and useful in tenant browser responses."""
        require_owner(owner_user_id)
        sql = """SELECT id,journal_name,journal_url,short_name,description,
                        username,provider,site_key,enabled,
                        CASE WHEN credential_ref IS NOT NULL AND credential_ref<>''
                             THEN 1 ELSE 0 END AS credential_configured
                 FROM journal_configs WHERE owner_user_id=?"""
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY journal_name"
        with self.connection() as conn:
            items = [dict(row) for row in conn.execute(sql, (owner_user_id,))]
        for item in items:
            item["enabled"] = bool(item["enabled"])
            item["credential_configured"] = bool(item["credential_configured"])
        return items

    def update_journal_config(self, owner_user_id: int, config_id: int, data: dict[str, Any]) -> bool:
        require_owner(owner_user_id)
        with self.connection() as conn:
            result = conn.execute(
                """UPDATE journal_configs
                   SET journal_name=?,journal_url=?,short_name=?,description=?,
                       username=?,provider=?,site_key=?,enabled=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (
                    data["journal_name"],
                    data["journal_url"],
                    data.get("short_name", ""),
                    data.get("description", ""),
                    data.get("username", ""),
                    data.get("provider", "scholarone"),
                    data.get("site_key", ""),
                    int(data.get("enabled", True)),
                    config_id,
                    owner_user_id,
                ),
            )
            return result.rowcount == 1

    def save_manuscript(self, owner_user_id: int, journal_config_id: int, record: dict[str, Any]) -> int:
        require_owner(owner_user_id)
        with self.connection() as conn:
            owned = conn.execute(
                "SELECT 1 FROM journal_configs WHERE id=? AND owner_user_id=?",
                (journal_config_id, owner_user_id),
            ).fetchone()
            if not owned:
                raise LookupError("journal configuration not found")
            cursor = conn.execute(
                """INSERT INTO manuscript_records
                   (owner_user_id,journal_config_id,original_id,manuscript_id,title,journal,
                    version_number,author_role,status,status_text,detailed_status,editor,
                    submitted_date,decision_date,provider,source_original_id,status_date,
                    final_disposition,workflow_folder,authorship,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))""",
                (
                    owner_user_id,
                    journal_config_id,
                    record["original_id"],
                    record["manuscript_id"],
                    record.get("title", ""),
                    record.get("journal", ""),
                    record.get("version_number", 1),
                    record.get("author_role", ""),
                    record.get("status", ""),
                    record.get("status_text", ""),
                    record.get("detailed_status", ""),
                    record.get("SE_or_DE", record.get("editor", "")),
                    record.get("submitted_date", ""),
                    record.get("decision_date", ""),
                    record.get("provider", "scholarone"),
                    record.get("source_original_id", record["original_id"]),
                    record.get("status_date", ""),
                    record.get("final_disposition", ""),
                    record.get("workflow_folder", ""),
                    record.get("authorship", ""),
                    record.get("recorded_at"),
                ),
            )
            return cursor.lastrowid

    def persist_journal_result(
        self,
        owner_user_id: int,
        journal_config_id: int,
        result: dict[str, Any],
    ) -> int:
        require_owner(owner_user_id)
        outcome = str(result.get("outcome") or ("complete" if result.get("success") else "failed"))
        if outcome not in {"complete", "verified_empty", "anomalous_empty", "failed"}:
            raise ValueError("invalid tracking outcome")
        manuscripts = list(result.get("manuscripts") or [])
        if not manuscripts and outcome == "complete":
            outcome = "anomalous_empty"
        if outcome in {"anomalous_empty", "failed"}:
            manuscripts = []
        for record in manuscripts:
            if not isinstance(record, dict) or not record.get("original_id") or not record.get("manuscript_id"):
                raise ValueError("invalid manuscript result")

        with self.connection(immediate=True) as conn:
            config = conn.execute(
                "SELECT journal_name,provider FROM journal_configs WHERE id=? AND owner_user_id=?",
                (journal_config_id, owner_user_id),
            ).fetchone()
            if not config:
                raise LookupError("journal configuration not found")
            for record in manuscripts:
                conn.execute(
                    """INSERT INTO manuscript_records
                       (owner_user_id,journal_config_id,original_id,manuscript_id,title,journal,
                        version_number,author_role,status,status_text,detailed_status,editor,
                        submitted_date,decision_date,provider,source_original_id,status_date,
                        final_disposition,workflow_folder,authorship,recorded_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))""",
                    (
                        owner_user_id,
                        journal_config_id,
                        record["original_id"],
                        record["manuscript_id"],
                        record.get("title", ""),
                        record.get("journal", config["journal_name"]),
                        record.get("version_number", 1),
                        record.get("author_role", ""),
                        record.get("status", ""),
                        record.get("status_text", ""),
                        record.get("detailed_status", ""),
                        record.get("SE_or_DE", record.get("editor", "")),
                        record.get("submitted_date", ""),
                        record.get("decision_date", ""),
                        record.get("provider", config["provider"]),
                        record.get("source_original_id", record["original_id"]),
                        record.get("status_date", ""),
                        record.get("final_disposition", ""),
                        record.get("workflow_folder", ""),
                        record.get("authorship", ""),
                        record.get("recorded_at"),
                    ),
                )
            error_code = "empty_result_anomaly" if outcome == "anomalous_empty" else result.get("error_code", "")
            conn.execute(
                """INSERT INTO query_logs
                   (owner_user_id,journal_config_id,journal,provider,success,manuscripts_found,error_message)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    owner_user_id,
                    journal_config_id,
                    config["journal_name"],
                    config["provider"],
                    int(outcome in {"complete", "verified_empty"}),
                    len(manuscripts),
                    _safe_tracking_error(error_code),
                ),
            )
        return len(manuscripts)

    def _latest_submission_rows(self, owner_user_id: int) -> list[dict]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT r.*,COALESCE(j.short_name,'') AS short_name,
                          COALESCE(
                            (
                              SELECT NULLIF(first_sub.submitted_date,'')
                              FROM manuscript_records first_sub
                              WHERE first_sub.owner_user_id=r.owner_user_id
                                AND first_sub.original_id=r.original_id
                                AND NULLIF(first_sub.submitted_date,'') IS NOT NULL
                              ORDER BY first_sub.version_number ASC,first_sub.id ASC
                              LIMIT 1
                            ),
                            NULLIF(r.submitted_date,''),
                            ''
                          ) AS first_submitted_date,
                          (
                            SELECT COALESCE(
                                     NULLIF(h.decision_date,''),
                                     NULLIF(h.status_date,''),
                                     h.recorded_at
                                   )
                            FROM manuscript_records h
                            WHERE h.owner_user_id=r.owner_user_id
                              AND h.original_id=r.original_id
                              AND h.version_number=r.version_number
                              AND h.status=r.status
                              AND h.detailed_status=r.detailed_status
                              AND h.id>(
                                SELECT COALESCE(MAX(previous.id),0)
                                FROM manuscript_records previous
                                WHERE previous.owner_user_id=r.owner_user_id
                                  AND previous.original_id=r.original_id
                                  AND previous.version_number=r.version_number
                                  AND previous.id<r.id
                                  AND (
                                    previous.status<>r.status
                                    OR previous.detailed_status<>r.detailed_status
                                  )
                              )
                            ORDER BY datetime(h.recorded_at) ASC,h.id ASC
                            LIMIT 1
                          ) AS status_pair_first_seen_at
                   FROM manuscript_records r
                   LEFT JOIN journal_configs j
                     ON j.id=r.journal_config_id
                    AND j.owner_user_id=r.owner_user_id
                   JOIN (
                     SELECT m.owner_user_id,m.original_id,MAX(m.id) AS max_id
                     FROM manuscript_records m
                     JOIN (
                       SELECT owner_user_id,original_id,MAX(version_number) AS max_version
                       FROM manuscript_records WHERE owner_user_id=?
                       GROUP BY owner_user_id,original_id
                     ) versions
                       ON versions.owner_user_id=m.owner_user_id
                      AND versions.original_id=m.original_id
                      AND versions.max_version=m.version_number
                     WHERE m.owner_user_id=?
                     GROUP BY m.owner_user_id,m.original_id
                   ) latest ON latest.max_id=r.id AND latest.owner_user_id=r.owner_user_id
                   WHERE r.owner_user_id=? ORDER BY r.recorded_at DESC,r.id DESC""",
                (owner_user_id, owner_user_id, owner_user_id),
            )
            items = [dict(row) for row in rows]
            for item in items:
                item["submitted_date"] = item.pop("first_submitted_date", "")
            return items

    def latest_manuscripts(self, owner_user_id: int, *, visibility: str = "visible") -> list[dict]:
        if visibility not in {"visible", "hidden", "all"}:
            raise ValueError("invalid manuscript visibility")
        rows = self._latest_submission_rows(owner_user_id)
        hidden = self.effectively_hidden_ids(owner_user_id)
        for row in rows:
            row["is_hidden"] = row["original_id"] in hidden
        if visibility == "visible":
            return [row for row in rows if not row["is_hidden"]]
        if visibility == "hidden":
            return [row for row in rows if row["is_hidden"]]
        return rows

    def manuscript_history(self, owner_user_id: int, original_id: str) -> list[dict]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM manuscript_records
                   WHERE owner_user_id=? AND original_id=?
                   ORDER BY version_number ASC,recorded_at ASC,id ASC""",
                (owner_user_id, original_id),
            )
            history: list[dict] = []
            previous_state: tuple[Any, ...] | None = None
            for row in rows:
                record = dict(row)
                state = (
                    record["manuscript_id"],
                    record["version_number"],
                    record["status"],
                    record["detailed_status"],
                )
                if state != previous_state:
                    history.append(record)
                    previous_state = state
            return history

    def set_hidden(self, owner_user_id: int, original_id: str, hidden: bool) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM manuscript_records WHERE owner_user_id=? AND original_id=? LIMIT 1",
                (owner_user_id, original_id),
            ).fetchone()
            if not exists:
                raise LookupError("manuscript not found")
            if hidden:
                conn.execute(
                    "INSERT OR IGNORE INTO manuscript_hides(owner_user_id,original_id) VALUES(?,?)",
                    (owner_user_id, original_id),
                )
            else:
                conn.execute(
                    "DELETE FROM manuscript_hides WHERE owner_user_id=? AND original_id=?",
                    (owner_user_id, original_id),
                )
            return hidden

    def hidden_ids(self, owner_user_id: int) -> set[str]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT original_id FROM manuscript_hides WHERE owner_user_id=?",
                    (owner_user_id,),
                )
            }

    def effectively_hidden_ids(self, owner_user_id: int) -> set[str]:
        require_owner(owner_user_id)
        hidden = self.hidden_ids(owner_user_id)
        with self.connection() as conn:
            hidden.update(
                row[0]
                for row in conn.execute(
                    """SELECT member.original_id
                       FROM submission_journey_members member
                       JOIN submission_journeys journey
                         ON journey.id=member.journey_id
                        AND journey.owner_user_id=member.owner_user_id
                       WHERE member.owner_user_id=? AND journey.hidden_at IS NOT NULL""",
                    (owner_user_id,),
                )
            )
        return hidden

    @staticmethod
    def _validate_original_ids(original_ids: list[str], *, minimum: int = 1) -> list[str]:
        ids = [str(value or "").strip() for value in original_ids]
        if len(ids) < minimum or any(not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid manuscript selection")
        return ids

    @staticmethod
    def _audit_in_connection(
        conn: sqlite3.Connection, owner_user_id: int, action: str, metadata: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome,metadata_json) VALUES(?,?,?,?,?)",
            (
                owner_user_id,
                owner_user_id,
                action,
                "success",
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
            ),
        )

    def _require_journey(self, conn: sqlite3.Connection, owner_user_id: int, journey_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM submission_journeys WHERE id=? AND owner_user_id=?",
            (journey_id, owner_user_id),
        ).fetchone()
        if not row:
            raise LookupError("submission journey not found")
        return row

    def _validate_owned_submissions(
        self, conn: sqlite3.Connection, owner_user_id: int, original_ids: list[str]
    ) -> None:
        placeholders = ",".join("?" for _ in original_ids)
        rows = conn.execute(
            f"""SELECT DISTINCT original_id FROM manuscript_records
                WHERE owner_user_id=? AND original_id IN ({placeholders})""",
            (owner_user_id, *original_ids),
        ).fetchall()
        if {row["original_id"] for row in rows} != set(original_ids):
            raise LookupError("manuscript not found")

    def create_submission_journey(self, owner_user_id: int, original_ids: list[str]) -> dict[str, Any]:
        require_owner(owner_user_id)
        ids = self._validate_original_ids(original_ids, minimum=2)
        with self.connection(immediate=True) as conn:
            self._validate_owned_submissions(conn, owner_user_id, ids)
            placeholders = ",".join("?" for _ in ids)
            grouped = conn.execute(
                f"""SELECT original_id FROM submission_journey_members
                    WHERE owner_user_id=? AND original_id IN ({placeholders}) LIMIT 1""",
                (owner_user_id, *ids),
            ).fetchone()
            if grouped:
                raise sqlite3.IntegrityError("manuscript already belongs to a journey")
            journey_id = conn.execute(
                "INSERT INTO submission_journeys(owner_user_id) VALUES(?)",
                (owner_user_id,),
            ).lastrowid
            conn.executemany(
                """INSERT INTO submission_journey_members
                   (owner_user_id,journey_id,original_id,position) VALUES(?,?,?,?)""",
                [(owner_user_id, journey_id, original_id, position) for position, original_id in enumerate(ids)],
            )
            self._audit_in_connection(
                conn,
                owner_user_id,
                "journey_create",
                {"journey_id": journey_id, "member_count": len(ids)},
            )
        return self.submission_journey(owner_user_id, int(journey_id))

    def submission_journey(self, owner_user_id: int, journey_id: int) -> dict[str, Any] | None:
        require_owner(owner_user_id)
        rows_by_id = {row["original_id"]: row for row in self._latest_submission_rows(owner_user_id)}
        stars = self.starred_ids(owner_user_id)
        hidden = self.hidden_ids(owner_user_id)
        changed = self.changed_status_ids(owner_user_id, list(rows_by_id.values()))
        with self.connection() as conn:
            journey = conn.execute(
                "SELECT * FROM submission_journeys WHERE id=? AND owner_user_id=?",
                (journey_id, owner_user_id),
            ).fetchone()
            if not journey:
                return None
            memberships = conn.execute(
                """SELECT original_id,position FROM submission_journey_members
                   WHERE journey_id=? AND owner_user_id=? ORDER BY position""",
                (journey_id, owner_user_id),
            ).fetchall()
        members = []
        for membership in memberships:
            record = rows_by_id.get(membership["original_id"])
            if not record:
                continue
            item = dict(record)
            item.update(
                position=membership["position"],
                is_hidden=membership["original_id"] in hidden,
                starred=membership["original_id"] in stars,
                unread=membership["original_id"] in changed,
            )
            members.append(item)
        if not members:
            return None
        representative = dict(members[-1])
        return {
            "id": journey["id"],
            "revision": journey["revision"],
            "hidden": bool(journey["hidden_at"]),
            "members": members,
            "member_count": len(members),
            "representative": representative,
            "starred_count": sum(item["starred"] and not item["is_hidden"] for item in members),
            "unread_count": sum(item["unread"] and not item["is_hidden"] for item in members),
        }

    def list_submission_journeys(self, owner_user_id: int) -> list[dict[str, Any]]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM submission_journeys WHERE owner_user_id=? ORDER BY updated_at DESC,id DESC",
                    (owner_user_id,),
                )
            ]
        return [journey for journey_id in ids if (journey := self.submission_journey(owner_user_id, journey_id))]

    def replace_submission_journey_members(
        self, owner_user_id: int, journey_id: int, original_ids: list[str], expected_revision: int
    ) -> dict[str, Any]:
        require_owner(owner_user_id)
        ids = self._validate_original_ids(original_ids, minimum=2)
        with self.connection(immediate=True) as conn:
            journey = self._require_journey(conn, owner_user_id, journey_id)
            if int(journey["revision"]) != int(expected_revision):
                raise RuntimeError("submission journey revision conflict")
            existing = [
                row[0]
                for row in conn.execute(
                    """SELECT original_id FROM submission_journey_members
                       WHERE owner_user_id=? AND journey_id=? ORDER BY position""",
                    (owner_user_id, journey_id),
                )
            ]
            self._validate_owned_submissions(conn, owner_user_id, ids)
            placeholders = ",".join("?" for _ in ids)
            conflict = conn.execute(
                f"""SELECT original_id FROM submission_journey_members
                    WHERE owner_user_id=? AND journey_id<>? AND original_id IN ({placeholders}) LIMIT 1""",
                (owner_user_id, journey_id, *ids),
            ).fetchone()
            if conflict:
                raise sqlite3.IntegrityError("manuscript already belongs to a journey")
            conn.execute(
                "DELETE FROM submission_journey_members WHERE owner_user_id=? AND journey_id=?",
                (owner_user_id, journey_id),
            )
            conn.executemany(
                """INSERT INTO submission_journey_members
                   (owner_user_id,journey_id,original_id,position) VALUES(?,?,?,?)""",
                [(owner_user_id, journey_id, original_id, position) for position, original_id in enumerate(ids)],
            )
            conn.execute(
                """UPDATE submission_journeys SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (journey_id, owner_user_id),
            )
            self._audit_in_connection(
                conn,
                owner_user_id,
                "journey_members_replace",
                {"journey_id": journey_id, "old_count": len(existing), "new_count": len(ids)},
            )
        return self.submission_journey(owner_user_id, journey_id)

    def unlink_submission_journey_member(
        self, owner_user_id: int, journey_id: int, original_id: str
    ) -> dict[str, Any] | None:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            self._require_journey(conn, owner_user_id, journey_id)
            result = conn.execute(
                """DELETE FROM submission_journey_members
                   WHERE owner_user_id=? AND journey_id=? AND original_id=?""",
                (owner_user_id, journey_id, original_id),
            )
            if result.rowcount != 1:
                raise LookupError("journey member not found")
            remaining = [
                row[0]
                for row in conn.execute(
                    """SELECT original_id FROM submission_journey_members
                       WHERE owner_user_id=? AND journey_id=? ORDER BY position""",
                    (owner_user_id, journey_id),
                )
            ]
            if len(remaining) < 2:
                conn.execute(
                    "DELETE FROM submission_journeys WHERE id=? AND owner_user_id=?",
                    (journey_id, owner_user_id),
                )
                self._audit_in_connection(
                    conn,
                    owner_user_id,
                    "journey_dissolve",
                    {"journey_id": journey_id, "reason": "singleton"},
                )
                return None
            conn.execute(
                "UPDATE submission_journey_members SET position=position+1000000 WHERE owner_user_id=? AND journey_id=?",
                (owner_user_id, journey_id),
            )
            for position, member_id in enumerate(remaining):
                conn.execute(
                    """UPDATE submission_journey_members SET position=?
                       WHERE owner_user_id=? AND journey_id=? AND original_id=?""",
                    (position, owner_user_id, journey_id, member_id),
                )
            conn.execute(
                """UPDATE submission_journeys SET revision=revision+1,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (journey_id, owner_user_id),
            )
            self._audit_in_connection(
                conn,
                owner_user_id,
                "journey_unlink",
                {"journey_id": journey_id, "remaining_count": len(remaining)},
            )
        return self.submission_journey(owner_user_id, journey_id)

    def delete_submission_journey(self, owner_user_id: int, journey_id: int) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            result = conn.execute(
                "DELETE FROM submission_journeys WHERE id=? AND owner_user_id=?",
                (journey_id, owner_user_id),
            )
            if result.rowcount:
                self._audit_in_connection(conn, owner_user_id, "journey_dissolve", {"journey_id": journey_id})
            return result.rowcount == 1

    def set_submission_journey_hidden(self, owner_user_id: int, journey_id: int, hidden: bool) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            result = conn.execute(
                """UPDATE submission_journeys
                   SET hidden_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                       revision=revision+1,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (int(hidden), journey_id, owner_user_id),
            )
            if result.rowcount != 1:
                raise LookupError("submission journey not found")
            self._audit_in_connection(
                conn,
                owner_user_id,
                "journey_hide" if hidden else "journey_unhide",
                {"journey_id": journey_id},
            )
        return hidden

    def changed_status_ids(self, owner_user_id: int, items: list[dict] | None = None) -> set[str]:
        require_owner(owner_user_id)
        current_items = items if items is not None else self._latest_submission_rows(owner_user_id)
        with self.connection() as conn:
            acknowledgements = {
                row["original_id"]: (row["ack_status"], row["ack_detailed_status"])
                for row in conn.execute(
                    "SELECT original_id,ack_status,ack_detailed_status FROM manuscript_acks WHERE owner_user_id=?",
                    (owner_user_id,),
                )
            }
        return {
            item["original_id"]
            for item in current_items
            if item["original_id"] not in acknowledgements
            or acknowledgements[item["original_id"]] != (item.get("status") or "", item.get("detailed_status") or "")
        }

    def ungrouped_manuscripts(self, owner_user_id: int) -> list[dict]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            grouped_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT original_id FROM submission_journey_members WHERE owner_user_id=?",
                    (owner_user_id,),
                )
            }
        return [row for row in self._latest_submission_rows(owner_user_id) if row["original_id"] not in grouped_ids]

    def manuscript_cards(self, owner_user_id: int, *, view: str = "visible") -> list[dict[str, Any]]:
        require_owner(owner_user_id)
        if view not in {"visible", "hidden", "all"}:
            raise ValueError("invalid manuscript card view")
        rows = self._latest_submission_rows(owner_user_id)
        by_id = {row["original_id"]: row for row in rows}
        stars = self.starred_ids(owner_user_id)
        hidden = self.hidden_ids(owner_user_id)
        changed = self.changed_status_ids(owner_user_id, rows)
        journeys = self.list_submission_journeys(owner_user_id)
        grouped_ids = {member["original_id"] for journey in journeys for member in journey["members"]}
        cards: list[dict[str, Any]] = []
        for row in rows:
            original_id = row["original_id"]
            if original_id in grouped_ids:
                continue
            is_hidden = original_id in hidden
            if view == "visible" and is_hidden or view == "hidden" and not is_hidden:
                continue
            representative = dict(row)
            representative["is_hidden"] = is_hidden
            cards.append(
                {
                    "card_key": f"manuscript:{original_id}",
                    "kind": "manuscript",
                    "journey_id": None,
                    "revision": None,
                    "member_count": 1,
                    "representative": representative,
                    "members": [representative],
                    "starred_count": int(original_id in stars and not is_hidden),
                    "unread_count": int(original_id in changed and not is_hidden),
                    "hidden": is_hidden,
                    "search_text": f"{row.get('title', '')} {row.get('manuscript_id', '')}".casefold(),
                }
            )
        for journey in journeys:
            if view == "visible" and journey["hidden"] or view == "hidden" and not journey["hidden"]:
                if not (view == "hidden" and not journey["hidden"] and any(m["is_hidden"] for m in journey["members"])):
                    continue
            representative = dict(journey["representative"])
            cards.append(
                {
                    "card_key": f"journey:{journey['id']}",
                    "kind": "journey",
                    "journey_id": journey["id"],
                    "revision": journey["revision"],
                    "member_count": journey["member_count"],
                    "representative": representative,
                    "members": journey["members"],
                    "starred_count": journey["starred_count"],
                    "unread_count": journey["unread_count"],
                    "hidden": journey["hidden"],
                    "search_text": " ".join(
                        f"{member.get('title', '')} {member.get('manuscript_id', '')}" for member in journey["members"]
                    ).casefold(),
                }
            )
        cards.sort(
            key=lambda card: (
                str(card["representative"].get("recorded_at") or ""),
                int(card["representative"].get("id") or 0),
            ),
            reverse=True,
        )
        return cards

    def manuscript_card_statistics(self, owner_user_id: int, *, view: str = "visible") -> dict[str, int]:
        cards = self.manuscript_cards(owner_user_id, view=view)
        submissions = sum(card["member_count"] for card in cards)
        ongoing_cards = sum(card["representative"].get("status") in {"Under Review", "Revision"} for card in cards)
        ongoing_submissions = sum(
            member.get("status") in {"Under Review", "Revision"} for card in cards for member in card["members"]
        )
        return {
            "card_count": len(cards),
            "submission_count": submissions,
            "ongoing_card_count": ongoing_cards,
            "ongoing_submission_count": ongoing_submissions,
        }

    def starred_update_target(self, owner_user_id: int) -> dict[str, Any]:
        """Snapshot the distinct journals containing the owner's current starred rows."""
        require_owner(owner_user_id)
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT current.journal_config_id,j.enabled
                   FROM manuscript_stars s
                   JOIN manuscript_records current
                     ON current.id=(
                       SELECT m.id FROM manuscript_records m
                       WHERE m.owner_user_id=s.owner_user_id
                         AND m.original_id=s.original_id
                       ORDER BY m.version_number DESC,m.id DESC LIMIT 1
                     )
                   LEFT JOIN journal_configs j
                     ON j.id=current.journal_config_id
                    AND j.owner_user_id=current.owner_user_id
                   WHERE s.owner_user_id=?
                     AND NOT EXISTS (
                       SELECT 1 FROM manuscript_hides hidden
                       WHERE hidden.owner_user_id=s.owner_user_id
                         AND hidden.original_id=s.original_id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM submission_journey_members member
                       JOIN submission_journeys journey
                         ON journey.id=member.journey_id
                        AND journey.owner_user_id=member.owner_user_id
                       WHERE member.owner_user_id=s.owner_user_id
                         AND member.original_id=s.original_id
                         AND journey.hidden_at IS NOT NULL
                     )""",
                (owner_user_id,),
            ).fetchall()
        config_ids = sorted({int(row["journal_config_id"]) for row in rows if row["journal_config_id"] is not None})
        enabled_ids = sorted(
            {
                int(row["journal_config_id"])
                for row in rows
                if row["journal_config_id"] is not None and bool(row["enabled"])
            }
        )
        return {
            "starred_manuscript_count": len(rows),
            "journal_config_ids": config_ids,
            "enabled_journal_config_ids": enabled_ids,
        }

    def working_update_target(self, owner_user_id: int) -> dict[str, Any]:
        """Snapshot journals containing the owner's visible ongoing manuscripts."""
        items = self.email_items(owner_user_id, "working")
        config_ids = sorted(
            {int(item["journal_config_id"]) for item in items if item.get("journal_config_id") is not None}
        )
        enabled = {row["id"] for row in self.journal_configs_by_ids(owner_user_id, config_ids, enabled_only=True)}
        return {
            "working_manuscript_count": len(items),
            "journal_config_ids": config_ids,
            "enabled_journal_config_ids": sorted(enabled),
        }

    def journal_configs_by_ids(
        self, owner_user_id: int, config_ids: list[int], *, enabled_only: bool = True
    ) -> list[dict]:
        require_owner(owner_user_id)
        ids = sorted(set(config_ids))
        if not ids:
            return []
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
            raise ValueError("journal config IDs must be positive integers")
        placeholders = ",".join("?" for _ in ids)
        sql = f"SELECT * FROM journal_configs WHERE owner_user_id=? AND id IN ({placeholders})"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY journal_name,id"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, (owner_user_id, *ids))]

    def toggle_star(self, owner_user_id: int, original_id: str) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM manuscript_records WHERE owner_user_id=? AND original_id=? LIMIT 1",
                (owner_user_id, original_id),
            ).fetchone()
            if not exists:
                raise LookupError("manuscript not found")
            starred = conn.execute(
                "SELECT 1 FROM manuscript_stars WHERE owner_user_id=? AND original_id=?",
                (owner_user_id, original_id),
            ).fetchone()
            if starred:
                conn.execute(
                    "DELETE FROM manuscript_stars WHERE owner_user_id=? AND original_id=?", (owner_user_id, original_id)
                )
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO manuscript_stars(owner_user_id,original_id) VALUES(?,?)", (owner_user_id, original_id)
            )
            conn.commit()
            return True

    def starred_ids(self, owner_user_id: int) -> set[str]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT original_id FROM manuscript_stars WHERE owner_user_id=?", (owner_user_id,)
                )
            }

    def acknowledge(self, owner_user_id: int, original_id: str) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT status,detailed_status FROM manuscript_records
                   WHERE owner_user_id=? AND original_id=?
                   ORDER BY version_number DESC,id DESC LIMIT 1""",
                (owner_user_id, original_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            conn.execute(
                """INSERT INTO manuscript_acks(owner_user_id,original_id,ack_status,ack_detailed_status)
                   VALUES(?,?,?,?) ON CONFLICT(owner_user_id,original_id) DO UPDATE SET
                   ack_status=excluded.ack_status,ack_detailed_status=excluded.ack_detailed_status,updated_at=CURRENT_TIMESTAMP""",
                (owner_user_id, original_id, row["status"], row["detailed_status"]),
            )
            conn.commit()
            return True

    def statistics(self, owner_user_id: int) -> dict[str, int]:
        cards = self.manuscript_cards(owner_user_id, view="visible")
        items = [card["representative"] for card in cards]
        return {
            "total": len(items),
            "submissions": sum(card["member_count"] for card in cards),
            "accepted": sum(item["status"] == "Accept" for item in items),
            "under_review": sum(item["status"] == "Under Review" for item in items),
            "revision": sum(item["status"] == "Revision" for item in items),
            "ongoing": sum(item["status"] in {"Under Review", "Revision"} for item in items),
        }

    def log_query(
        self,
        owner_user_id: int,
        journal_config_id: int | None,
        journal: str,
        provider: str,
        success: bool,
        manuscripts_found: int,
        error_message: str = "",
    ) -> int:
        require_owner(owner_user_id)
        sanitized = _safe_tracking_error(error_message)
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO query_logs(owner_user_id,journal_config_id,journal,provider,success,manuscripts_found,error_message)
                   VALUES(?,?,?,?,?,?,?)""",
                (owner_user_id, journal_config_id, journal, provider, int(success), manuscripts_found, sanitized),
            )
            return cursor.lastrowid

    def audit(
        self,
        action: str,
        outcome: str,
        actor_user_id: int | None = None,
        target_user_id: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_events(actor_user_id,target_user_id,action,outcome,metadata_json) VALUES(?,?,?,?,?)",
                (
                    actor_user_id,
                    target_user_id,
                    action,
                    outcome,
                    json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
                ),
            )
            return cursor.lastrowid

    # Tenant-scoped automation foundation. Public operations require an owner,
    # and mutations repeat that owner in their predicates and foreign keys.
    def save_automation_job(self, owner_user_id: int, data: dict[str, Any], job_id: int | None = None) -> int:
        require_owner(owner_user_id)
        kind = data["kind"]
        if kind not in {"update", "email"}:
            raise ValueError("invalid automation job kind")
        payload = dict(data.get("payload") or {})
        recipients = list(dict.fromkeys(str(v).strip() for v in data.get("recipients", []) if str(v).strip()))
        timezone_name = validate_timezone(data.get("timezone") or DEFAULT_TIMEZONE)
        values = (
            str(data["name"]).strip(),
            kind,
            str(data["cron_expression"]).strip(),
            timezone_name,
            int(bool(data.get("enabled", True))),
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        with self.connection(immediate=True) as conn:
            if job_id is None:
                job_id = conn.execute(
                    "INSERT INTO automation_jobs(owner_user_id,name,kind,cron_expression,timezone,enabled,payload_json) VALUES(?,?,?,?,?,?,?)",
                    (owner_user_id,) + values,
                ).lastrowid
            else:
                result = conn.execute(
                    "UPDATE automation_jobs SET name=?,kind=?,cron_expression=?,timezone=?,enabled=?,payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
                    values + (job_id, owner_user_id),
                )
                if result.rowcount != 1:
                    raise LookupError("automation job not found")
                conn.execute(
                    "DELETE FROM automation_email_recipients WHERE job_id=? AND owner_user_id=?",
                    (job_id, owner_user_id),
                )
            for address in recipients:
                conn.execute(
                    "INSERT INTO automation_email_recipients(owner_user_id,job_id,email) VALUES(?,?,?)",
                    (owner_user_id, job_id, address),
                )
            conn.commit()
            return int(job_id)

    def get_automation_job(self, owner_user_id: int, job_id: int):
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM automation_jobs WHERE id=? AND owner_user_id=?", (job_id, owner_user_id)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["recipients"] = [
                r[0]
                for r in conn.execute(
                    "SELECT email FROM automation_email_recipients WHERE owner_user_id=? AND job_id=? ORDER BY id",
                    (owner_user_id, job_id),
                )
            ]
            return item

    def list_automation_jobs(self, owner_user_id: int) -> list[dict]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM automation_jobs WHERE owner_user_id=? ORDER BY kind,id", (owner_user_id,)
                )
            ]
        return [job for job in (self.get_automation_job(owner_user_id, job_id) for job_id in ids) if job]

    def delete_automation_job(self, owner_user_id: int, job_id: int) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            result = conn.execute("DELETE FROM automation_jobs WHERE id=? AND owner_user_id=?", (job_id, owner_user_id))
            conn.commit()
            return result.rowcount == 1

    def save_user_smtp(self, owner_user_id: int, data: dict[str, Any]) -> None:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            conn.execute(
                """INSERT INTO user_smtp_configs(owner_user_id,host,port,security_mode,username,sender_address,sender_name,credential_ref)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_user_id) DO UPDATE SET host=excluded.host,port=excluded.port,
                security_mode=excluded.security_mode,username=excluded.username,sender_address=excluded.sender_address,
                sender_name=excluded.sender_name,credential_ref=excluded.credential_ref,updated_at=CURRENT_TIMESTAMP""",
                (
                    owner_user_id,
                    data["host"],
                    int(data["port"]),
                    data["security_mode"],
                    data.get("username", ""),
                    data["sender_address"],
                    data.get("sender_name", ""),
                    data.get("credential_ref"),
                ),
            )
            conn.commit()

    def get_user_smtp(self, owner_user_id: int):
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM user_smtp_configs WHERE owner_user_id=?", (owner_user_id,)).fetchone()
            return dict(row) if row else None

    def get_user_smtp_source(self, owner_user_id: int) -> str:
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute("SELECT smtp_source FROM users WHERE id=?", (owner_user_id,)).fetchone()
        if not row:
            raise LookupError("user not found")
        return str(row["smtp_source"])

    def set_user_smtp_source(self, owner_user_id: int, source: str) -> None:
        require_owner(owner_user_id)
        if source not in {"personal", "system"}:
            raise ValueError("invalid SMTP source")
        if source == "system" and not self.get_system_smtp_health()["selectable"]:
            raise SmtpUnavailableError("system_smtp_unavailable", "system")
        with self.connection(immediate=True) as conn:
            result = conn.execute(
                "UPDATE users SET smtp_source=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (source, owner_user_id),
            )
            if result.rowcount != 1:
                raise LookupError("user not found")

    def get_system_smtp(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM system_smtp_config WHERE singleton=1").fetchone()
        return dict(row) if row else None

    @staticmethod
    def _system_smtp_health(config: dict | None) -> dict:
        configured = bool(
            config
            and str(config.get("host") or "").strip()
            and config.get("port")
            and str(config.get("sender_address") or "").strip()
            and config.get("credential_ref")
        )
        tested_at = config.get("last_tested_at") if config else None
        if not configured:
            health = "unconfigured"
            message = "管理员尚未配置系统发送通道"
        elif not config.get("last_test_status") or config.get("last_tested_generation") is None:
            health = "untested"
            message = "系统发送通道尚未由管理员测试"
        elif int(config["last_tested_generation"]) != int(config["config_generation"]):
            health = "stale"
            message = "管理员已修改配置，需要重新测试"
        elif config["last_test_status"] == "success":
            health = "success"
            message = "系统发送通道当前配置测试成功"
        else:
            health = "failed"
            message = "系统发送通道当前配置测试失败"
        return {
            "configured": configured,
            "health": health,
            "selectable": health == "success",
            "tested_at": tested_at,
            "message": message,
        }

    def get_system_smtp_health(self) -> dict:
        return self._system_smtp_health(self.get_system_smtp())

    def save_system_smtp(self, actor_user_id: int, data: dict[str, Any]) -> dict:
        require_owner(actor_user_id)
        existing = self.get_system_smtp()
        generation = int(existing.get("config_generation") or 1) + 1 if existing else 1
        with self.connection(immediate=True) as conn:
            conn.execute(
                """INSERT INTO system_smtp_config(
                       singleton,host,port,security_mode,username,sender_address,sender_name,credential_ref,
                       config_generation,last_test_status,last_tested_generation,last_tested_at,last_test_error_code,
                       updated_by_user_id)
                   VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       host=excluded.host,port=excluded.port,security_mode=excluded.security_mode,
                       username=excluded.username,sender_address=excluded.sender_address,
                       sender_name=excluded.sender_name,credential_ref=excluded.credential_ref,
                       config_generation=excluded.config_generation,
                       updated_by_user_id=excluded.updated_by_user_id,updated_at=CURRENT_TIMESTAMP""",
                (
                    data["host"],
                    int(data["port"]),
                    data["security_mode"],
                    data.get("username", ""),
                    data["sender_address"],
                    data.get("sender_name", ""),
                    data.get("credential_ref"),
                    generation,
                    existing.get("last_test_status") if existing else None,
                    existing.get("last_tested_generation") if existing else None,
                    existing.get("last_tested_at") if existing else None,
                    existing.get("last_test_error_code") if existing else None,
                    actor_user_id,
                ),
            )
        return self.get_system_smtp()

    def record_system_smtp_test(
        self,
        generation: int,
        status: str,
        safe_error_code: str | None = None,
    ) -> bool:
        if status not in {"success", "failed"}:
            raise ValueError("invalid SMTP test status")
        with self.connection(immediate=True) as conn:
            current = conn.execute(
                "SELECT config_generation,last_tested_generation FROM system_smtp_config WHERE singleton=1"
            ).fetchone()
            if not current:
                return False
            previous = current["last_tested_generation"]
            if previous is not None and int(previous) > int(generation):
                return False
            result = conn.execute(
                """UPDATE system_smtp_config SET last_test_status=?,last_tested_generation=?,
                       last_tested_at=CURRENT_TIMESTAMP,last_test_error_code=? WHERE singleton=1""",
                (status, int(generation), safe_error_code if status == "failed" else None),
            )
            return result.rowcount == 1

    def resolve_automation_smtp(self, owner_user_id: int) -> dict:
        source = self.get_user_smtp_source(owner_user_id)
        if source == "personal":
            config = self.get_user_smtp(owner_user_id)
            if not config or not config.get("credential_ref"):
                raise SmtpUnavailableError("personal_smtp_unavailable", "personal")
        else:
            config = self.get_system_smtp()
            if not self._system_smtp_health(config)["selectable"]:
                raise SmtpUnavailableError("system_smtp_unavailable", "system")
        resolved = dict(config)
        resolved["smtp_source"] = source
        return resolved

    def _normalize_run_payload(self, owner_user_id: int, kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if kind == "email":
            if payload:
                raise ValueError("email run requests do not accept payload")
            return {}
        data = dict(payload or {})
        if not data:
            return {"update_scope": "all"}
        if set(data) - {"update_scope", "journal_config_ids"}:
            raise ValueError("invalid update request payload")
        scope = data.get("update_scope")
        if scope == "all":
            if "journal_config_ids" in data:
                raise ValueError("all-journal update cannot specify journal IDs")
            return {"update_scope": "all"}
        if scope not in {"journal", "starred", "working"}:
            raise ValueError("invalid update scope")
        raw_ids = data.get("journal_config_ids")
        if not isinstance(raw_ids, list):
            raise ValueError(f"{scope} update requires journal IDs")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in raw_ids):
            raise ValueError("journal config IDs must be positive integers")
        ids = sorted(set(raw_ids))
        if not ids:
            raise ValueError(f"{scope} update requires journal IDs")
        if scope == "journal" and len(ids) != 1:
            raise ValueError("journal update requires exactly one journal ID")
        owned = {row["id"] for row in self.journal_configs_by_ids(owner_user_id, ids, enabled_only=False)}
        if owned != set(ids):
            raise PermissionError("journal configuration does not belong to tenant")
        return {"update_scope": scope, "journal_config_ids": ids}

    @staticmethod
    def _decode_run_payload(value: str | None, kind: str) -> dict[str, Any]:
        try:
            payload = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid persisted run payload") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid persisted run payload")
        if kind == "update" and not payload:
            return {"update_scope": "all"}
        return payload

    @staticmethod
    def _update_coverage(requested: dict[str, Any], active: dict[str, Any]) -> str:
        requested_scope = requested.get("update_scope", "all")
        active_scope = active.get("update_scope", "all")
        if requested_scope == active_scope == "all":
            return "same"
        if requested_scope in {"journal", "starred", "working"} and active_scope == "all":
            return "superset"
        if requested_scope == "journal" and active_scope in {"journal", "starred", "working"}:
            requested_ids = set(requested.get("journal_config_ids") or [])
            active_ids = set(active.get("journal_config_ids") or [])
            if requested_ids and requested_ids <= active_ids:
                return "same" if requested_scope == active_scope and requested_ids == active_ids else "superset"
        if (
            requested_scope == active_scope
            and requested_scope in {"starred", "working"}
            and requested.get("journal_config_ids") == active.get("journal_config_ids")
        ):
            return "same"
        return "different"

    def enqueue_run(
        self,
        owner_user_id: int,
        kind: str,
        scheduled_for: str,
        source: str = "manual",
        job_id: int | None = None,
        requested_by_user_id: int | None = None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        require_owner(owner_user_id)
        if kind not in {"update", "email"} or source not in {"schedule", "manual", "retry"}:
            raise ValueError("invalid automation run request")
        if requested_by_user_id is not None and requested_by_user_id != owner_user_id:
            raise PermissionError("manual runs may only be requested for the current tenant")
        normalized_payload = self._normalize_run_payload(owner_user_id, kind, payload)
        payload_json = json.dumps(normalized_payload, separators=(",", ":"), sort_keys=True)
        with self.connection(immediate=True) as conn:
            if job_id is not None:
                job = conn.execute(
                    "SELECT kind FROM automation_jobs WHERE id=? AND owner_user_id=?",
                    (job_id, owner_user_id),
                ).fetchone()
                if not job:
                    raise LookupError("automation job not found")
                if job["kind"] != kind:
                    raise ValueError("automation job kind does not match request")
            elif kind == "email":
                raise ValueError("email run requires a job")

            if kind == "update":
                active = conn.execute(
                    """SELECT * FROM automation_run_requests
                       WHERE owner_user_id=? AND kind='update'
                         AND status IN ('queued','claimed','running')
                       ORDER BY scheduled_for,id LIMIT 1""",
                    (owner_user_id,),
                ).fetchone()
            else:
                active = conn.execute(
                    """SELECT * FROM automation_run_requests
                       WHERE owner_user_id=? AND job_id=? AND kind='email'
                         AND status IN ('queued','claimed','running')
                       ORDER BY scheduled_for,id LIMIT 1""",
                    (owner_user_id, job_id),
                ).fetchone()
            if active:
                active_item = dict(active)
                active_payload = self._decode_run_payload(active_item.get("payload_json"), kind)
                active_item["payload"] = active_payload
                return {
                    "created": False,
                    "coverage": self._update_coverage(normalized_payload, active_payload)
                    if kind == "update"
                    else "same",
                    "request": active_item,
                }

            try:
                request_id = conn.execute(
                    """INSERT INTO automation_run_requests
                       (owner_user_id,job_id,kind,source,scheduled_for,status,requested_by_user_id,payload_json)
                       VALUES(?,?,?,?,?,'queued',?,?)""",
                    (owner_user_id, job_id, kind, source, scheduled_for, requested_by_user_id, payload_json),
                ).lastrowid
            except sqlite3.IntegrityError:
                if kind == "update":
                    active = conn.execute(
                        """SELECT * FROM automation_run_requests
                           WHERE owner_user_id=? AND kind='update'
                             AND status IN ('queued','claimed','running')
                           ORDER BY scheduled_for,id LIMIT 1""",
                        (owner_user_id,),
                    ).fetchone()
                else:
                    active = conn.execute(
                        """SELECT * FROM automation_run_requests
                           WHERE owner_user_id=? AND job_id=? AND kind='email'
                             AND status IN ('queued','claimed','running')
                           ORDER BY scheduled_for,id LIMIT 1""",
                        (owner_user_id, job_id),
                    ).fetchone()
                if active:
                    active_item = dict(active)
                    active_payload = self._decode_run_payload(active_item.get("payload_json"), kind)
                    active_item["payload"] = active_payload
                    return {
                        "created": False,
                        "coverage": self._update_coverage(normalized_payload, active_payload)
                        if kind == "update"
                        else "same",
                        "request": active_item,
                    }
                raise
            request = conn.execute(
                "SELECT * FROM automation_run_requests WHERE id=? AND owner_user_id=?",
                (request_id, owner_user_id),
            ).fetchone()
            request_item = dict(request)
            request_item["payload"] = normalized_payload
            return {"created": True, "coverage": "new", "request": request_item}

    def _project_run_summary(
        self,
        owner_user_id: int,
        value: object,
        *,
        status: str,
        kind: str | None = None,
        historical: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        require_owner(owner_user_id)
        if conn is None:
            with self.connection() as owned_conn:
                return self._project_run_summary(
                    owner_user_id,
                    value,
                    status=status,
                    kind=kind,
                    historical=historical,
                    conn=owned_conn,
                )
        names = {
            int(row["id"]): str(row["journal_name"])
            for row in conn.execute(
                "SELECT id,journal_name FROM journal_configs WHERE owner_user_id=?",
                (owner_user_id,),
            )
        }
        return project_automation_summary(
            value,
            status=status,
            kind=kind,
            journal_names_by_id=names,
            historical=historical,
        )

    def list_automation_runs(self, owner_user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        require_owner(owner_user_id)
        safe_limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT r.id AS run_id,r.request_id,q.source,
                          COALESCE(j.kind,q.kind) AS kind,r.status,r.started_at,r.finished_at,
                          r.summary_json,j.name AS job_name
                   FROM automation_job_runs r
                   LEFT JOIN automation_jobs j ON j.id=r.job_id AND j.owner_user_id=r.owner_user_id
                   LEFT JOIN automation_run_requests q ON q.id=r.request_id AND q.owner_user_id=r.owner_user_id
                   WHERE r.owner_user_id=? ORDER BY r.id DESC LIMIT ?""",
                (owner_user_id, safe_limit),
            ).fetchall()
            results = []
            for row in rows:
                try:
                    raw = json.loads(row["summary_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    raw = None
                results.append(
                    {
                        "source": row["source"],
                        "job_name": row["job_name"] or "手动",
                        "kind": row["kind"],
                        "started_at": row["started_at"],
                        "finished_at": row["finished_at"],
                        "status": row["status"],
                        "summary": self._project_run_summary(
                            owner_user_id,
                            raw,
                            status=str(row["status"]),
                            kind=row["kind"],
                            historical=True,
                            conn=conn,
                        ),
                    }
                )
            return results

    def get_run_request(self, owner_user_id: int, request_id: int):
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute(
                """SELECT q.*,r.id AS run_id,r.status AS result,r.summary_json,
                          r.started_at,r.finished_at
                   FROM automation_run_requests q
                   LEFT JOIN automation_job_runs r
                     ON r.request_id=q.id AND r.owner_user_id=q.owner_user_id
                   WHERE q.id=? AND q.owner_user_id=?
                   ORDER BY r.id DESC LIMIT 1""",
                (request_id, owner_user_id),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = self._decode_run_payload(item.pop("payload_json", "{}"), item["kind"])
            raw_summary = item.pop("summary_json") or "{}"
            try:
                decoded_summary = json.loads(raw_summary)
            except (TypeError, json.JSONDecodeError):
                decoded_summary = None
            item["summary"] = self._project_run_summary(
                owner_user_id,
                decoded_summary,
                status=str(item.get("result") or item.get("status") or "failed"),
                kind=item.get("kind"),
                historical=True,
                conn=conn,
            )
            item["active"] = item["status"] in {"queued", "claimed", "running"}
            return item

    def active_update_request(self, owner_user_id: int):
        require_owner(owner_user_id)
        with self.connection() as conn:
            row = conn.execute(
                """SELECT id FROM automation_run_requests
                   WHERE owner_user_id=? AND kind='update'
                     AND status IN ('queued','claimed','running')
                   ORDER BY scheduled_for,id LIMIT 1""",
                (owner_user_id,),
            ).fetchone()
        return self.get_run_request(owner_user_id, row["id"]) if row else None

    def active_email_requests(self, owner_user_id: int) -> dict[int, dict]:
        require_owner(owner_user_id)
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id,job_id FROM automation_run_requests
                   WHERE owner_user_id=? AND kind='email' AND job_id IS NOT NULL
                     AND status IN ('queued','claimed','running')
                   ORDER BY scheduled_for,id""",
                (owner_user_id,),
            ).fetchall()
        return {row["job_id"]: request for row in rows if (request := self.get_run_request(owner_user_id, row["id"]))}

    def fail_run_request(self, owner_user_id: int, request_id: int, summary: dict | None = None) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            request = conn.execute(
                "SELECT job_id,kind FROM automation_run_requests WHERE id=? AND owner_user_id=?",
                (request_id, owner_user_id),
            ).fetchone()
            if not request:
                return False
            run = conn.execute(
                "SELECT id FROM automation_job_runs WHERE request_id=? AND owner_user_id=? ORDER BY id DESC LIMIT 1",
                (request_id, owner_user_id),
            ).fetchone()
            compact_summary = self._project_run_summary(
                owner_user_id,
                summary or {},
                status="failed",
                kind=request["kind"],
                conn=conn,
            )
            summary_text = json.dumps(compact_summary, separators=(",", ":"), sort_keys=True)
            if run:
                conn.execute(
                    """UPDATE automation_job_runs SET status='failed',summary_json=?,finished_at=CURRENT_TIMESTAMP
                       WHERE id=? AND owner_user_id=?""",
                    (summary_text, run["id"], owner_user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO automation_job_runs
                       (owner_user_id,job_id,request_id,status,summary_json,finished_at)
                       VALUES(?,?,?,'failed',?,CURRENT_TIMESTAMP)""",
                    (
                        owner_user_id,
                        request["job_id"],
                        request_id,
                        summary_text,
                    ),
                )
            conn.execute(
                """UPDATE automation_run_requests
                   SET status='failed',claimed_by=NULL,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (request_id, owner_user_id),
            )
            return True

    def claim_next_run(self, owner_user_id: int, worker_id: str, lease_seconds: int = 300, now: datetime | None = None):
        require_owner(owner_user_id)
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker and positive lease required")
        now = now or datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM automation_run_requests WHERE owner_user_id=? AND scheduled_for<=?
                AND (status='queued' OR (status IN ('claimed','running') AND lease_expires_at<=?))
                ORDER BY scheduled_for,id LIMIT 1""",
                (owner_user_id, now_text, now_text),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            claim_token = secrets.token_urlsafe(32)
            generation = int(row["claim_generation"] or 0) + 1
            result = conn.execute(
                """UPDATE automation_run_requests
                SET status='claimed',claimed_by=?,lease_expires_at=?,claim_generation=?,claim_token=?,heartbeat_at=?,updated_at=?
                WHERE id=? AND owner_user_id=? AND (status='queued' OR lease_expires_at<=?)""",
                (worker_id, lease, generation, claim_token, now_text, now_text, row["id"], owner_user_id, now_text),
            )
            if result.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            claimed = dict(row)
            claimed["payload"] = self._decode_run_payload(claimed.pop("payload_json", "{}"), claimed["kind"])
            claimed.update(
                status="claimed",
                claimed_by=worker_id,
                lease_expires_at=lease,
                claim_generation=generation,
                claim_token=claim_token,
                heartbeat_at=now_text,
            )
            return claimed

    def heartbeat_run_claim(
        self,
        owner_user_id: int,
        request_id: int,
        worker_id: str,
        claim_token: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> bool:
        require_owner(owner_user_id)
        now = now or datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection(immediate=True) as conn:
            result = conn.execute(
                """UPDATE automation_run_requests
                   SET heartbeat_at=?,lease_expires_at=?,updated_at=?
                   WHERE id=? AND owner_user_id=? AND claimed_by=? AND claim_token=?
                     AND status IN ('claimed','running') AND lease_expires_at>?""",
                (
                    now_text,
                    lease,
                    now_text,
                    request_id,
                    owner_user_id,
                    worker_id,
                    claim_token,
                    now_text,
                ),
            )
            return result.rowcount == 1

    def start_claimed_run(
        self,
        owner_user_id: int,
        request_id: int,
        worker_id: str,
        claim_token: str | None = None,
    ) -> int:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            request = conn.execute(
                """SELECT * FROM automation_run_requests
                   WHERE id=? AND owner_user_id=? AND status='claimed' AND claimed_by=?
                     AND (? IS NULL OR claim_token=?)""",
                (request_id, owner_user_id, worker_id, claim_token, claim_token),
            ).fetchone()
            if not request:
                raise LookupError("claimed run request not found")
            conn.execute(
                """UPDATE automation_run_requests
                   SET status='running',heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (request_id, owner_user_id),
            )
            run_id = conn.execute(
                "INSERT INTO automation_job_runs(owner_user_id,job_id,request_id,status) VALUES(?,?,?,'running')",
                (owner_user_id, request["job_id"], request_id),
            ).lastrowid
            conn.commit()
            return run_id

    def finish_run(
        self,
        owner_user_id: int,
        run_id: int,
        status: str,
        summary: dict | None = None,
        claim_token: str | None = None,
    ) -> bool:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            run = conn.execute(
                """SELECT r.request_id,q.kind
                   FROM automation_job_runs r
                   JOIN automation_run_requests q
                     ON q.id=r.request_id AND q.owner_user_id=r.owner_user_id
                   WHERE r.id=? AND r.owner_user_id=? AND (? IS NULL OR q.claim_token=?)""",
                (run_id, owner_user_id, claim_token, claim_token),
            ).fetchone()
            if not run:
                return False
            compact_summary = self._project_run_summary(
                owner_user_id,
                summary or {},
                status=status,
                kind=run["kind"],
                conn=conn,
            )
            conn.execute(
                "UPDATE automation_job_runs SET status=?,summary_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=? AND owner_user_id=?",
                (
                    status,
                    json.dumps(compact_summary, separators=(",", ":"), sort_keys=True),
                    run_id,
                    owner_user_id,
                ),
            )
            request_status = "finished" if status in {"success", "partial", "finished", "no_changes"} else "failed"
            conn.execute(
                """UPDATE automation_run_requests
                   SET status=?,claimed_by=NULL,lease_expires_at=NULL,claim_token=NULL,
                       heartbeat_at=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND owner_user_id=?""",
                (request_status, run["request_id"], owner_user_id),
            )
            conn.commit()
            return True

    def unread_status_ids(self, owner_user_id: int, items: list[dict] | None = None) -> set[str]:
        require_owner(owner_user_id)
        current_items = items if items is not None else self.latest_manuscripts(owner_user_id)
        with self.connection() as conn:
            acknowledgements = {
                row["original_id"]: (row["ack_status"], row["ack_detailed_status"])
                for row in conn.execute(
                    "SELECT original_id,ack_status,ack_detailed_status FROM manuscript_acks WHERE owner_user_id=?",
                    (owner_user_id,),
                )
            }
        return {
            item["original_id"]
            for item in current_items
            if item["original_id"] in acknowledgements
            and acknowledgements[item["original_id"]] != (item.get("status") or "", item.get("detailed_status") or "")
        }

    def email_items(self, owner_user_id: int, scope: str = "all") -> list[dict]:
        require_owner(owner_user_id)
        if scope not in {"all", "starred", "working"}:
            raise ValueError("invalid manuscript scope")
        hidden = self.effectively_hidden_ids(owner_user_id)
        items = [item for item in self._latest_submission_rows(owner_user_id) if item["original_id"] not in hidden]
        if scope == "starred":
            stars = self.starred_ids(owner_user_id)
            items = [item for item in items if item["original_id"] in stars]
        elif scope == "working":
            items = [item for item in items if item.get("status") in {"Under Review", "Revision"}]
        return items

    def create_email_delivery(
        self,
        owner_user_id: int,
        job_id: int,
        items: list[dict],
        message_id: str,
        token_hash_value: str | None = None,
        expires_at: str | None = None,
        run_id: int | None = None,
    ) -> int:
        require_owner(owner_user_id)
        with self.connection(immediate=True) as conn:
            if not conn.execute(
                "SELECT 1 FROM automation_jobs WHERE id=? AND owner_user_id=? AND kind='email'", (job_id, owner_user_id)
            ).fetchone():
                raise LookupError("email job not found")
            delivery_id = conn.execute(
                "INSERT INTO automation_email_deliveries(owner_user_id,job_id,run_id,status,message_id) VALUES(?,?,?,'prepared',?)",
                (owner_user_id, job_id, run_id, message_id),
            ).lastrowid
            for item in items:
                conn.execute(
                    "INSERT INTO automation_email_delivery_items(owner_user_id,delivery_id,original_id,delivered_fingerprint) VALUES(?,?,?,?)",
                    (owner_user_id, delivery_id, item["original_id"], manuscript_fingerprint(item)),
                )
            if token_hash_value and expires_at:
                conn.execute(
                    "INSERT INTO automation_ack_tokens(owner_user_id,delivery_id,token_hash,scope,expires_at) VALUES(?,?,?,?,?)",
                    (owner_user_id, delivery_id, token_hash_value, "delivery", expires_at),
                )
            elif token_hash_value or expires_at:
                raise ValueError("acknowledgement token and expiry must be provided together")
            conn.commit()
            return delivery_id

    def mark_email_delivery(self, owner_user_id: int, delivery_id: int, status: str) -> bool:
        require_owner(owner_user_id)
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE automation_email_deliveries SET status=?,sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END WHERE id=? AND owner_user_id=?",
                (status, status, delivery_id, owner_user_id),
            )
            return result.rowcount == 1

    def consume_ack_token(self, owner_user_id: int, raw_token: str, now: datetime | None = None):
        require_owner(owner_user_id)
        now_text = (now or datetime.now(timezone.utc)).isoformat()
        digest = token_hash(raw_token)
        with self.connection(immediate=True) as conn:
            token = conn.execute(
                """SELECT t.* FROM automation_ack_tokens t JOIN automation_email_deliveries d
                ON d.id=t.delivery_id AND d.owner_user_id=t.owner_user_id
                WHERE t.owner_user_id=? AND t.token_hash=? AND t.consumed_at IS NULL AND t.expires_at>? AND d.status='sent'""",
                (owner_user_id, digest, now_text),
            ).fetchone()
            if not token:
                conn.rollback()
                return None
            items = conn.execute(
                "SELECT * FROM automation_email_delivery_items WHERE delivery_id=? AND owner_user_id=?",
                (token["delivery_id"], owner_user_id),
            ).fetchall()
            acknowledged = skipped = 0
            for item in items:
                record = conn.execute(
                    """SELECT * FROM manuscript_records
                       WHERE owner_user_id=? AND original_id=?
                       ORDER BY version_number DESC,id DESC LIMIT 1""",
                    (owner_user_id, item["original_id"]),
                ).fetchone()
                if not record or manuscript_fingerprint(dict(record)) != item["delivered_fingerprint"]:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO manuscript_acks(owner_user_id,original_id,ack_status,ack_detailed_status) VALUES(?,?,?,?)
                    ON CONFLICT(owner_user_id,original_id) DO UPDATE SET ack_status=excluded.ack_status,
                    ack_detailed_status=excluded.ack_detailed_status,updated_at=CURRENT_TIMESTAMP""",
                    (owner_user_id, record["original_id"], record["status"], record["detailed_status"]),
                )
                acknowledged += 1
            conn.execute(
                "UPDATE automation_ack_tokens SET consumed_at=? WHERE id=? AND owner_user_id=?",
                (now_text, token["id"], owner_user_id),
            )
            conn.commit()
            return {"acknowledged": acknowledged, "skipped_newer": skipped}


def manuscript_fingerprint(record: dict[str, Any]) -> str:
    import hashlib

    fields = (
        "provider",
        "journal_config_id",
        "original_id",
        "manuscript_id",
        "version_number",
        "status",
        "status_text",
        "detailed_status",
    )
    payload = {field: str(record.get(field) or "").strip() for field in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
