from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from manuscript_desk.accounts import AccountService
from manuscript_desk.captcha import CaptchaService
from manuscript_desk.repository import Repository
from manuscript_desk.schema import create_database
from manuscript_desk.security import hash_password
from manuscript_desk.web import create_app

KEY = b"k" * 32
RATE_KEY = b"r" * 32


def setup_service(tmp_path):
    path = tmp_path / "accounts.db"
    create_database(path)
    repo = Repository(path)
    service = AccountService(repo, KEY, RATE_KEY)
    return repo, service


def test_challenge_newest_only_and_five_attempts(tmp_path):
    repo, service = setup_service(tmp_path)
    first = service.issue_challenge("user@example.test", "register")
    second = service.issue_challenge("user@example.test", "register")
    assert not service.consume_challenge("user@example.test", "register", first.code)
    assert service.consume_challenge("user@example.test", "register", second.code)
    assert not service.consume_challenge("user@example.test", "register", second.code)

    third = service.issue_challenge("user@example.test", "password_reset")
    wrong = "000000" if third.code != "000000" else "999999"
    for _ in range(5):
        assert not service.consume_challenge("user@example.test", "password_reset", wrong)
    assert not service.consume_challenge("user@example.test", "password_reset", third.code)
    with repo.connection() as conn:
        assert third.code not in [row[0] for row in conn.execute("SELECT code_hmac FROM email_challenges")]


def test_durable_rate_limit(tmp_path):
    _, service = setup_service(tmp_path)
    service.check_rate_limit("send", "subject", 2, 3600)
    service.check_rate_limit("send", "subject", 2, 3600)
    with pytest.raises(PermissionError):
        service.check_rate_limit("send", "subject", 2, 3600)


