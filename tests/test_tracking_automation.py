import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from manuscript_desk.automation_runtime import AutomationRuntime
from manuscript_desk.repository import Repository
from manuscript_desk.schema import create_database
from manuscript_desk.secrets_store import SecretStore
from manuscript_desk.security import hash_password
from manuscript_desk.tracking_service import TenantRunContext, TrackingService


def setup(tmp_path):
    db = tmp_path / "tenant.db"
    create_database(db)
    repo = Repository(db)
    secrets = SecretStore(tmp_path / "secrets")
    alice = repo.create_user(
        "alice@example.test", "Alice", hash_password("correct horse battery staple1"), verified=True
    )
    bob = repo.create_user("bob@example.test", "Bob", hash_password("correct horse battery staple1"), verified=True)
    return repo, secrets, alice, bob


def config(repo, secrets, owner, name):
    ref = secrets.write(str(owner), "credentials", "test-password")
    return repo.create_journal_config(
        owner,
        {
            "journal_name": name,
            "journal_url": "https://example.test",
            "username": "same@example.test",
            "provider": "scholarone",
            "credential_ref": ref,
        },
    )


class FakeProvider:
    def fetch(self, cfg, credentials):
        assert credentials.password == "test-password"
        return [
            {
                "original_id": "COLLIDE",
                "manuscript_id": "COLLIDE",
                "title": cfg["journal_name"],
                "status": "Under Review",
                "detailed_status": "Under Review",
            }
        ]

    def close(self):
        pass


def test_tracking_colliding_ids_stays_tenant_scoped(tmp_path):
    repo, secrets, alice, bob = setup(tmp_path)
    config(repo, secrets, alice, "Alice Journal")
    config(repo, secrets, bob, "Bob Journal")
    service = TrackingService(repo, secrets, provider_factory=lambda provider, headless: FakeProvider())
    service.track_journals(TenantRunContext(alice, alice))
    service.track_journals(TenantRunContext(bob, bob))
    assert [r["title"] for r in repo.latest_manuscripts(alice)] == ["Alice Journal"]
    assert [r["title"] for r in repo.latest_manuscripts(bob)] == ["Bob Journal"]


class RecordingProvider:
    fetched = []

    def fetch(self, cfg, credentials):
        self.fetched.append(cfg["id"])
        return []

    def close(self):
        pass


def test_starred_update_runs_only_persisted_target_journals(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    second = config(repo, secrets, alice, "Second")
    third = config(repo, secrets, alice, "Third")
    for config_id, original_id in ((first, "A"), (second, "B"), (third, "C")):
        repo.save_manuscript(
            alice,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": original_id,
                "journal": original_id,
                "status": "Under Review",
                "detailed_status": "Under Review",
            },
        )
    repo.toggle_star(alice, "A")
    repo.toggle_star(alice, "B")
    target = repo.starred_update_target(alice)
    assert target["enabled_journal_config_ids"] == [first, second]

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    request = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "starred", "journal_config_ids": target["enabled_journal_config_ids"]},
    )["request"]
    repo.toggle_star(alice, "C")
    RecordingProvider.fetched = []
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")
    runtime.tracking = TrackingService(repo, secrets, provider_factory=lambda provider, headless: RecordingProvider())
    claimed = repo.claim_next_run(alice, runtime.worker_id)
    run_id = repo.start_claimed_run(alice, claimed["id"], runtime.worker_id)
    configs = repo.journal_configs_by_ids(alice, claimed["payload"]["journal_config_ids"])
    summary = runtime.tracking.track_journals(TenantRunContext(alice, alice, claimed["id"], run_id), configs)
    assert sorted(RecordingProvider.fetched) == [first, second]
    assert summary["attempted_journals"] == 2
    assert third not in RecordingProvider.fetched


