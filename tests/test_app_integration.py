"""Integration tests for the full HTML/Jinja multi-tenant application.

These exercise the wired application produced by ``create_app`` with the overlay
templates/static mounted: HTML route availability, admin-vs-tenant navigation,
tenant manuscript/config isolation through the session-authenticated APIs, form
CSRF enforcement, and owner-scoped configuration.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manuscript_desk.repository import Repository
from manuscript_desk.schema import create_database
from manuscript_desk.secrets_store import SecretStore
from manuscript_desk.security import hash_password
from manuscript_desk.web import create_app
from manuscript_desk.webui import _time_ago, normalize_shortcut_base_url

KEY = b"k" * 32
RATE_KEY = b"r" * 32
CAPTCHA_KEY = b"c" * 32
PASSWORD = "correct horse battery staple1"


class RecordingMailer:
    def __init__(self):
        self.codes = []
        self.invitations = []

    def configured(self):
        return True

    def send_code(self, email, display_name, code, purpose):
        self.codes.append((email, code, purpose))

    def send_invitation(self, email, display_name, token):
        self.invitations.append((email, token))

    def _send(self, recipient, subject, body):
        self.codes.append((recipient, subject, "raw"))


def build(tmp_path, *, shortcut_wait_seconds=110.0, shortcut_poll_seconds=0.5):
    db = tmp_path / "app.db"
    create_database(db)
    repo = Repository(db)
    store = SecretStore(tmp_path / "secrets")
    mailer = RecordingMailer()
    app = create_app(
        db,
        KEY,
        RATE_KEY,
        CAPTCHA_KEY,
        mailer,
        cookie_secure=False,
        secret_store=store,
        secret_cleanup=store.delete,
        public_base_url="http://testserver",
        shortcut_wait_seconds=shortcut_wait_seconds,
        shortcut_poll_seconds=shortcut_poll_seconds,
        shortcut_continuation_ttl_seconds=3600,
    )
    # A fresh TestClient per login keeps cookie jars isolated between tenants.
    return repo, store, mailer, app


def add_user(repo, email, *, admin=False):
    return repo.create_user(email, email.split("@")[0], hash_password(PASSWORD), is_admin=admin, verified=True)


def html_login(client, identifier):
    """Log in through the HTML form and return the session CSRF token."""
    page = client.get("/login")
    assert page.status_code == 200
    token = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    # login page is anonymous: csrf comes from the anon cookie echoed into the form
    form_token = re.search(r'name="csrf_token" value="([^"]*)"', page.text).group(1)
    resp = client.post(
        "/login",
        data={"identifier": identifier, "password": PASSWORD, "csrf_token": form_token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.text
    # Fetch an authenticated page to read the session CSRF token from the meta tag.
    home = client.get("/", follow_redirects=False)
    return home


def session_csrf(client):
    """Read the session CSRF token from any authenticated page's meta tag."""
    for path in ("/", "/admin/users"):
        page = client.get(path, follow_redirects=False)
        if page.status_code == 200:
            match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
            if match and match.group(1):
                return match.group(1)
    raise AssertionError("no authenticated page available for CSRF token")


def test_shortcut_base_url_normalization_contract():
    assert normalize_shortcut_base_url(" https://desk.example.test/ ") == "https://desk.example.test"
    assert normalize_shortcut_base_url("http://203.0.113.20:8123/app/") == "http://203.0.113.20:8123/app"
    assert normalize_shortcut_base_url("https://[2001:db8::1]:9443/desk/") == "https://[2001:db8::1]:9443/desk"
    for invalid in (
        "ftp://desk.example.test",
        "https://user:password@desk.example.test",
        "https://desk.example.test/path?token=x",
        "https://desk.example.test/path#fragment",
        "https://desk.example.test:70000",
        "https:///missing-host",
        "https://desk.example.test/\x00bad",
    ):
        with pytest.raises(ValueError):
            normalize_shortcut_base_url(invalid)