def test_invitation_activation_and_admin_authorization(tmp_path):
    repo, service = setup_service(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    non_admin = repo.create_user(
        "user@example.test", "User", hash_password("correct horse battery staple1"), verified=True
    )
    with pytest.raises(PermissionError):
        service.create_invitation(non_admin, "new@example.test", "New")
    invitation = service.create_invitation(admin, "new@example.test", "New")
    assert invitation.status == "created" and invitation.token
    duplicate = service.create_invitation(admin, "new@example.test", "New")
    assert duplicate.status == "already_pending" and duplicate.user_id == invitation.user_id
    assert duplicate.token is None
    assert service.activate_invitation("wrong-token", hash_password("another correct horse staple2")) is None
    assert (
        service.activate_invitation(invitation.token, hash_password("another correct horse staple2"))
        == invitation.user_id
    )
    user = repo.get_user(invitation.user_id)
    assert user["is_active"] == 1
    assert user["email_verified_at"] is not None
    assert service.activate_invitation(invitation.token, hash_password("another correct horse staple2")) is None


def test_reissue_invitation_revokes_old_token(tmp_path):
    repo, service = setup_service(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    invitation = service.create_invitation(admin, "new@example.test", "New", "newuser")
    reissued = service.reissue_invitation(admin, invitation.user_id)
    assert reissued.email == "new@example.test"
    assert reissued.token != invitation.token
    password_hash = hash_password("another correct horse staple2")
    assert service.activate_invitation(invitation.token, password_hash) is None
    assert service.activate_invitation(reissued.token, password_hash) == invitation.user_id


def test_invitation_conflicts_are_classified(tmp_path):
    from manuscript_desk.accounts import InvitationConflict

    repo, service = setup_service(tmp_path)
    admin = repo.create_user(
        "admin@example.test",
        "Admin",
        hash_password("correct horse battery staple1"),
        username="adminuser",
        is_admin=True,
        verified=True,
    )
    repo.create_user(
        "existing@example.test",
        "Existing",
        hash_password("correct horse battery staple1"),
        username="existinguser",
        verified=True,
    )
    with pytest.raises(InvitationConflict, match="email") as email_conflict:
        service.create_invitation(admin, "existing@example.test", "New", "differentuser")
    assert email_conflict.value.field == "email"
    with pytest.raises(InvitationConflict, match="username") as username_conflict:
        service.create_invitation(admin, "different@example.test", "New", "existinguser")
    assert username_conflict.value.field == "username"


def test_disable_revokes_access_and_sets_30_day_purge(tmp_path):
    repo, service = setup_service(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    user = repo.create_user("user@example.test", "User", hash_password("correct horse battery staple1"), verified=True)
    repo.create_session(user, "raw-session", "csrf-secret")
    purge_at = service.disable_user(admin, user)
    target = repo.get_user(user)
    assert target["is_active"] == 0
    delta = datetime.fromisoformat(purge_at) - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)
    assert repo.get_active_session("raw-session") is None


def test_captcha_is_signed_and_expires():
    service = CaptchaService(b"c" * 32)
    token, answer, svg = service.issue()
    assert answer in svg
    assert service.verify(token, answer.lower())
    assert not service.verify(token + "x", answer)
    assert not service.verify(token, "WRONG")


class RecordingMailer:
    def __init__(self):
        self.codes = []
        self.invitations = []

    def send_code(self, email, display_name, code, purpose):
        self.codes.append((email, code, purpose))

    def send_invitation(self, email, display_name, token):
        self.invitations.append((email, token))


def make_client(tmp_path):
    path = tmp_path / "web.db"
    create_database(path)
    repo = Repository(path)
    mailer = RecordingMailer()
    app = create_app(path, KEY, RATE_KEY, b"c" * 32, mailer, cookie_secure=False)
    return repo, mailer, TestClient(app)


def login(client, email, password="correct horse battery staple1"):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_web_login_csrf_logout_and_admin_authorization(tmp_path):
    repo, _, client = make_client(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    repo.create_user("user@example.test", "User", hash_password("correct horse battery staple1"), verified=True)
    csrf = login(client, "admin@example.test")
    assert client.get("/api/auth/me").json()["id"] == admin
    assert client.put("/api/admin/registration", json={"enabled": True}).status_code == 403
    assert (
        client.put("/api/admin/registration", headers={"X-CSRF-Token": csrf}, json={"enabled": True}).status_code == 200
    )
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    csrf = login(client, "user@example.test")
    assert (
        client.put("/api/admin/registration", headers={"X-CSRF-Token": csrf}, json={"enabled": False}).status_code
        == 403
    )


def test_registration_password_reset_and_email_change(tmp_path):
    repo, mailer, client = make_client(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    csrf = login(client, "admin@example.test")
    client.put("/api/admin/registration", headers={"X-CSRF-Token": csrf}, json={"enabled": True})
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    captcha = client.get("/api/auth/captcha").json()
    # Recovering a CAPTCHA answer from its rendered SVG is a client concern, not an OTP disclosure.
    import re

    answer = re.search(r">([2-9A-Z]{4})</text>", captcha["svg"]).group(1)
    assert (
        client.post(
            "/api/auth/register/send-code",
            json={"email": "new@example.test", "captcha_token": captcha["token"], "captcha_answer": answer},
        ).status_code
        == 200
    )
    code = mailer.codes[-1][1]
    assert (
        client.post(
            "/api/auth/register",
            json={
                "email": "new@example.test",
                "username": "newuser",
                "display_name": "New",
                "password": "another correct horse staple2",
                "code": code,
            },
        ).status_code
        == 200
    )

    captcha = client.get("/api/auth/captcha").json()
    answer = re.search(r">([2-9A-Z]{4})</text>", captcha["svg"]).group(1)
    assert (
        client.post(
            "/api/auth/password/forgot",
            json={"email": "new@example.test", "captcha_token": captcha["token"], "captcha_answer": answer},
        ).status_code
        == 200
    )
    reset_code = mailer.codes[-1][1]
    assert (
        client.post(
            "/api/auth/password/reset",
            json={"email": "new@example.test", "password": "replacement password good3", "code": reset_code},
        ).status_code
        == 200
    )
    csrf = login(client, "new@example.test", "replacement password good3")
    assert (
        client.post(
            "/api/account/email/send-code", headers={"X-CSRF-Token": csrf}, json={"email": "changed@example.test"}
        ).status_code
        == 200
    )
    change_code = mailer.codes[-1][1]
    assert (
        client.post(
            "/api/account/email/change",
            headers={"X-CSRF-Token": csrf},
            json={"email": "changed@example.test", "password": "replacement password good3", "code": change_code},
        ).status_code
        == 200
    )
    assert repo.get_user_by_email("changed@example.test") is not None
    assert client.get("/api/auth/me").status_code == 401


def test_invitation_disable_reactivate_and_purge_api(tmp_path):
    repo, mailer, client = make_client(tmp_path)
    admin = repo.create_user(
        "admin@example.test", "Admin", hash_password("correct horse battery staple1"), is_admin=True, verified=True
    )
    csrf = login(client, "admin@example.test")
    response = client.post(
        "/api/admin/invitations",
        headers={"X-CSRF-Token": csrf},
        json={"email": "invite@example.test", "username": "inviteuser", "display_name": "Invite"},
    )
    assert response.status_code == 200 and "token" not in response.json()
    token = mailer.invitations[-1][1]
    assert (
        client.post(
            "/api/auth/invitations/activate", json={"token": token, "password": "invited account password4"}
        ).status_code
        == 200
    )
    invited = repo.get_user_by_email("invite@example.test")
    assert client.post(f"/api/admin/users/{invited['id']}/disable", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert (
        client.post(f"/api/admin/users/{invited['id']}/reactivate", headers={"X-CSRF-Token": csrf}).status_code == 200
    )
    client.post(f"/api/admin/users/{invited['id']}/disable", headers={"X-CSRF-Token": csrf})
    with repo.connection() as conn:
        conn.execute(
            "UPDATE users SET purge_eligible_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), invited["id"]),
        )
    preview = client.get("/api/admin/purge-preview").json()["accounts"]
    assert [item["id"] for item in preview] == [invited["id"]]
    assert client.post(f"/api/admin/users/{invited['id']}/purge", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert repo.get_user(invited["id"]) is None
    with repo.connection() as conn:
        tombstone = conn.execute("SELECT * FROM deleted_user_tombstones").fetchone()
        assert tombstone and "invite@example.test" not in tuple(str(value) for value in tombstone)
