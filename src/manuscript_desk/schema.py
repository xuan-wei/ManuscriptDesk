import ipaddress
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import APP_SCHEMA_VERSION
from .automation_summaries import project_automation_summary

SCHEMA_SQL = """
CREATE TABLE app_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    application_compatibility TEXT NOT NULL,
    migration_id TEXT NOT NULL,
    migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    legacy_username TEXT UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    password_scheme TEXT NOT NULL CHECK(password_scheme IN ('argon2id','legacy_sha256','activation_pending')),
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    email_verified_at TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    smtp_source TEXT NOT NULL DEFAULT 'personal' CHECK(smtp_source IN ('personal','system')),
    disabled_at TEXT,
    purge_eligible_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE app_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    self_registration_enabled INTEGER NOT NULL DEFAULT 0 CHECK(self_registration_enabled IN (0,1)),
    updated_by_user_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);
CREATE TABLE user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_secret TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_sessions_user_active ON user_sessions(user_id, revoked_at, idle_expires_at);
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    secret_ref TEXT,
    label TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_api_tokens_owner ON api_tokens(owner_user_id, revoked_at);
CREATE TABLE user_shortcut_settings (
    owner_user_id INTEGER PRIMARY KEY,
    base_url TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE email_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL,
    code_hmac TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_email_challenges_active ON email_challenges(email, purpose, used_at, expires_at);
CREATE TABLE activation_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK(purpose IN ('invite','admin_email_setup')),
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by_user_id) REFERENCES users(id)
);
CREATE INDEX idx_activation_tokens_target ON activation_tokens(target_user_id, purpose, consumed_at, revoked_at);
CREATE TABLE rate_limit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_rate_limit_events_lookup ON rate_limit_events(action, subject_hash, occurred_at);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    target_user_id INTEGER,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(actor_user_id) REFERENCES users(id),
    FOREIGN KEY(target_user_id) REFERENCES users(id)
);
CREATE TABLE deleted_user_tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_hash TEXT NOT NULL UNIQUE,
    purged_by_user_id INTEGER NOT NULL,
    disabled_at TEXT,
    purged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    app_version TEXT NOT NULL,
    FOREIGN KEY(purged_by_user_id) REFERENCES users(id)
);
CREATE TABLE journal_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    journal_name TEXT NOT NULL,
    journal_url TEXT NOT NULL,
    short_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'scholarone',
    site_key TEXT NOT NULL DEFAULT '',
    credential_ref TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_user_id, journal_name),
    UNIQUE(id, owner_user_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_journal_configs_owner_enabled ON journal_configs(owner_user_id, enabled);
CREATE TABLE manuscript_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    journal_config_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    journal TEXT NOT NULL DEFAULT '',
    version_number INTEGER NOT NULL DEFAULT 1,
    author_role TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    detailed_status TEXT NOT NULL DEFAULT '',
    editor TEXT NOT NULL DEFAULT '',
    submitted_date TEXT NOT NULL DEFAULT '',
    decision_date TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'scholarone',
    source_original_id TEXT NOT NULL DEFAULT '',
    status_date TEXT NOT NULL DEFAULT '',
    final_disposition TEXT NOT NULL DEFAULT '',
    workflow_folder TEXT NOT NULL DEFAULT '',
    authorship TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_user_id, original_id, manuscript_id, recorded_at),
    FOREIGN KEY(journal_config_id, owner_user_id) REFERENCES journal_configs(id, owner_user_id) ON DELETE RESTRICT,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_records_owner_original ON manuscript_records(owner_user_id, original_id, version_number, id);
CREATE INDEX idx_records_owner_journal ON manuscript_records(owner_user_id, journal_config_id, recorded_at);
CREATE INDEX idx_records_owner_status ON manuscript_records(owner_user_id, status);
CREATE TABLE manuscript_stars (
    owner_user_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    starred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_user_id, original_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE manuscript_hides (
    owner_user_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_user_id, original_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_manuscript_hides_owner_time ON manuscript_hides(owner_user_id, hidden_at);
CREATE TABLE submission_journeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    hidden_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(id, owner_user_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_journeys_owner_updated ON submission_journeys(owner_user_id, updated_at DESC, id DESC);
CREATE TABLE submission_journey_members (
    owner_user_id INTEGER NOT NULL,
    journey_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_user_id, original_id),
    UNIQUE(owner_user_id, journey_id, position),
    FOREIGN KEY(journey_id, owner_user_id) REFERENCES submission_journeys(id, owner_user_id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_journey_members_order ON submission_journey_members(owner_user_id, journey_id, position);
CREATE TABLE manuscript_acks (
    owner_user_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    ack_status TEXT NOT NULL DEFAULT '',
    ack_detailed_status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_user_id, original_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE query_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    journal_config_id INTEGER,
    journal TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'scholarone',
    success INTEGER NOT NULL DEFAULT 0 CHECK(success IN (0,1)),
    manuscripts_found INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    query_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(journal_config_id, owner_user_id) REFERENCES journal_configs(id, owner_user_id) ON DELETE RESTRICT
);
CREATE INDEX idx_query_logs_owner_time ON query_logs(owner_user_id, query_time);
CREATE TABLE system_smtp_config (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    security_mode TEXT NOT NULL CHECK(security_mode IN ('starttls','ssl')),
    username TEXT NOT NULL DEFAULT '',
    sender_address TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    credential_ref TEXT,
    config_generation INTEGER NOT NULL DEFAULT 1 CHECK(config_generation > 0),
    last_test_status TEXT CHECK(last_test_status IN ('success','failed') OR last_test_status IS NULL),
    last_tested_generation INTEGER,
    last_tested_at TEXT,
    last_test_error_code TEXT,
    updated_by_user_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(updated_by_user_id) REFERENCES users(id)
);
CREATE TABLE user_smtp_configs (
    owner_user_id INTEGER PRIMARY KEY,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    security_mode TEXT NOT NULL CHECK(security_mode IN ('starttls','ssl')),
    username TEXT NOT NULL DEFAULT '',
    sender_address TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    credential_ref TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE automation_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('update','email')),
    cron_expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(id, owner_user_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_jobs_owner_enabled ON automation_jobs(owner_user_id, enabled, kind);
CREATE TABLE automation_run_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    job_id INTEGER,
    kind TEXT NOT NULL CHECK(kind IN ('update','email')),
    source TEXT NOT NULL CHECK(source IN ('schedule','manual','retry')),
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','claimed','running','finished','failed','cancelled')),
    requested_by_user_id INTEGER,
    claimed_by TEXT,
    lease_expires_at TEXT,
    claim_generation INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    heartbeat_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_user_id, job_id, scheduled_for, source),
    FOREIGN KEY(job_id, owner_user_id) REFERENCES automation_jobs(id, owner_user_id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(requested_by_user_id) REFERENCES users(id)
);
CREATE INDEX idx_run_requests_owner_status ON automation_run_requests(owner_user_id,status,scheduled_for);
CREATE UNIQUE INDEX idx_active_update_per_owner
ON automation_run_requests(owner_user_id)
WHERE kind='update' AND status IN ('queued','claimed','running');
CREATE UNIQUE INDEX idx_active_email_per_job
ON automation_run_requests(owner_user_id,job_id)
WHERE kind='email' AND job_id IS NOT NULL
  AND status IN ('queued','claimed','running');
CREATE TABLE automation_job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    job_id INTEGER,
    request_id INTEGER,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    FOREIGN KEY(job_id, owner_user_id) REFERENCES automation_jobs(id, owner_user_id) ON DELETE SET NULL,
    FOREIGN KEY(request_id) REFERENCES automation_run_requests(id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE automation_email_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    UNIQUE(owner_user_id, job_id, email),
    FOREIGN KEY(job_id, owner_user_id) REFERENCES automation_jobs(id, owner_user_id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE automation_email_manuscript_state (
    owner_user_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(owner_user_id, job_id, original_id),
    FOREIGN KEY(job_id, owner_user_id) REFERENCES automation_jobs(id, owner_user_id) ON DELETE CASCADE
);
CREATE TABLE automation_email_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    FOREIGN KEY(job_id, owner_user_id) REFERENCES automation_jobs(id, owner_user_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES automation_job_runs(id),
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE automation_email_delivery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    delivery_id INTEGER NOT NULL,
    original_id TEXT NOT NULL,
    delivered_fingerprint TEXT NOT NULL,
    UNIQUE(owner_user_id, delivery_id, original_id),
    FOREIGN KEY(delivery_id) REFERENCES automation_email_deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE automation_ack_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    delivery_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(delivery_id) REFERENCES automation_email_deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
INSERT INTO app_schema(singleton, version, application_compatibility, migration_id)
VALUES(1, 10, 'manuscript-desk-multitenant-v10', 'fresh');
INSERT INTO app_settings(singleton, self_registration_enabled) VALUES(1, 0);
"""