def test_shortcut_management_requires_tenant_session_and_csrf(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    add_user(repo, "admin@example.test", admin=True)

    anonymous = TestClient(app)
    assert anonymous.get("/shortcuts", follow_redirects=False).headers["location"] == "/login"
    assert (
        anonymous.post(
            "/shortcuts/base-url", data={"base_url": "https://desk.example.test"}, follow_redirects=False
        ).headers["location"]
        == "/login"
    )

    tenant = TestClient(app)
    html_login(tenant, "alice@example.test")
    denied = tenant.post(
        "/shortcuts/base-url",
        data={"base_url": "https://desk.example.test", "csrf_token": "wrong"},
    )
    assert denied.status_code == 403
    assert repo.get_shortcut_settings(1) is None

    admin = TestClient(app)
    html_login(admin, "admin@example.test")
    assert admin.get("/shortcuts", follow_redirects=False).headers["location"] == "/admin/users"
    assert (
        admin.post(
            "/shortcuts/base-url",
            data={"base_url": "https://desk.example.test", "csrf_token": session_csrf(admin)},
            follow_redirects=False,
        ).headers["location"]
        == "/admin/users"
    )


def test_shortcut_layout_is_fixed_width_and_wraps_content():
    template = (Path(__file__).parents[1] / "src/manuscript_desk/templates/shortcut_starred.html").read_text()
    assert "table-layout:fixed" in template
    assert "max-width:100%" in template
    assert "overflow-x:hidden" in template
    assert '<colgroup><col class="c-num">' in template
    assert ".tt{font-weight:500;white-space:normal" in template
    assert ".st{display:inline-block;max-width:100%" in template
    assert ".dt{font-size:.72em;color:#8e8e93;white-space:normal" in template
    assert "text-overflow:ellipsis" not in template


def test_timeline_event_styles_follow_event_type():
    template = (Path(__file__).parents[1] / "src/manuscript_desk/templates/index.html").read_text()
    assert "e.type==='submitted'?'submitted':e.type==='decision'?'decision':'status'" in template

    status_rules_end = template.index("/* 更新状态条 */")
    styles = template[:status_rules_end]
    state_rule = styles.index(".ptl-n.st-reject-resubmit .dot")
    submitted_rule = styles.index(".ptl-n.ev-submitted .dot{background:#dc3545;box-shadow:0 0 0 2px #bd2130")
    decision_rule = styles.index(".ptl-n.ev-decision .dot{background:#28a745;box-shadow:0 0 0 2px #1e7e34")
    status_rule = styles.index(".ptl-n.ev-status .dot{background:#6c757d;box-shadow:0 0 0 2px #545b62")

    assert state_rule < submitted_rule < decision_rule < status_rule
    assert ".ptl-n.ev-status .card{opacity:.7}" in styles
    assert ".ptl-n .card{opacity:.7}" not in styles


def test_config_table_compact_layout_contract():
    css = (Path(__file__).parents[1] / "src/manuscript_desk/static/ui/account.css").read_text()
    assert ".account-table-wrap{overflow-x:auto" in css
    assert ".config-table{min-width:900px}" in css
    assert ".config-table th,.config-table td{padding:8px 10px;line-height:1.25}" in css
    assert ".config-table th[scope=row]{min-width:180px;text-transform:none;letter-spacing:normal}" in css
    assert (
        ".config-description-preview{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        in css
    )
    assert ".config-table-actions{flex-wrap:nowrap;white-space:nowrap}" in css
    assert ".config-table-actions .account-button{min-height:36px;flex:0 0 auto" in css


def test_time_ago_supports_scholarone_and_editorial_manager_dates():
    assert _time_ago("27-Apr-2026")
    assert _time_ago("Aug 12, 2026")
    assert _time_ago("December 04, 2025")
    assert _time_ago("not a date") == ""


def test_auth_pages_available_without_login(tmp_path):
    _, _, _, app = build(tmp_path)
    client = TestClient(app)
    # Registration disabled by default -> /register redirects to /login.
    for path, expected in [
        ("/login", 200),
        ("/forgot-password", 200),
        ("/reset-password", 200),
        ("/invite/some-token", 200),
        ("/verify-email", 200),
    ]:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == expected, f"{path} -> {resp.status_code}"
    # Protected pages redirect anonymous users to the login page.
    for path in ("/", "/profile", "/config", "/automation", "/admin/users"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303) and resp.headers["location"] == "/login", path


def test_password_recovery_links_to_code_entry(tmp_path):
    repo, _, mailer, app = build(tmp_path)
    add_user(repo, "xuan@example.test")
    client = TestClient(app)

    page = client.get("/forgot-password")
    csrf = re.search(r'name="csrf_token" value="([^"]*)"', page.text).group(1)
    submitted = client.post(
        "/forgot-password",
        data={"email": "xuan@example.test", "csrf_token": csrf},
    )

    assert submitted.status_code == 200
    assert "如果该邮箱对应有效账号" in submitted.text
    assert 'href="/reset-password?email=xuan%40example.test"' in submitted.text
    assert "输入验证码并设置新密码" in submitted.text
    assert mailer.codes and mailer.codes[-1][2] == "password_reset"

    reset = client.get("/reset-password?email=xuan%40example.test")
    assert reset.status_code == 200
    assert 'name="email" value="xuan@example.test"' in reset.text
    assert 'name="code"' in reset.text
    assert 'name="password"' in reset.text
    assert 'name="confirm_password"' in reset.text


def test_password_recovery_submitted_state_does_not_enumerate_accounts(tmp_path):
    _, _, mailer, app = build(tmp_path)
    client = TestClient(app)

    page = client.get("/forgot-password")
    csrf = re.search(r'name="csrf_token" value="([^"]*)"', page.text).group(1)
    submitted = client.post(
        "/forgot-password",
        data={"email": "missing@example.test", "csrf_token": csrf},
    )

    assert submitted.status_code == 200
    assert "如果该邮箱对应有效账号" in submitted.text
    assert 'href="/reset-password?email=missing%40example.test"' in submitted.text
    assert mailer.codes == []


def test_static_assets_are_mounted(tmp_path):
    _, _, _, app = build(tmp_path)
    client = TestClient(app)
    assert client.get("/static/ui/shell.css").status_code == 200
    assert client.get("/static/ui/account.js").status_code == 200
    assert client.get("/static/automation/automation.js").status_code == 200
    automation_page = client.get("/automation", follow_redirects=False)
    assert automation_page.status_code in (302, 303)


def test_tenant_sees_manuscript_nav_admin_sees_control_plane(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "tenant@example.test")
    add_user(repo, "admin@example.test", admin=True)

    tenant = TestClient(app)
    html_login(tenant, "tenant@example.test")
    home = tenant.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "稿件" in home.text and "期刊配置" in home.text
    assert "/admin/users" not in home.text.split("app-navigation")[1].split("app-sidebar-footer")[0]

    admin = TestClient(app)
    html_login(admin, "admin@example.test")
    # Admin is steered to the control plane and cannot reach the tenant workspace.
    root = admin.get("/", follow_redirects=False)
    assert root.status_code in (302, 303) and root.headers["location"] == "/admin/users"
    users = admin.get("/admin/users", follow_redirects=False)
    assert users.status_code == 200 and "用户管理" in users.text
    # Tenant cannot reach the admin control plane.
    assert tenant.get("/admin/users", follow_redirects=False).headers["location"] == "/"


def test_tenant_manuscript_isolation_via_api(tmp_path):
    repo, store, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    a_cfg = repo.create_journal_config(
        alice, {"journal_name": "Shared", "journal_url": "https://mc.manuscriptcentral.com/x", "username": "a"}
    )
    b_cfg = repo.create_journal_config(
        bob, {"journal_name": "Shared", "journal_url": "https://mc.manuscriptcentral.com/x", "username": "b"}
    )
    repo.save_manuscript(
        alice,
        a_cfg,
        {
            "original_id": "COLLIDE",
            "manuscript_id": "COLLIDE",
            "title": "Alice paper",
            "status": "Accept",
            "detailed_status": "Accept",
        },
    )
    repo.save_manuscript(
        bob,
        b_cfg,
        {
            "original_id": "COLLIDE",
            "manuscript_id": "COLLIDE",
            "title": "Bob paper",
            "status": "Reject",
            "detailed_status": "Reject",
        },
    )

    alice_client = TestClient(app)
    html_login(alice_client, "alice@example.test")
    data = alice_client.get("/api/manuscripts").json()
    assert [m["title"] for m in data["manuscripts"]] == ["Alice paper"]

    bob_client = TestClient(app)
    html_login(bob_client, "bob@example.test")
    data = bob_client.get("/api/manuscripts").json()
    assert [m["title"] for m in data["manuscripts"]] == ["Bob paper"]
    # Bob's history for the colliding id must never surface Alice's record.
    hist = bob_client.get("/api/manuscripts/COLLIDE/history").json()
    assert all(row["title"] == "Bob paper" for row in hist["history"])


def test_json_mutation_requires_csrf(tmp_path):
    repo, _, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    cfg = repo.create_journal_config(
        alice, {"journal_name": "J", "journal_url": "https://mc.manuscriptcentral.com/x", "username": "a"}
    )
    repo.save_manuscript(
        alice,
        cfg,
        {"original_id": "M1", "manuscript_id": "M1", "title": "P", "status": "Accept", "detailed_status": "Accept"},
    )

    client = TestClient(app)
    html_login(client, "alice@example.test")
    # No CSRF header -> rejected.
    assert client.post("/api/manuscripts/M1/star").status_code == 403
    # With the session CSRF header -> accepted.
    csrf = session_csrf(client)
    resp = client.post("/api/manuscripts/M1/star", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200 and resp.json()["starred"] is True


def test_html_form_csrf_enforced(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    # Profile update with a bogus CSRF token is rejected (403) and does not persist.
    resp = client.post("/profile", data={"display_name": "Hacked", "timezone": "UTC", "csrf_token": "wrong"})
    assert resp.status_code == 403
    assert repo.get_user_by_email("alice@example.test")["display_name"] != "Hacked"
    # With the correct token the update succeeds.
    csrf = session_csrf(client)
    ok = client.post("/profile", data={"display_name": "Alice New", "timezone": "UTC", "csrf_token": csrf})
    assert ok.status_code == 200
    assert repo.get_user_by_email("alice@example.test")["display_name"] == "Alice New"


def automation_csrf(client):
    page = client.get("/automation")
    assert page.status_code == 200
    return re.search(r'name="automation-csrf" content="([^"]*)"', page.text).group(1)


def test_automation_smtp_source_is_sanitized_and_csrf_protected(tmp_path):
    repo, _, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = automation_csrf(client)

    page = client.get("/automation")
    assert page.text.count('<option value="') >= 24
    assert 'value="Asia/Shanghai"' in page.text
    assert 'name="smtp_source" value="system"' in page.text
    assert 'name="smtp_source" value="personal"' in page.text

    initial = client.get("/api/automation/smtp").json()
    assert initial["source"] == "personal"
    assert initial["system"]["health"] == "unconfigured"
    assert set(initial["system"]) == {"configured", "health", "selectable", "tested_at", "message"}
    assert not ({"host", "username", "sender_address", "credential_ref", "config_generation"} & set(initial["system"]))

    no_csrf = client.put("/api/automation/smtp/source", json={"source": "personal"})
    assert no_csrf.status_code == 403
    unavailable = client.put(
        "/api/automation/smtp/source",
        json={"source": "system"},
        headers={"X-Automation-CSRF": csrf},
    )
    assert unavailable.status_code == 409

    config = repo.save_system_smtp(
        alice,
        {
            "host": "smtp.system.test",
            "port": 465,
            "security_mode": "ssl",
            "username": "private-user",
            "sender_address": "private@example.test",
            "credential_ref": "users/0/smtp/private-ref",
        },
    )
    repo.record_system_smtp_test(config["config_generation"], "success")
    switched = client.put(
        "/api/automation/smtp/source",
        json={"source": "system"},
        headers={"X-Automation-CSRF": csrf},
    )
    assert switched.status_code == 200
    projected = client.get("/api/automation/smtp")
    assert projected.json()["source"] == "system"
    assert "smtp.system.test" not in projected.text
    assert "private-user" not in projected.text
    assert "private@example.test" not in projected.text
    assert "private-ref" not in projected.text


def test_profile_uses_compact_timezones_and_preserves_uncommon_values(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)

    page = client.get("/profile")
    assert page.status_code == 200
    assert 'type="search"' not in page.text
    assert "<optgroup" not in page.text
    assert page.text.count("<option value=") == 24
    assert 'value="Asia/Shanghai"' in page.text
    assert 'value="Pacific/Chatham"' not in page.text

    valid = client.post(
        "/profile", data={"display_name": "Alice Chatham", "timezone": "Pacific/Chatham", "csrf_token": csrf}
    )
    assert valid.status_code == 200
    assert repo.get_user_by_email("alice@example.test")["timezone"] == "Pacific/Chatham"
    preserved = client.get("/profile")
    assert 'value="Pacific/Chatham" selected' in preserved.text
    assert "Pacific/Chatham（当前已保存）" in preserved.text

    invalid = client.post(
        "/profile", data={"display_name": "Must Not Persist", "timezone": "Not/A_Zone", "csrf_token": csrf}
    )
    assert invalid.status_code == 400
    user = repo.get_user_by_email("alice@example.test")
    assert user["display_name"] == "Alice Chatham"
    assert user["timezone"] == "Pacific/Chatham"
    assert "请选择有效的 IANA 时区" in invalid.text


def test_status_filter_has_native_hierarchy_without_changing_values(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    page = client.get("/")
    select = page.text.split('id="statusFilter"', 1)[1].split("</select>", 1)[0]
    assert '<optgroup label="Revision">' in select
    assert '<optgroup label="Reject">' in select
    assert '<optgroup label="Under Review">' in select
    assert re.findall(r'<option value="([^"]*)"', select) == [
        "",
        "accept",
        "revision",
        "minor",
        "major",
        "reject_and_resubmit",
        "reject",
        "reject-only",
        "desk",
        "draft",
        "under_review",
        "awaiting",
        "review",
        "other",
    ]


def test_config_ownership_create_and_cross_tenant_isolation(tmp_path):
    repo, store, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    add_user(repo, "bob@example.test")

    alice_client = TestClient(app)
    html_login(alice_client, "alice@example.test")
    csrf = session_csrf(alice_client)
    payload = {
        "journal_name": "Alice MixedCase Journal",
        "journal_url": "https://mc.manuscriptcentral.com/aj",
        "username": "alice",
        "provider": "scholarone",
        "password": "journal-secret-value",
        "enabled": True,
        "description": "A concise opening that continues into a much longer journal description for hover display.",
    }
    created = alice_client.post("/api/journal-configs", json=payload, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200, created.text
    # The plaintext password and internal secret reference are never echoed.
    configs = alice_client.get("/api/journal-configs").json()["configs"]
    assert len(configs) == 1 and configs[0]["credential_configured"] is True
    assert "credential_ref" not in configs[0]
    assert "owner_user_id" not in configs[0]
    with repo.connection() as conn:
        ref = conn.execute(
            "SELECT credential_ref FROM journal_configs WHERE id=? AND owner_user_id=?",
            (configs[0]["id"], alice),
        ).fetchone()[0]
    assert "journal-secret-value" not in ref
    assert ref.startswith(f"users/{alice}/credentials/")
    assert store.read(ref) == "journal-secret-value"

    config_page = alice_client.get("/config")
    assert config_page.status_code == 200
    assert '<table class="account-table config-table">' in config_page.text
    assert '<caption class="visually-hidden">' in config_page.text
    assert "登录账号与凭据" in config_page.text
    assert "Alice MixedCase Journal" in config_page.text
    account_cell = config_page.text.split('<td class="config-account-cell">', 1)[1].split("</td>", 1)[0]
    assert "alice" in account_cell and "凭据已配置" in account_cell
    assert "<a " not in account_cell and payload["journal_url"] not in account_cell
    preview = payload["description"][:32] + "…"
    assert preview in config_page.text
    assert f'title="{payload["description"]}"' in config_page.text
    assert f'aria-label="完整描述：{payload["description"]}"' in config_page.text
    assert 'class="account-table-actions config-table-actions"' in config_page.text
    assert payload["journal_url"] in config_page.text
    assert "credential_ref" not in config_page.text
    assert "owner_user_id" not in config_page.text
    assert "journal-secret-value" not in config_page.text

    edited = alice_client.put(
        f"/api/journal-configs/{configs[0]['id']}",
        json={**payload, "short_name": "AJ", "description": "Updated", "password": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert edited.status_code == 200
    internal = repo.list_journal_configs(alice)[0]
    assert internal["short_name"] == "AJ"
    assert internal["description"] == "Updated"
    assert internal["credential_ref"] == ref

    # Bob cannot see Alice's configuration.
    bob_client = TestClient(app)
    html_login(bob_client, "bob@example.test")
    assert bob_client.get("/api/journal-configs").json()["configs"] == []
    # Provider/URL mismatch is rejected.
    bob_csrf = session_csrf(bob_client)
    bad = bob_client.post(
        "/api/journal-configs",
        json={
            "journal_name": "X",
            "journal_url": "https://evil.test",
            "username": "b",
            "provider": "scholarone",
            "password": "x" * 12,
        },
        headers={"X-CSRF-Token": bob_csrf},
    )
    assert bad.status_code == 400


def test_hidden_and_journey_api_are_tenant_scoped(tmp_path):
    repo, _, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    alice_config = repo.create_journal_config(
        alice, {"journal_name": "Alice", "journal_url": "https://mc.manuscriptcentral.com/a", "username": "a"}
    )
    bob_config = repo.create_journal_config(
        bob, {"journal_name": "Bob", "journal_url": "https://mc.manuscriptcentral.com/b", "username": "b"}
    )
    for owner, config_id in ((alice, alice_config), (bob, bob_config)):
        for original_id, status in (("ONE", "Under Review"), ("TWO", "Revision")):
            repo.save_manuscript(
                owner,
                config_id,
                {
                    "original_id": original_id,
                    "manuscript_id": original_id,
                    "title": f"{owner}-{original_id}",
                    "status": status,
                    "detailed_status": status,
                },
            )

    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)
    assert client.put("/api/manuscripts/ONE/hidden").status_code == 403
    hidden = client.put("/api/manuscripts/ONE/hidden", headers={"X-CSRF-Token": csrf})
    assert hidden.status_code == 200
    assert [item["original_id"] for item in client.get("/api/manuscripts").json()["manuscripts"]] == ["TWO"]
    hidden_page = client.get("/?view=hidden")
    assert hidden_page.status_code == 200 and "1-ONE" in hidden_page.text

    assert client.delete("/api/manuscripts/ONE/hidden", headers={"X-CSRF-Token": csrf}).status_code == 200
    created = client.post(
        "/api/journeys",
        json={"member_original_ids": ["ONE", "TWO"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    journey = created.json()["journey"]
    cards = client.get("/api/manuscript-cards").json()["cards"]
    assert len(cards) == 1 and cards[0]["kind"] == "journey"
    assert cards[0]["representative"]["original_id"] == "TWO"
    stale_revision = journey["revision"]
    reordered = client.put(
        f"/api/journeys/{journey['id']}/order",
        json={"ordered_original_ids": ["TWO", "ONE"], "expected_revision": stale_revision},
        headers={"X-CSRF-Token": csrf},
    )
    assert reordered.status_code == 200
    conflict = client.put(
        f"/api/journeys/{journey['id']}/order",
        json={"ordered_original_ids": ["ONE", "TWO"], "expected_revision": stale_revision},
        headers={"X-CSRF-Token": csrf},
    )
    assert conflict.status_code == 409

    assert client.put(f"/api/journeys/{journey['id']}/hidden", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/manuscripts").json()["manuscripts"] == []
    assert client.get("/api/manuscript-cards").json()["cards"] == []
    hidden_cards = client.get("/api/manuscript-cards?view=hidden").json()["cards"]
    assert len(hidden_cards) == 1 and hidden_cards[0]["journey_id"] == journey["id"]
    assert client.delete(f"/api/journeys/{journey['id']}/hidden", headers={"X-CSRF-Token": csrf}).status_code == 200

    bob_client = TestClient(app)
    html_login(bob_client, "bob@example.test")
    assert bob_client.get(f"/api/journeys/{journey['id']}").status_code == 404


def test_manuscript_navigation_includes_ongoing_and_hidden(tmp_path):
    repo, _, _, app = build(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config_id = repo.create_journal_config(
        owner, {"journal_name": "Journal", "journal_url": "https://mc.manuscriptcentral.com/j", "username": "a"}
    )
    repo.save_manuscript(
        owner,
        config_id,
        {
            "original_id": "ACTIVE",
            "manuscript_id": "ACTIVE",
            "title": "Active",
            "status": "Under Review",
            "detailed_status": "Under Review",
        },
    )
    client = TestClient(app)
    html_login(client, "alice@example.test")
    page = client.get("/")
    assert "/?view=ongoing" in page.text and ">进行中<" in page.text
    assert "/?view=hidden" in page.text and ">隐藏稿件<" in page.text
    ongoing = client.get("/?view=ongoing")
    assert "Active" in ongoing.text
    assert "合并投稿历程" in page.text and 'id="journeyModal"' in page.text
    assert 'id="journeySearchInput"' in page.text
    assert "搜索标题、期刊、稿件编号或状态" in page.text
    assert "filterJourneyPicker" in page.text
    assert "隐藏这次投稿？以后仍会继续抓取" in page.text
    assert 'data-filter-scope="latest"' in page.text
    assert 'data-filter-scope="any"' in page.text
    assert "只看最新" in page.text and "任一投稿" in page.text
    assert "data-journey-members=" in page.text
    assert 'data-manuscript-id="ACTIVE"' in page.text
    filter_script = (Path(__file__).parents[1] / "src/manuscript_desk/static/ui/manuscripts.js").read_text()
    assert "card.dataset.searchText || card.dataset.title" not in filter_script
    assert "card.dataset.title || ''" in filter_script
    assert (
        "查看 {{ member.title or member.manuscript_id }} 的状态时间轴"
        in (Path(__file__).parents[1] / "src/manuscript_desk/templates/index.html").read_text()
    )
    assert (
        ">时间轴</button>" not in (Path(__file__).parents[1] / "src/manuscript_desk/templates/index.html").read_text()
    )


def test_manual_update_frontend_polls_and_reloads_each_request_once():
    template = (Path(__file__).parents[1] / "src/manuscript_desk/templates/index.html").read_text()

    assert "let _watchedRequestId = null;" in template
    assert "function stopPolling()" in template
    assert "if (_pollController) _pollController.abort();" in template
    assert "const generation = _pollGeneration;" in template
    assert "if (generation !== _pollGeneration || !resp.ok) return;" in template
    assert "if (generation !== _pollGeneration || _watchedRequestId === null) return;" in template
    assert "if (!watchedTerminal && _watchedRequestId !== null) {" in template
    assert "_watchedRequestId = null;\n                cancelPendingReload();" in template
    assert "if (renderUpdateStatus(s)) startPolling();" in template
    assert "watchActiveRequest(startResult.request_id);" in template
    assert "setUpdateBtn(true);\n            stopPolling();" in template
    assert "sessionGet(_reloadedRequestKey) === key" in template
    assert "sessionSet(_reloadedRequestKey, key);" in template
    assert "key !== _watchedRequestId" in template
    assert "s.outcome === 'success' || s.outcome === 'partial'" in template
    assert "if (_reloadRequestId !== null && _reloadRequestId !== key) cancelPendingReload();" in template
    assert "_reloadScheduled" not in template


def test_manual_update_frontend_status_dismissal_is_request_scoped():
    template = (Path(__file__).parents[1] / "src/manuscript_desk/templates/index.html").read_text()

    assert "const _dismissedRequestPrefix = 'manuscriptDesk.update.dismissed.';" in template
    assert "sessionSet(`${_dismissedRequestPrefix}${_statusRequestId}`, '1');" in template
    assert "isStatusDismissed(requestId)" in template
    assert "bar.style.display = '';" in template


def test_manual_update_enqueue_and_status(tmp_path):
    repo, _, _, app = build(tmp_path)
    owner = add_user(repo, "alice@example.test")
    repo.create_journal_config(
        owner,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "username": "alice",
            "provider": "scholarone",
        },
    )
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)
    first = client.post("/api/update", headers={"X-CSRF-Token": csrf}).json()
    assert first["phase"] == "queued" and first["active"] is True
    # A second enqueue while one is queued attaches to the active request.
    second = client.post("/api/update", headers={"X-CSRF-Token": csrf}).json()
    assert second["created"] is False
    assert second["request_id"] == first["request_id"]
    status = client.get("/api/update/status").json()
    assert status["active"] is True and status["phase"] == "queued"


def test_manual_update_without_enabled_journal_is_controlled(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    response = client.post("/api/update", headers={"X-CSRF-Token": session_csrf(client)})
    assert response.status_code == 409
    assert response.json()["error_code"] == "no_enabled_journals"
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM automation_run_requests").fetchone()[0] == 0


def test_manual_update_terminal_status_is_flat_and_sanitized(tmp_path):
    repo, _, _, app = build(tmp_path)
    owner = add_user(repo, "alice@example.test")
    repo.create_journal_config(
        owner,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "username": "alice",
            "provider": "scholarone",
        },
    )
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)
    started = client.post("/api/update", headers={"X-CSRF-Token": csrf}).json()
    claimed = repo.claim_next_run(owner, "test-worker")
    run_id = repo.start_claimed_run(owner, claimed["id"], "test-worker")
    repo.finish_run(
        owner,
        run_id,
        "failed",
        {
            "result": "failed",
            "error": "browser_start_failed",
            "attempted_journals": 1,
            "successful_journals": 0,
            "failed_journals": 1,
        },
    )

    status = client.get("/api/update/status").json()

    assert status["request_id"] == started["request_id"]
    assert status["active"] is False and status["phase"] == "failed"
    assert status["outcome"] == "failed" and status["error_code"] == "browser_start_failed"
    assert "自动化浏览器无法启动" in status["message"]
    rendered = str(status)
    assert "SessionNotCreatedException" not in rendered and "chromedriver" not in rendered.casefold()


def test_admin_invitation_html_is_idempotent_and_token_free(tmp_path):
    repo, _, mailer, app = build(tmp_path)
    add_user(repo, "admin@example.test", admin=True)
    client = TestClient(app)
    html_login(client, "admin@example.test")
    csrf = session_csrf(client)
    payload = {
        "email": "invite@example.test",
        "username": "inviteuser",
        "display_name": "Invite",
        "csrf_token": csrf,
    }

    first = client.post("/admin/users/invite", data=payload)
    assert first.status_code == 200 and "邀请已发送。" in first.text
    assert len(mailer.invitations) == 1
    second = client.post("/admin/users/invite", data=payload)
    assert second.status_code == 200 and "邀请已存在，无需重复创建" in second.text
    assert len(mailer.invitations) == 1
    assert "raw-token-dialog" not in second.text
    assert "#token=" not in second.text
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email='invite@example.test'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM activation_tokens").fetchone()[0] == 1


def test_admin_invitation_resend_rotates_token(tmp_path):
    repo, _, mailer, app = build(tmp_path)
    add_user(repo, "admin@example.test", admin=True)
    client = TestClient(app)
    html_login(client, "admin@example.test")
    csrf = session_csrf(client)
    client.post(
        "/admin/users/invite",
        data={
            "email": "invite@example.test",
            "username": "inviteuser",
            "display_name": "Invite",
            "csrf_token": csrf,
        },
    )
    invited = repo.get_user_by_email("invite@example.test")
    first_token = mailer.invitations[-1][1]
    response = client.post(f"/admin/users/{invited['id']}/resend-invitation", data={"csrf_token": csrf})
    assert response.status_code == 200 and "旧链接已失效" in response.text
    second_token = mailer.invitations[-1][1]
    assert second_token != first_token
    assert len(mailer.invitations) == 2


def test_automation_jobs_owner_scoped_and_cron_preview(tmp_path):
    repo, _, _, app = build(tmp_path)
    add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)
    headers = {"X-CSRF-Token": csrf}
    # Cron preview returns concrete future timestamps.
    preview = client.post(
        "/api/automation/cron-preview",
        json={"cron_expression": "0 7 * * *", "timezone": "Asia/Shanghai"},
        headers=headers,
    )
    assert preview.status_code == 200 and len(preview.json()["next_runs"]) == 5
    # Create an email job scoped to the tenant.
    created = client.post(
        "/api/automation/jobs",
        json={
            "kind": "email",
            "name": "Daily",
            "cron_expression": "0 7 * * *",
            "timezone": "Asia/Shanghai",
            "enabled": True,
            "manuscript_scope": "working",
            "delivery_policy": "changes_only",
            "recipients": ["alice@example.test"],
        },
        headers=headers,
    )
    assert created.status_code == 200
    jobs = client.get("/api/automation/jobs").json()
    assert len(jobs["jobs"]) == 1 and jobs["jobs"][0]["recipients"] == ["alice@example.test"]
    assert jobs["jobs"][0]["manuscript_scope"] == "working"
    stored_job = repo.get_automation_job(repo.get_user_by_email("alice@example.test")["id"], created.json()["id"])
    assert stored_job["payload"]["manuscript_scope"] == "working"
    invalid_scope = client.post(
        "/api/automation/jobs",
        json={
            "kind": "email",
            "name": "Invalid",
            "cron_expression": "0 7 * * *",
            "timezone": "Asia/Shanghai",
            "manuscript_scope": "unknown",
            "recipients": ["alice@example.test"],
        },
        headers=headers,
    )
    assert invalid_scope.status_code == 400
    assert jobs["scheduler"]["is_running"] is False

    rare_preview = client.post(
        "/api/automation/cron-preview",
        json={"cron_expression": "15 8 * * *", "timezone": "Asia/Kathmandu"},
        headers=headers,
    )
    assert rare_preview.status_code == 200
    rare_job = client.post(
        "/api/automation/jobs",
        json={
            "kind": "update",
            "name": "Chatham",
            "cron_expression": "30 9 * * *",
            "timezone": "Pacific/Chatham",
            "enabled": True,
        },
        headers=headers,
    )
    assert rare_job.status_code == 200
    assert (
        repo.get_automation_job(repo.get_user_by_email("alice@example.test")["id"], rare_job.json()["id"])["timezone"]
        == "Pacific/Chatham"
    )
    invalid = client.post(
        "/api/automation/cron-preview", json={"cron_expression": "0 7 * * *", "timezone": "UTC+8"}, headers=headers
    )
    assert invalid.status_code == 400


def test_automation_runs_api_uses_job_or_request_kind(tmp_path):
    repo, _, _, app = build(tmp_path)
    owner = add_user(repo, "alice@example.test")
    client = TestClient(app)
    html_login(client, "alice@example.test")

    with repo.connection() as conn:
        request_id = conn.execute(
            """INSERT INTO automation_run_requests
               (owner_user_id,kind,source,scheduled_for,status,requested_by_user_id)
               VALUES(?,'update','manual',CURRENT_TIMESTAMP,'running',?)""",
            (owner, owner),
        ).lastrowid
        conn.execute(
            """INSERT INTO automation_job_runs
               (owner_user_id,request_id,status,summary_json,started_at)
               VALUES(?,?,'running','{}',CURRENT_TIMESTAMP)""",
            (owner, request_id),
        )

    response = client.get("/api/automation/runs")
    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["kind"] == "update"
    assert run["job_name"] == "手动"
    assert "summary" in run and "summary_json" not in run
    assert run["summary"]["message_code"] == "historical_summary_unavailable"
    page = client.get("/automation")
    assert page.status_code == 200
    assert 'class="run-table"' in page.text
    assert 'id="run-summary-dialog"' not in page.text
    script = (Path(__file__).parents[1] / "src/manuscript_desk/static/automation.js").read_text()
    assert "summary_json" not in script
    assert "data-run-summary" not in script
    assert "部分完成" in script and "没有发现变化" in script


def test_shortcut_token_links_are_persisted_and_scoped(tmp_path):
    repo, store, _, app = build(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config = repo.create_journal_config(
        owner,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "short_name": "JRN",
            "username": "alice",
        },
    )
    repo.save_manuscript(
        owner,
        config,
        {
            "original_id": "STAR",
            "manuscript_id": "STAR",
            "title": "Starred paper",
            "journal": "Journal",
            "status": "Under Review",
            "detailed_status": "Under Review",
        },
    )
    repo.toggle_star(owner, "STAR")
    client = TestClient(app)
    html_login(client, "alice@example.test")
    csrf = session_csrf(client)
    shortcuts = client.get("/shortcuts")
    assert shortcuts.status_code == 200
    assert "请先保存网站地址" in shortcuts.text
    assert (
        client.post(
            "/shortcuts/base-url", data={"base_url": "http://203.0.113.20:8123/", "csrf_token": csrf}
        ).status_code
        == 200
    )
    created = client.post(
        "/shortcuts/tokens",
        data={"label": "iPhone Shortcuts", "csrf_token": csrf},
    )
    assert created.status_code == 200
    assert "http://203.0.113.20:8123/starred?api=" in created.text
    assert "/update_starred?api=" in created.text
    assert "/working?api=" in created.text
    assert "/update_working?api=" in created.text
    with repo.connection() as conn:
        token = dict(conn.execute("SELECT * FROM api_tokens WHERE owner_user_id=?", (owner,)).fetchone())
    assert token["scopes"] == "starred:read starred:update working:read working:update"
    assert token["secret_ref"].startswith(f"users/{owner}/tokens/")
    raw = store.read(token["secret_ref"])
    assert raw not in token["token_hash"]

    fresh = TestClient(app)
    starred = fresh.get(f"/starred?api={raw}")
    assert starred.status_code == 200
    assert "Starred paper" in starred.text
    assert ">JRN</td>" in starred.text
    assert "ago</td>" in starred.text or ">today</td>" in starred.text
    assert 'class="bar"' in starred.text
    assert "<table>" in starred.text
    assert 'class="chip c-t"' in starred.text
    assert "只读标星稿件状态" in starred.text
    assert starred.headers["referrer-policy"] == "no-referrer"
    assert starred.headers["cache-control"] == "no-store"
    working = fresh.get(f"/working?api={raw}")
    assert working.status_code == 200
    assert "Starred paper" in working.text
    assert "只读进行中稿件状态" in working.text
    assert working.headers["cache-control"] == "no-store"

    result = {}

    def open_update():
        result["response"] = fresh.get(f"/update_starred?api={raw}")

    waiter = threading.Thread(target=open_update)
    waiter.start()
    deadline = time.time() + 3
    request_row = None
    while time.time() < deadline:
        with repo.connection() as conn:
            request_row = conn.execute(
                """SELECT * FROM automation_run_requests
                   WHERE owner_user_id=? AND kind='update' ORDER BY id DESC LIMIT 1""",
                (owner,),
            ).fetchone()
        if request_row:
            break
        time.sleep(0.01)
    assert request_row is not None
    assert waiter.is_alive()
    claimed = repo.claim_next_run(owner, "test-worker")
    run_id = repo.start_claimed_run(owner, claimed["id"], "test-worker")
    repo.save_manuscript(
        owner,
        config,
        {
            "original_id": "STAR",
            "manuscript_id": "STAR",
            "title": "Starred paper",
            "journal": "Journal",
            "status": "Revision",
            "detailed_status": "Major Revision",
            "recorded_at": "2026-08-30 12:34:56",
        },
    )
    repo.finish_run(
        owner,
        run_id,
        "success",
        {
            "attempted_journals": 1,
            "successful_journals": 1,
            "failed_journals": 0,
            "update_scope": "starred",
        },
    )
    waiter.join(timeout=3)
    assert not waiter.is_alive()
    response = result["response"]
    assert response.status_code == 200
    assert "已更新 1/1 个标星期刊" in response.text
    assert "Major Revision" in response.text
    with repo.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM automation_run_requests WHERE owner_user_id=? AND kind='update'",
                (owner,),
            ).fetchone()[0]
            == 1
        )
    alias_result = {}

    def open_legacy_alias():
        alias_result["response"] = fresh.get(f"/renew_starred?api={raw}")

    alias_waiter = threading.Thread(target=open_legacy_alias)
    alias_waiter.start()
    deadline = time.time() + 3
    second_request = None
    while time.time() < deadline:
        with repo.connection() as conn:
            second_request = conn.execute(
                "SELECT * FROM automation_run_requests WHERE owner_user_id=? ORDER BY id DESC LIMIT 1",
                (owner,),
            ).fetchone()
        if second_request and second_request["id"] != request_row["id"]:
            break
        time.sleep(0.01)
    assert second_request and second_request["id"] != request_row["id"]
    claimed = repo.claim_next_run(owner, "test-worker-alias")
    alias_run = repo.start_claimed_run(owner, claimed["id"], "test-worker-alias")
    repo.finish_run(
        owner,
        alias_run,
        "no_changes",
        {
            "attempted_journals": 1,
            "successful_journals": 1,
            "failed_journals": 0,
            "update_scope": "starred",
        },
    )
    alias_waiter.join(timeout=3)
    assert alias_result["response"].status_code == 200
    assert fresh.get(f"/starred/ack-all?api={raw}").status_code == 403
    assert fresh.get(f"/api/starred?api={raw}").status_code == 404

    revoked = client.post(
        f"/shortcuts/tokens/{token['id']}/revoke",
        data={"csrf_token": csrf},
    )
    assert revoked.status_code == 200
    assert fresh.get(f"/starred?api={raw}").status_code == 401
    with pytest.raises(FileNotFoundError):
        store.read(token["secret_ref"])


def test_shortcut_renew_timeout_continues_same_request(tmp_path):
    repo, store, _, app = build(tmp_path, shortcut_wait_seconds=0.05, shortcut_poll_seconds=0.01)
    owner = add_user(repo, "alice@example.test")
    config = repo.create_journal_config(
        owner,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "short_name": "JRN",
            "username": "alice",
        },
    )
    repo.save_manuscript(
        owner,
        config,
        {
            "original_id": "STAR",
            "manuscript_id": "STAR",
            "title": "Starred paper",
            "journal": "Journal",
            "status": "Under Review",
            "detailed_status": "Under Review",
        },
    )
    repo.toggle_star(owner, "STAR")
    session = TestClient(app)
    html_login(session, "alice@example.test")
    csrf = session_csrf(session)
    assert (
        session.post(
            "/shortcuts/base-url",
            data={"base_url": "https://desk.example.test/research/", "csrf_token": csrf},
        ).status_code
        == 200
    )
    session.post("/shortcuts/tokens", data={"label": "Shortcut", "csrf_token": csrf})
    with repo.connection() as conn:
        token = dict(conn.execute("SELECT * FROM api_tokens WHERE owner_user_id=?", (owner,)).fetchone())
    raw = store.read(token["secret_ref"])

    fresh = TestClient(app)
    pending = fresh.get(f"/update_starred?api={raw}")
    assert pending.status_code == 200
    assert "更新仍在进行" in pending.text
    assert raw not in pending.text
    match = re.search(r'href="([^"]*continue=[^"]+)"', pending.text)
    assert match
    continuation_url = match.group(1).replace("&amp;", "&")
    assert fresh.get(continuation_url.replace("/update_starred", "/update_working")).status_code == 401
    with repo.connection() as conn:
        request = dict(conn.execute("SELECT * FROM automation_run_requests WHERE owner_user_id=?", (owner,)).fetchone())
    claimed = repo.claim_next_run(owner, "test-worker")
    run_id = repo.start_claimed_run(owner, claimed["id"], "test-worker")
    repo.finish_run(
        owner,
        run_id,
        "no_changes",
        {
            "attempted_journals": 1,
            "successful_journals": 1,
            "failed_journals": 0,
        },
    )

    completed = fresh.get(continuation_url)
    assert completed.status_code == 200
    assert "没有发现变化" in completed.text
    reused = fresh.get(continuation_url)
    assert reused.status_code == 200
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM automation_run_requests WHERE owner_user_id=?", (owner,)).fetchone()[0]
            == 1
        )
    session.post(f"/shortcuts/tokens/{token['id']}/revoke", data={"csrf_token": csrf})
    assert fresh.get(continuation_url).status_code == 401


def test_legacy_json_token_api_is_not_exposed(tmp_path):
    repo, _, _, app = build(tmp_path)
    alice = add_user(repo, "alice@example.test")
    raw = "legacy-bearer-token-for-testing"
    repo.create_api_token(alice, raw, "legacy", ["manuscripts:read"])

    client = TestClient(app)
    assert client.get("/api/starred").status_code == 404
    assert client.get("/api/starred", headers={"Authorization": f"Bearer {raw}"}).status_code == 404
    assert client.post("/api/starred/acknowledge", headers={"Authorization": f"Bearer {raw}"}).status_code == 404
    assert client.get(f"/update_starred?api={raw}").status_code == 401
    assert client.get(f"/renew_starred?api={raw}").status_code == 401
    assert client.get(f"/starred?api={raw}").status_code == 401