def test_working_items_and_update_target_use_visible_ongoing_manuscripts(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    second = config(repo, secrets, alice, "Second")
    third = config(repo, secrets, alice, "Third")
    fourth = config(repo, secrets, alice, "Fourth")
    with repo.connection() as conn:
        conn.execute(
            "UPDATE journal_configs SET enabled=0 WHERE id=? AND owner_user_id=?",
            (second, alice),
        )
    for config_id, original_id, status in (
        (first, "REVIEW", "Under Review"),
        (second, "REVISION", "Revision"),
        (third, "ACCEPTED", "Accept"),
        (fourth, "HIDDEN", "Under Review"),
    ):
        repo.save_manuscript(
            alice,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": original_id,
                "status": status,
                "detailed_status": status,
            },
        )
    repo.set_hidden(alice, "HIDDEN", True)

    assert {item["original_id"] for item in repo.email_items(alice, "working")} == {"REVIEW", "REVISION"}
    target = repo.working_update_target(alice)
    assert target == {
        "working_manuscript_count": 2,
        "journal_config_ids": [first, second],
        "enabled_journal_config_ids": [first],
    }


def test_working_update_payload_is_scoped_and_covered_by_all(tmp_path):
    repo, secrets, alice, bob = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    foreign = config(repo, secrets, bob, "Foreign")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    request = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "working", "journal_config_ids": [first, first]},
    )
    assert request["request"]["payload"] == {"update_scope": "working", "journal_config_ids": [first]}
    duplicate = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "working", "journal_config_ids": [first]},
    )
    assert duplicate["coverage"] == "same"
    with pytest.raises(PermissionError):
        repo._normalize_run_payload(alice, "update", {"update_scope": "working", "journal_config_ids": [foreign]})

    repo.finish_run(
        alice,
        repo.start_claimed_run(alice, repo.claim_next_run(alice, "worker")["id"], "worker"),
        "success",
    )
    all_request = repo.enqueue_run(alice, "update", now, requested_by_user_id=alice)
    covered = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "working", "journal_config_ids": [first]},
    )
    assert all_request["created"] is True and covered["coverage"] == "superset"