class SchemaError(RuntimeError):
    pass


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database(db_path: str | Path) -> None:
    path = Path(db_path)
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    except Exception:
        conn.close()
        path.unlink(missing_ok=True)
        Path(f"{path}-wal").unlink(missing_ok=True)
        Path(f"{path}-shm").unlink(missing_ok=True)
        raise
    else:
        conn.close()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(users)")}
    if "username" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT COLLATE NOCASE")
    rows = conn.execute("SELECT id,legacy_username,email FROM users ORDER BY id").fetchall()
    used: set[str] = set()
    for user in rows:
        candidate = str(user["legacy_username"] or user["email"].split("@", 1)[0]).strip().casefold()
        if not candidate:
            candidate = f"user{user['id']}"
        base = candidate
        suffix = 1
        while (
            candidate in used
            or conn.execute(
                "SELECT 1 FROM users WHERE username=? COLLATE NOCASE AND id<>?",
                (candidate, user["id"]),
            ).fetchone()
        ):
            suffix += 1
            candidate = f"{base}{suffix}"
        used.add(candidate)
        conn.execute("UPDATE users SET username=? WHERE id=?", (candidate, user["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE)")
    conn.execute(
        "UPDATE app_schema SET version=2,application_compatibility='manuscript-desk-multitenant-v2',"
        "migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _cancel_duplicate_active_requests(conn: sqlite3.Connection) -> None:
    active = "('queued','claimed','running')"
    duplicates = conn.execute(
        f"""SELECT id,owner_user_id,job_id,kind FROM automation_run_requests
            WHERE status IN {active}
            ORDER BY scheduled_for,id"""
    ).fetchall()
    retained_updates: set[int] = set()
    retained_emails: set[tuple[int, int]] = set()
    cancelled: list[int] = []
    for request in duplicates:
        if request["kind"] == "update":
            key = request["owner_user_id"]
            if key in retained_updates:
                cancelled.append(request["id"])
            else:
                retained_updates.add(key)
        elif request["kind"] == "email" and request["job_id"] is not None:
            key = (request["owner_user_id"], request["job_id"])
            if key in retained_emails:
                cancelled.append(request["id"])
            else:
                retained_emails.add(key)
    if cancelled:
        placeholders = ",".join("?" for _ in cancelled)
        conn.execute(
            f"""UPDATE automation_run_requests
                SET status='cancelled',claimed_by=NULL,lease_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})""",
            cancelled,
        )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    _cancel_duplicate_active_requests(conn)
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_run_requests_owner_status
           ON automation_run_requests(owner_user_id,status,scheduled_for)"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_update_per_owner
           ON automation_run_requests(owner_user_id)
           WHERE kind='update' AND status IN ('queued','claimed','running')"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_email_per_job
           ON automation_run_requests(owner_user_id,job_id)
           WHERE kind='email' AND job_id IS NOT NULL
             AND status IN ('queued','claimed','running')"""
    )
    conn.execute(
        "UPDATE app_schema SET version=3,application_compatibility='manuscript-desk-multitenant-v3',"
        "migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(api_tokens)")}
    if "secret_ref" not in columns:
        conn.execute("ALTER TABLE api_tokens ADD COLUMN secret_ref TEXT")
    conn.execute(
        "UPDATE app_schema SET version=4,application_compatibility='manuscript-desk-multitenant-v4',"
        "migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(automation_run_requests)")}
    if "payload_json" not in columns:
        conn.execute("ALTER TABLE automation_run_requests ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
    tokens = conn.execute("SELECT id,scopes FROM api_tokens").fetchall()
    for token in tokens:
        scopes = {
            "starred:update" if scope == "starred:renew" else scope
            for scope in str(token["scopes"] or "").split()
            if scope
        }
        conn.execute(
            "UPDATE api_tokens SET scopes=? WHERE id=?",
            (" ".join(sorted(scopes)), token["id"]),
        )
    conn.execute(
        "UPDATE app_schema SET version=5,application_compatibility='manuscript-desk-multitenant-v5',"
        "migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS manuscript_hides (
               owner_user_id INTEGER NOT NULL,
               original_id TEXT NOT NULL,
               hidden_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY(owner_user_id, original_id),
               FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
           )""",
        "CREATE INDEX IF NOT EXISTS idx_manuscript_hides_owner_time ON manuscript_hides(owner_user_id, hidden_at)",
        """CREATE TABLE IF NOT EXISTS submission_journeys (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               owner_user_id INTEGER NOT NULL,
               hidden_at TEXT,
               revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(id, owner_user_id),
               FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
           )""",
        "CREATE INDEX IF NOT EXISTS idx_journeys_owner_updated ON submission_journeys(owner_user_id, updated_at DESC, id DESC)",
        """CREATE TABLE IF NOT EXISTS submission_journey_members (
               owner_user_id INTEGER NOT NULL,
               journey_id INTEGER NOT NULL,
               original_id TEXT NOT NULL,
               position INTEGER NOT NULL CHECK(position >= 0),
               added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY(owner_user_id, original_id),
               UNIQUE(owner_user_id, journey_id, position),
               FOREIGN KEY(journey_id, owner_user_id)
                 REFERENCES submission_journeys(id, owner_user_id) ON DELETE CASCADE,
               FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
           )""",
        "CREATE INDEX IF NOT EXISTS idx_journey_members_order ON submission_journey_members(owner_user_id, journey_id, position)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute(
        "UPDATE app_schema SET version=6,application_compatibility='manuscript-desk-multitenant-v6',"
        "migration_id='v5-to-v6-hidden-journeys',migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_shortcut_settings (
               owner_user_id INTEGER PRIMARY KEY,
               base_url TEXT NOT NULL,
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
           )"""
    )
    conn.execute(
        "UPDATE app_schema SET version=7,application_compatibility='manuscript-desk-multitenant-v7',"
        "migration_id='v6-to-v7-shortcut-settings',migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(automation_run_requests)")}
    if "claim_generation" not in columns:
        conn.execute("ALTER TABLE automation_run_requests ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0")
    if "claim_token" not in columns:
        conn.execute("ALTER TABLE automation_run_requests ADD COLUMN claim_token TEXT")
    if "heartbeat_at" not in columns:
        conn.execute("ALTER TABLE automation_run_requests ADD COLUMN heartbeat_at TEXT")
    conn.execute(
        """UPDATE automation_run_requests
           SET status='queued',claimed_by=NULL,lease_expires_at=NULL,claim_token=NULL,heartbeat_at=NULL
           WHERE status IN ('claimed','running')"""
    )
    conn.execute(
        "UPDATE app_schema SET version=8,application_compatibility='manuscript-desk-multitenant-v8',"
        "migration_id='v7-to-v8-automation-fencing',migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    user_columns = {item["name"] for item in conn.execute("PRAGMA table_info(users)")}
    if "smtp_source" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN smtp_source TEXT NOT NULL DEFAULT 'personal' "
            "CHECK(smtp_source IN ('personal','system'))"
        )
    smtp_columns = {item["name"] for item in conn.execute("PRAGMA table_info(system_smtp_config)")}
    additions = {
        "config_generation": "INTEGER NOT NULL DEFAULT 1 CHECK(config_generation > 0)",
        "last_test_status": "TEXT CHECK(last_test_status IN ('success','failed') OR last_test_status IS NULL)",
        "last_tested_generation": "INTEGER",
        "last_tested_at": "TEXT",
        "last_test_error_code": "TEXT",
    }
    for name, definition in additions.items():
        if name not in smtp_columns:
            conn.execute(f"ALTER TABLE system_smtp_config ADD COLUMN {name} {definition}")
    conn.execute(
        "UPDATE app_schema SET version=9,application_compatibility='manuscript-desk-multitenant-v9',"
        "migration_id='v8-to-v9-smtp-sources',migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def _canonicalize_legacy_scholarone_url(value: object) -> tuple[str, str] | None:
    """Strict, local migration helper; avoids importing provider/web layers."""
    raw = str(value or "").strip()
    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold() if parsed.hostname else ""
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme.casefold() != "http" or not host or parsed.username is not None or parsed.password is not None:
        return None
    if parsed.fragment or port not in (None, 80):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    if host != "manuscriptcentral.com" and not host.endswith(".manuscriptcentral.com"):
        return None
    path = parsed.path or "/"
    canonical = urlunsplit(("https", host, path, parsed.query, ""))
    parts = [part for part in path.split("/") if part]
    site_key = f"{host}:{parts[0].casefold() if parts else 'default'}"
    return canonical, site_key


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    configs = conn.execute("SELECT id,journal_url FROM journal_configs WHERE provider='scholarone'").fetchall()
    for config in configs:
        canonical = _canonicalize_legacy_scholarone_url(config["journal_url"])
        if canonical:
            conn.execute(
                "UPDATE journal_configs SET journal_url=?,site_key=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (canonical[0], canonical[1], config["id"]),
            )

    names_by_owner: dict[int, dict[int, str]] = {}
    for row in conn.execute("SELECT owner_user_id,id,journal_name FROM journal_configs"):
        names_by_owner.setdefault(int(row["owner_user_id"]), {})[int(row["id"])] = str(row["journal_name"])
    runs = conn.execute(
        """SELECT r.id,r.owner_user_id,r.status,r.summary_json,COALESCE(j.kind,q.kind) AS kind
           FROM automation_job_runs r
           LEFT JOIN automation_jobs j ON j.id=r.job_id AND j.owner_user_id=r.owner_user_id
           LEFT JOIN automation_run_requests q ON q.id=r.request_id AND q.owner_user_id=r.owner_user_id"""
    ).fetchall()
    for run in runs:
        try:
            raw_summary = json.loads(run["summary_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raw_summary = None
        compact = project_automation_summary(
            raw_summary,
            status=str(run["status"]),
            kind=run["kind"],
            journal_names_by_id=names_by_owner.get(int(run["owner_user_id"]), {}),
            historical=True,
        )
        conn.execute(
            "UPDATE automation_job_runs SET summary_json=? WHERE id=?",
            (json.dumps(compact, separators=(",", ":"), sort_keys=True), run["id"]),
        )
    conn.execute(
        "UPDATE app_schema SET version=10,application_compatibility='manuscript-desk-multitenant-v10',"
        "migration_id='v9-to-v10-compact-run-history',migrated_at=CURRENT_TIMESTAMP WHERE singleton=1"
    )


def migrate_database(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT version FROM app_schema WHERE singleton=1").fetchone()
        if not row or row["version"] >= APP_SCHEMA_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")
        version = row["version"]
        if version == 1:
            _migrate_v1_to_v2(conn)
            version = 2
        if version == 2:
            _migrate_v2_to_v3(conn)
            version = 3
        if version == 3:
            _migrate_v3_to_v4(conn)
            version = 4
        if version == 4:
            _migrate_v4_to_v5(conn)
            version = 5
        if version == 5:
            _migrate_v5_to_v6(conn)
            version = 6
        if version == 6:
            _migrate_v6_to_v7(conn)
            version = 7
        if version == 7:
            _migrate_v7_to_v8(conn)
            version = 8
        if version == 8:
            _migrate_v8_to_v9(conn)
            version = 9
        if version == 9:
            _migrate_v9_to_v10(conn)
            version = 10
        if version != APP_SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version: {version}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def assert_schema(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("SELECT version, application_compatibility FROM app_schema WHERE singleton = 1").fetchone()
    except sqlite3.Error as exc:
        raise SchemaError("database is not a multi-tenant Manuscript Desk database") from exc
    if not row or row["version"] != APP_SCHEMA_VERSION:
        found = None if not row else row["version"]
        raise SchemaError(f"unsupported schema version: {found}")
    if row["application_compatibility"] != f"manuscript-desk-multitenant-v{APP_SCHEMA_VERSION}":
        raise SchemaError("application compatibility marker does not match")
