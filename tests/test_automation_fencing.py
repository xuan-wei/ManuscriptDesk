from datetime import datetime, timedelta, timezone
from pathlib import Path

from manuscript_desk.repository import Repository
from manuscript_desk.schema import connect, create_database, migrate_database
from manuscript_desk.security import hash_password


def setup_repo(tmp_path: Path):
    database = tmp_path / "fence.db"
    create_database(database)
    repository = Repository(database)
    owner = repository.create_user(
        "owner@example.test", "Owner", hash_password("correct horse battery staple1"), verified=True
    )
    return database, repository, owner


def enqueue(repository, owner, scheduled_for):
    return repository.enqueue_run(
        owner,
        "update",
        scheduled_for.isoformat(),
        requested_by_user_id=owner,
        payload={"update_scope": "all"},
    )["request"]["id"]


def test_expired_claim_is_reissued_with_new_fence(tmp_path):
    _, repository, owner = setup_repo(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request_id = enqueue(repository, owner, now)
    first = repository.claim_next_run(owner, "worker-a", lease_seconds=30, now=now)
    assert first["id"] == request_id
    second = repository.claim_next_run(owner, "worker-b", lease_seconds=30, now=now + timedelta(seconds=31))
    assert second["claim_generation"] == first["claim_generation"] + 1
    assert second["claim_token"] != first["claim_token"]
    assert not repository.heartbeat_run_claim(
        owner,
        request_id,
        "worker-a",
        first["claim_token"],
        now=now + timedelta(seconds=31),
    )
    assert repository.heartbeat_run_claim(
        owner,
        request_id,
        "worker-b",
        second["claim_token"],
        now=now + timedelta(seconds=31),
    )


def test_late_worker_cannot_start_or_finish_reclaimed_request(tmp_path):
    _, repository, owner = setup_repo(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    enqueue(repository, owner, now)
    first = repository.claim_next_run(owner, "worker-a", lease_seconds=30, now=now)
    second = repository.claim_next_run(owner, "worker-b", lease_seconds=30, now=now + timedelta(seconds=31))
    run_id = repository.start_claimed_run(owner, second["id"], "worker-b", second["claim_token"])
    assert not repository.finish_run(owner, run_id, "success", {}, claim_token=first["claim_token"])
    assert repository.finish_run(owner, run_id, "success", {}, claim_token=second["claim_token"])


def test_v7_migration_adds_fence_columns(tmp_path):
    database, _, _ = setup_repo(tmp_path)
    with connect(database) as connection:
        connection.execute("UPDATE app_schema SET version=7,application_compatibility='manuscript-desk-multitenant-v7'")
        connection.commit()
    migrate_database(database)
    with connect(database) as connection:
        schema = connection.execute(
            "SELECT version,application_compatibility,migration_id FROM app_schema WHERE singleton=1"
        ).fetchone()
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(automation_run_requests)")}
    assert dict(schema) == {
        "version": 9,
        "application_compatibility": "manuscript-desk-multitenant-v9",
        "migration_id": "v8-to-v9-smtp-sources",
    }
    assert {"claim_generation", "claim_token", "heartbeat_at"} <= columns