def test_runtime_executes_persisted_working_target(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    second = config(repo, secrets, alice, "Second")
    RecordingProvider.fetched = []
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")
    runtime.tracking = TrackingService(repo, secrets, provider_factory=lambda provider, headless: RecordingProvider())
    claimed = {
        "id": 41,
        "payload": {"update_scope": "working", "journal_config_ids": [first]},
    }

    summary = runtime._run_update(alice, claimed, 99)

    assert RecordingProvider.fetched == [first]
    assert second not in RecordingProvider.fetched
    assert summary["update_scope"] == "working"
    assert summary["requested_journals"] == 1


def test_working_update_without_enabled_target_reports_working_reason(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config_id = config(repo, secrets, alice, "Disabled")
    with repo.connection() as conn:
        conn.execute(
            "UPDATE journal_configs SET enabled=0 WHERE id=? AND owner_user_id=?",
            (config_id, alice),
        )
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")
    summary = runtime._run_update(
        alice,
        {
            "id": 42,
            "payload": {"update_scope": "working", "journal_config_ids": [config_id]},
        },
        100,
    )
    assert summary["result"] == "no_changes"
    assert summary["reason"] == "no_enabled_working_journals"
    assert summary["update_scope"] == "working"


def test_hidden_items_are_excluded_from_email_and_starred_targets(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    second = config(repo, secrets, alice, "Second")
    for config_id, original_id in ((first, "A"), (second, "B")):
        repo.save_manuscript(
            alice,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": original_id,
                "status": "Under Review",
                "detailed_status": "Under Review",
            },
        )
        repo.toggle_star(alice, original_id)
    repo.set_hidden(alice, "A", True)
    assert [item["original_id"] for item in repo.email_items(alice, "all")] == ["B"]
    assert [item["original_id"] for item in repo.email_items(alice, "starred")] == ["B"]
    target = repo.starred_update_target(alice)
    assert target["starred_manuscript_count"] == 1
    assert target["enabled_journal_config_ids"] == [second]

    journey = repo.create_submission_journey(alice, ["A", "B"])
    repo.set_hidden(alice, "A", False)
    repo.set_submission_journey_hidden(alice, journey["id"], True)
    assert repo.email_items(alice, "all") == []
    assert repo.starred_update_target(alice)["starred_manuscript_count"] == 0


def test_queue_claims_are_isolated_and_serialized(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    a = repo.enqueue_run(alice, "update", now.isoformat(), requested_by_user_id=alice)["request"]["id"]
    b = repo.enqueue_run(bob, "update", now.isoformat(), requested_by_user_id=bob)["request"]["id"]
    assert repo.claim_next_run(alice, "worker-a", now=now)["id"] == a
    assert repo.claim_next_run(alice, "worker-b", now=now) is None
    assert repo.claim_next_run(bob, "worker-b", now=now)["id"] == b
    later = now + timedelta(seconds=301)
    assert repo.claim_next_run(alice, "worker-c", now=later)["id"] == a


def test_active_run_uniqueness_and_terminal_requeue(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    first = repo.enqueue_run(alice, "update", now.isoformat(), requested_by_user_id=alice)
    duplicate = repo.enqueue_run(alice, "update", (now + timedelta(seconds=1)).isoformat(), requested_by_user_id=alice)
    other_tenant = repo.enqueue_run(bob, "update", now.isoformat(), requested_by_user_id=bob)
    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["request"]["id"] == first["request"]["id"]
    assert other_tenant["created"] is True

    claimed = repo.claim_next_run(alice, "worker", now=now)
    run_id = repo.start_claimed_run(alice, claimed["id"], "worker")
    assert repo.finish_run(alice, run_id, "success", {"result": "success"})
    next_run = repo.enqueue_run(alice, "update", (now + timedelta(seconds=2)).isoformat(), requested_by_user_id=alice)
    assert next_run["created"] is True
    assert next_run["request"]["id"] != first["request"]["id"]


def test_email_run_uniqueness_is_per_job(tmp_path):
    repo, _, alice, _ = setup(tmp_path)
    first_job = repo.save_automation_job(
        alice,
        {
            "kind": "email",
            "name": "First",
            "cron_expression": "0 7 * * *",
            "recipients": ["alice@example.test"],
        },
    )
    second_job = repo.save_automation_job(
        alice,
        {
            "kind": "email",
            "name": "Second",
            "cron_expression": "0 8 * * *",
            "recipients": ["alice@example.test"],
        },
    )
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    first = repo.enqueue_run(alice, "email", now, job_id=first_job, requested_by_user_id=alice)
    duplicate = repo.enqueue_run(alice, "email", now, job_id=first_job, requested_by_user_id=alice)
    separate = repo.enqueue_run(alice, "email", now, job_id=second_job, requested_by_user_id=alice)
    assert first["created"] is True
    assert duplicate["created"] is False
    assert separate["created"] is True
    assert set(repo.active_email_requests(alice)) == {first_job, second_job}


def test_run_request_status_is_tenant_scoped(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request = repo.enqueue_run(alice, "update", now.isoformat(), requested_by_user_id=alice)["request"]
    assert repo.get_run_request(alice, request["id"])["status"] == "queued"
    assert repo.get_run_request(bob, request["id"]) is None
    claimed = repo.claim_next_run(alice, "worker", now=now)
    assert repo.get_run_request(alice, claimed["id"])["status"] == "claimed"
    run_id = repo.start_claimed_run(alice, claimed["id"], "worker")
    running = repo.get_run_request(alice, claimed["id"])
    assert running["status"] == "running" and running["result"] == "running"
    repo.finish_run(alice, run_id, "partial", {"result": "partial", "failed_journals": 1})
    finished = repo.get_run_request(alice, claimed["id"])
    assert finished["status"] == "finished" and finished["result"] == "partial"
    assert finished["summary"]["failed_journals"] == 1


def test_smtp_config_is_owned(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    repo.save_user_smtp(
        alice,
        {
            "host": "smtp.alice.test",
            "port": 465,
            "security_mode": "ssl",
            "sender_address": "alice@example.test",
            "credential_ref": f"users/{alice}/smtp/a",
        },
    )
    assert repo.get_user_smtp(alice)["host"] == "smtp.alice.test"
    assert repo.get_user_smtp(bob) is None


class RecordingSMTP:
    messages = []

    def __init__(self, *args, **kwargs):
        pass

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message, to_addrs=None):
        self.messages.append(message)

    def quit(self):
        pass

    def close(self):
        pass


def test_digest_html_marks_only_unread_statuses(monkeypatch, tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    smtp_ref = secrets.write(str(alice), "smtp", "smtp-password")
    repo.save_user_smtp(
        alice,
        {
            "host": "smtp.example.test",
            "port": 465,
            "security_mode": "ssl",
            "username": "alice",
            "sender_address": "alice@example.test",
            "sender_name": "Alice",
            "credential_ref": smtp_ref,
        },
    )
    config_id = repo.create_journal_config(
        alice,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "username": "alice",
            "provider": "scholarone",
        },
    )
    for original_id, title, status in (
        ("NEW", "New status paper", "Revision"),
        ("SAME", "Read status paper", "Under Review"),
    ):
        repo.save_manuscript(
            alice,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": title,
                "journal": "Journal",
                "status": status,
                "detailed_status": status,
            },
        )
        repo.acknowledge(alice, original_id)
    repo.save_manuscript(
        alice,
        config_id,
        {
            "original_id": "NEW",
            "manuscript_id": "NEW.R1",
            "title": "New status paper",
            "journal": "Journal",
            "version_number": 2,
            "status": "Accept",
            "detailed_status": "Accept",
        },
    )
    job_id = repo.save_automation_job(
        alice,
        {
            "kind": "email",
            "name": "Digest",
            "cron_expression": "0 7 * * *",
            "payload": {"manuscript_scope": "all", "delivery_policy": "always"},
            "recipients": ["alice@example.test"],
        },
    )
    with repo.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO automation_job_runs(owner_user_id,job_id,status) VALUES(?,?,'running')",
            (alice, job_id),
        ).lastrowid
    RecordingSMTP.messages = []
    monkeypatch.setattr("manuscript_desk.automation_runtime.smtplib.SMTP_SSL", RecordingSMTP)
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")

    status, summary = runtime._run_email(alice, job_id, run_id)

    assert status == "success"
    assert summary["item_count"] == 2
    assert summary["new_status_count"] == 1
    message = RecordingSMTP.messages[-1]
    assert message["Subject"] == "Manuscript Desk · 1/2 篇新状态"
    plain = message.get_body(preferencelist=("plain",)).get_content()
    rich = message.get_body(preferencelist=("html",)).get_content()
    assert "标记为已读" in plain and "链接有效期为一周" in plain
    assert "标记为已读" in rich and "新状态" in rich
    with repo.connection() as conn:
        token = conn.execute(
            "SELECT expires_at FROM automation_ack_tokens WHERE owner_user_id=?",
            (alice,),
        ).fetchone()
        delivery_items = conn.execute(
            "SELECT original_id FROM automation_email_delivery_items WHERE owner_user_id=?",
            (alice,),
        ).fetchall()
    assert token is not None
    expires = datetime.fromisoformat(token["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)
    assert [row["original_id"] for row in delivery_items] == ["NEW"]


def test_working_digest_only_includes_visible_ongoing_manuscripts(monkeypatch, tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    smtp_ref = secrets.write(str(alice), "smtp", "smtp-password")
    repo.save_user_smtp(
        alice,
        {
            "host": "smtp.example.test",
            "port": 465,
            "security_mode": "ssl",
            "sender_address": "alice@example.test",
            "credential_ref": smtp_ref,
        },
    )
    config_id = repo.create_journal_config(
        alice, {"journal_name": "Journal", "journal_url": "https://example.test", "username": "alice"}
    )
    for original_id, status in (
        ("REVIEW", "Under Review"),
        ("REVISION", "Revision"),
        ("ACCEPTED", "Accept"),
        ("HIDDEN", "Under Review"),
    ):
        repo.save_manuscript(
            alice,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": original_id,
                "journal": "Journal",
                "status": status,
                "detailed_status": status,
            },
        )
    repo.set_hidden(alice, "HIDDEN", True)
    job_id = repo.save_automation_job(
        alice,
        {
            "kind": "email",
            "name": "Working Digest",
            "cron_expression": "0 7 * * *",
            "payload": {"manuscript_scope": "working", "delivery_policy": "always"},
            "recipients": ["alice@example.test"],
        },
    )
    with repo.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO automation_job_runs(owner_user_id,job_id,status) VALUES(?,?,'running')",
            (alice, job_id),
        ).lastrowid
    RecordingSMTP.messages = []
    monkeypatch.setattr("manuscript_desk.automation_runtime.smtplib.SMTP_SSL", RecordingSMTP)
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")

    status, summary = runtime._run_email(alice, job_id, run_id)

    assert status == "success" and summary["item_count"] == 2
    plain = RecordingSMTP.messages[-1].get_body(preferencelist=("plain",)).get_content()
    assert "REVIEW" in plain and "REVISION" in plain
    assert "ACCEPTED" not in plain and "HIDDEN" not in plain


def test_digest_without_unread_status_has_no_mark_read_link(monkeypatch, tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    smtp_ref = secrets.write(str(alice), "smtp", "smtp-password")
    repo.save_user_smtp(
        alice,
        {
            "host": "smtp.example.test",
            "port": 465,
            "security_mode": "ssl",
            "sender_address": "alice@example.test",
            "credential_ref": smtp_ref,
        },
    )
    config_id = repo.create_journal_config(
        alice,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test",
            "username": "alice",
            "provider": "scholarone",
        },
    )
    repo.save_manuscript(
        alice,
        config_id,
        {
            "original_id": "READ",
            "manuscript_id": "READ",
            "title": "Read paper",
            "journal": "Journal",
            "status": "Under Review",
            "detailed_status": "Under Review",
        },
    )
    repo.acknowledge(alice, "READ")
    job_id = repo.save_automation_job(
        alice,
        {
            "kind": "email",
            "name": "Digest",
            "cron_expression": "0 7 * * *",
            "payload": {"manuscript_scope": "all", "delivery_policy": "always"},
            "recipients": ["alice@example.test"],
        },
    )
    with repo.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO automation_job_runs(owner_user_id,job_id,status) VALUES(?,?,'running')",
            (alice, job_id),
        ).lastrowid
    RecordingSMTP.messages = []
    monkeypatch.setattr("manuscript_desk.automation_runtime.smtplib.SMTP_SSL", RecordingSMTP)
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")

    status, summary = runtime._run_email(alice, job_id, run_id)

    assert status == "success" and summary["new_status_count"] == 0
    message = RecordingSMTP.messages[-1]
    assert message["Subject"] == "Manuscript Desk · 0/1 篇新状态"
    assert "标记为已读" not in message.get_body(preferencelist=("plain",)).get_content()
    assert "标记为已读" not in message.get_body(preferencelist=("html",)).get_content()
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM automation_ack_tokens").fetchone()[0] == 0


def test_ack_token_cannot_cross_tenant(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    ac = repo.create_journal_config(alice, {"journal_name": "A", "journal_url": "https://a.test", "username": "a"})
    bc = repo.create_journal_config(bob, {"journal_name": "B", "journal_url": "https://b.test", "username": "b"})
    for owner, cfg, title in ((alice, ac, "Alice"), (bob, bc, "Bob")):
        repo.save_manuscript(
            owner,
            cfg,
            {
                "original_id": "COLLIDE",
                "manuscript_id": "COLLIDE",
                "title": title,
                "status": "Revision",
                "detailed_status": "Revision",
            },
        )
    job = repo.save_automation_job(
        alice, {"kind": "email", "name": "mail", "cron_expression": "0 7 * * *", "recipients": ["alice@example.test"]}
    )
    raw = "ack-token-for-test-that-is-long-enough"
    delivery = repo.create_email_delivery(
        alice,
        job,
        repo.email_items(alice),
        "<a@example.test>",
        hashlib.sha256(raw.encode()).hexdigest(),
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    repo.mark_email_delivery(alice, delivery, "sent")
    assert repo.consume_ack_token(bob, raw) is None
    assert repo.consume_ack_token(alice, raw) == {"acknowledged": 1, "skipped_newer": 0}
    assert repo.consume_ack_token(alice, raw) is None
