import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from manuscript_desk.automation_runtime import AutomationRuntime
from manuscript_desk.repository import Repository
from manuscript_desk.schema import create_database
from manuscript_desk.secrets_store import SecretStore
from manuscript_desk.security import hash_password
from manuscript_desk.tracking_models import (
    BrowserTabCrashedError,
    BrowserTimeoutError,
    ProviderContractError,
    ProviderLoginError,
    ProviderUnavailableError,
    TrackingDiagnosticCode,
)
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


def test_tracking_retries_browser_crash_once_with_fresh_tracker(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config(repo, secrets, alice, "Crash Journal")
    trackers = []

    class CrashThenSucceed:
        def __init__(self, should_crash):
            self.should_crash = should_crash
            self.closed = False

        def fetch(self, cfg, credentials):
            if self.should_crash:
                raise BrowserTabCrashedError()
            return [{"original_id": "OK", "manuscript_id": "OK", "title": cfg["journal_name"]}]

        def close(self):
            self.closed = True

    def factory(_provider, _headless):
        tracker = CrashThenSucceed(not trackers)
        trackers.append(tracker)
        return tracker

    summary = TrackingService(repo, secrets, provider_factory=factory).track_journals(TenantRunContext(alice, alice))

    assert summary["result"] == "success"
    assert len(trackers) == 2 and all(tracker.closed for tracker in trackers)
    assert [row["original_id"] for row in repo.latest_manuscripts(alice)] == ["OK"]


def test_tracking_retries_provider_unavailable_once_with_fresh_tracker(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config(repo, secrets, alice, "Slow Journal")
    trackers = []

    class TimeoutThenSucceed:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.closed = False

        def fetch(self, cfg, credentials):
            if self.should_fail:
                raise ProviderUnavailableError()
            return [{"original_id": "OK", "manuscript_id": "OK", "title": cfg["journal_name"]}]

        def close(self):
            self.closed = True

    def factory(_provider, _headless):
        tracker = TimeoutThenSucceed(not trackers)
        trackers.append(tracker)
        return tracker

    summary = TrackingService(repo, secrets, provider_factory=factory).track_journals(TenantRunContext(alice, alice))

    assert summary["result"] == "success"
    assert len(trackers) == 2 and all(tracker.closed for tracker in trackers)
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM query_logs WHERE owner_user_id=?", (alice,)).fetchone()[0] == 1


def test_tracking_does_not_retry_unclassified_failure(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config(repo, secrets, alice, "Broken Journal")
    trackers = []

    class DeterministicFailure:
        def __init__(self):
            self.closed = False

        def fetch(self, _cfg, _credentials):
            raise KeyError("unexpected response shape")

        def close(self):
            self.closed = True

    def factory(_provider, _headless):
        tracker = DeterministicFailure()
        trackers.append(tracker)
        return tracker

    summary = TrackingService(repo, secrets, provider_factory=factory).track_journals(TenantRunContext(alice, alice))

    assert summary["error"] == "tracking_failed"
    assert len(trackers) == 1 and trackers[0].closed


def test_tracking_retries_browser_timeout_once_with_fresh_tracker(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config(repo, secrets, alice, "Timeout Journal")
    trackers = []

    class TimeoutThenSucceed:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.closed = False

        def fetch(self, cfg, credentials):
            if self.should_fail:
                raise BrowserTimeoutError(TrackingDiagnosticCode.PROVIDER_LOGIN_RESPONSE_TIMEOUT)
            return [{"original_id": "OK", "manuscript_id": "OK", "title": cfg["journal_name"]}]

        def close(self):
            self.closed = True

    def factory(_provider, _headless):
        tracker = TimeoutThenSucceed(not trackers)
        trackers.append(tracker)
        return tracker

    summary = TrackingService(repo, secrets, provider_factory=factory).track_journals(TenantRunContext(alice, alice))

    assert summary["result"] == "success"
    assert len(trackers) == 2 and all(tracker.closed for tracker in trackers)


def test_tracking_contract_error_is_terminal_and_carries_diagnostic(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config_id = config(repo, secrets, alice, "Layout Journal")
    repo.save_manuscript(
        alice,
        config_id,
        {"original_id": "OLD", "manuscript_id": "OLD", "title": "Existing", "status": "Under Review"},
    )
    trackers = []

    class ContractFailure:
        def __init__(self):
            self.closed = False

        def fetch(self, _cfg, _credentials):
            raise ProviderContractError(TrackingDiagnosticCode.PROVIDER_TABLE_HEADERS_CHANGED)

        def close(self):
            self.closed = True

    def factory(_provider, _headless):
        tracker = ContractFailure()
        trackers.append(tracker)
        return tracker

    now = datetime.now(timezone.utc).replace(microsecond=0)
    request = repo.enqueue_run(alice, "update", now.isoformat(), requested_by_user_id=alice)["request"]
    claimed = repo.claim_next_run(alice, "worker", now=now)
    run_id = repo.start_claimed_run(alice, claimed["id"], "worker")
    service = TrackingService(repo, secrets, provider_factory=factory)
    summary = service.track_journals(TenantRunContext(alice, alice, claimed["id"], run_id))
    repo.finish_run(alice, run_id, summary.get("result", "failed"), summary)

    # Deterministic layout regressions are never retried.
    assert len(trackers) == 1 and trackers[0].closed
    assert summary["journal_results"][0]["error_code"] == "tracking_failed"
    assert summary["journal_results"][0]["diagnostic_code"] == "provider_table_headers_changed"
    # Prior snapshot is retained.
    assert [row["original_id"] for row in repo.latest_manuscripts(alice)] == ["OLD"]

    item = repo.get_run_request(alice, request["id"])
    failure = item["summary"]["failures"][0]
    assert failure == {
        "code": "tracking_failed",
        "diagnostic_code": "provider_table_headers_changed",
        "journal_name": "Layout Journal",
    }
    rendered = json.dumps(item["summary"])
    assert "missing_headers" not in rendered and "TimeoutException" not in rendered


def test_editorial_manager_row_contract_error_is_safe():
    from manuscript_desk.editorial_manager_tracker import EditorialManagerTracker

    with pytest.raises(ProviderContractError) as excinfo:
        EditorialManagerTracker.parse_row_values(
            {"manuscript_id": 0},
            [""],
            "submissions_being_processed",
            "Layout Journal",
            7,
            "sitekey",
        )
    assert excinfo.value.error_code.value == "tracking_failed"
    assert excinfo.value.diagnostic_code == "provider_row_data_incomplete"
    assert "Layout Journal" not in str(excinfo.value)


def test_tracking_does_not_retry_auth_failure_or_persist_payload(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    config_id = config(repo, secrets, alice, "Auth Journal")
    repo.save_manuscript(
        alice,
        config_id,
        {"original_id": "OLD", "manuscript_id": "OLD", "title": "Existing", "status": "Under Review"},
    )
    created = []

    class AuthFailure:
        def fetch(self, _cfg, _credentials):
            raise ProviderLoginError()

        def close(self):
            pass

    def factory(_provider, _headless):
        created.append(1)
        return AuthFailure()

    summary = TrackingService(repo, secrets, provider_factory=factory).track_journals(TenantRunContext(alice, alice))

    assert summary["error"] == "provider_login_failed"
    assert len(created) == 1
    assert [row["original_id"] for row in repo.latest_manuscripts(alice)] == ["OLD"]
    assert summary["journal_results"][0]["manuscripts"] == []
    assert summary["journal_results"][0]["error_message"] == ""


def test_tracking_worker_default_is_two_and_configuration_is_bounded(monkeypatch, tmp_path):
    repo, secrets, _, _ = setup(tmp_path)
    monkeypatch.delenv("MANUSCRIPT_DESK_MAX_BROWSER_WORKERS", raising=False)
    assert TrackingService(repo, secrets).max_workers == 2
    monkeypatch.setenv("MANUSCRIPT_DESK_MAX_BROWSER_WORKERS", "99")
    assert TrackingService(repo, secrets).max_workers == 8
    assert TrackingService(repo, secrets, max_workers=0).max_workers == 1


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


def test_journal_update_payload_and_coverage_are_tenant_scoped(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    first = config(repo, _, alice, "First")
    second = config(repo, _, alice, "Second")
    foreign = config(repo, _, bob, "Foreign")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    request = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "journal", "journal_config_ids": [first]},
    )
    assert request["request"]["payload"] == {"update_scope": "journal", "journal_config_ids": [first]}
    same = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "journal", "journal_config_ids": [first]},
    )
    different = repo.enqueue_run(
        alice,
        "update",
        now,
        requested_by_user_id=alice,
        payload={"update_scope": "journal", "journal_config_ids": [second]},
    )
    assert same["coverage"] == "same"
    assert different["coverage"] == "different"
    with pytest.raises(ValueError):
        repo._normalize_run_payload(alice, "update", {"update_scope": "journal", "journal_config_ids": []})
    with pytest.raises(ValueError):
        repo._normalize_run_payload(alice, "update", {"update_scope": "journal", "journal_config_ids": [first, second]})
    with pytest.raises(PermissionError):
        repo._normalize_run_payload(alice, "update", {"update_scope": "journal", "journal_config_ids": [foreign]})


def test_runtime_executes_only_persisted_journal_target(tmp_path):
    repo, secrets, alice, _ = setup(tmp_path)
    first = config(repo, secrets, alice, "First")
    second = config(repo, secrets, alice, "Second")
    RecordingProvider.fetched = []
    runtime = AutomationRuntime(repo, secrets, "https://desk.example.test")
    runtime.tracking = TrackingService(repo, secrets, provider_factory=lambda provider, headless: RecordingProvider())

    summary = runtime._run_update(
        alice,
        {"id": 43, "payload": {"update_scope": "journal", "journal_config_ids": [first]}},
        101,
    )

    assert RecordingProvider.fetched == [first]
    assert second not in RecordingProvider.fetched
    assert summary["update_scope"] == "journal"
    assert summary["requested_journals"] == 1


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


def test_run_summary_is_compact_on_write_and_read(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    own = config(repo, SecretStore(tmp_path / "extra-secrets"), alice, "Alice Journal")
    foreign = config(repo, SecretStore(tmp_path / "foreign-secrets"), bob, "Bob Journal")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request = repo.enqueue_run(alice, "update", now.isoformat(), requested_by_user_id=alice)["request"]
    claimed = repo.claim_next_run(alice, "worker", now=now)
    run_id = repo.start_claimed_run(alice, claimed["id"], "worker")

    assert repo.finish_run(
        alice,
        run_id,
        "partial",
        {
            "result": "partial",
            "attempted_journals": 9,
            "successful_journals": 2,
            "failed_journals": 7,
            "results": {"Alice Journal": [{"title": "Secret title", "original_id": "SECRET-ID"}]},
            "journal_results": [
                {
                    "journal_config_id": own,
                    "success": False,
                    "error_code": "provider_login_failed",
                    "diagnostic_code": "provider_login_rejected",
                },
                {"journal_config_id": foreign, "success": False, "error_code": "raw traceback"},
                {
                    "journal_config_id": own,
                    "success": False,
                    "error_code": "tracking_failed",
                    "diagnostic_code": "missing_headers:Manuscript Number",
                },
                {"journal_config_id": own, "success": False, "error_code": "browser_timeout"},
                {"journal_config_id": own, "success": False, "error_code": "unsafe_provider_redirect"},
                {"journal_config_id": own, "success": False, "error_code": "credentials_unavailable"},
            ],
            "url": "https://private.example.test",
            "stack": "Traceback: password=secret",
        },
    )

    item = repo.get_run_request(alice, request["id"])
    summary = item["summary"]
    assert set(summary) == {
        "summary_version",
        "result",
        "kind",
        "attempted_journals",
        "successful_journals",
        "failed_journals",
        "failures",
        "historical",
    }
    assert len(summary["failures"]) == 5
    assert summary["failures"][0] == {
        "code": "provider_login_failed",
        "diagnostic_code": "provider_login_rejected",
        "journal_name": "Alice Journal",
    }
    assert "journal_name" not in summary["failures"][1]
    # A raw, non-allowlisted diagnostic string is dropped rather than exposed.
    assert "diagnostic_code" not in summary["failures"][2]
    rendered = json.dumps(summary)
    assert "Secret title" not in rendered and "SECRET-ID" not in rendered
    assert "private.example" not in rendered and "Traceback" not in rendered and "raw traceback" not in rendered
    assert "Manuscript Number" not in rendered
    with repo.connection() as conn:
        stored = conn.execute("SELECT summary_json FROM automation_job_runs WHERE id=?", (run_id,)).fetchone()[0]
    assert "Secret title" not in stored and "private.example" not in stored


def test_smtp_source_and_system_health_are_safe_and_owner_scoped(tmp_path):
    repo, _, alice, bob = setup(tmp_path)
    assert repo.get_user_smtp_source(alice) == "personal"
    assert repo.get_user_smtp_source(bob) == "personal"
    assert repo.get_system_smtp_health()["health"] == "unconfigured"

    config = repo.save_system_smtp(
        alice,
        {
            "host": "smtp.system.test",
            "port": 465,
            "security_mode": "ssl",
            "username": "system-user",
            "sender_address": "system@example.test",
            "sender_name": "System",
            "credential_ref": "users/00000000-0000-0000-0000-000000000000/smtp/ref",
        },
    )
    assert repo.get_system_smtp_health() == {
        "configured": True,
        "health": "untested",
        "selectable": False,
        "tested_at": None,
        "message": "系统发送通道尚未由管理员测试",
    }
    with pytest.raises(Exception, match="system_smtp_unavailable"):
        repo.set_user_smtp_source(alice, "system")

    assert repo.record_system_smtp_test(config["config_generation"], "success")
    repo.set_user_smtp_source(alice, "system")
    assert repo.get_user_smtp_source(alice) == "system"
    assert repo.get_user_smtp_source(bob) == "personal"
    resolved = repo.resolve_automation_smtp(alice)
    assert resolved["host"] == "smtp.system.test" and resolved["smtp_source"] == "system"

    repo.save_system_smtp(alice, {**config, "sender_name": "Changed"})
    assert repo.get_system_smtp_health()["health"] == "stale"
    with pytest.raises(Exception, match="system_smtp_unavailable"):
        repo.resolve_automation_smtp(alice)


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
