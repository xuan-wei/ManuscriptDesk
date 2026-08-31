from pathlib import Path

import pytest

from manuscript_desk.repository import Repository
from manuscript_desk.schema import create_database
from manuscript_desk.security import hash_password


def setup_repo(tmp_path: Path):
    database = tmp_path / "test.db"
    create_database(database)
    repository = Repository(database)
    owner = repository.create_user(
        "owner@example.test",
        "Owner",
        hash_password("correct horse battery staple1"),
        verified=True,
    )
    config_id = repository.create_journal_config(
        owner,
        {
            "journal_name": "Synthetic Journal",
            "journal_url": "https://mc.manuscriptcentral.com/synthetic",
            "username": "owner@example.test",
            "provider": "scholarone",
        },
    )
    return repository, owner, config_id


def manuscript(identifier: str):
    return {
        "original_id": identifier,
        "manuscript_id": identifier,
        "title": f"Synthetic {identifier}",
        "status": "Under Review",
        "detailed_status": "Under Review",
    }


def test_journal_batch_rolls_back_when_any_record_is_invalid(tmp_path):
    repository, owner, config_id = setup_repo(tmp_path)
    with pytest.raises(ValueError):
        repository.persist_journal_result(
            owner,
            config_id,
            {"outcome": "complete", "manuscripts": [manuscript("GOOD"), {"title": "invalid"}]},
        )
    with repository.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM manuscript_records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0] == 0


def test_anomalous_empty_preserves_records_and_logs_safe_code(tmp_path):
    repository, owner, config_id = setup_repo(tmp_path)
    repository.save_manuscript(owner, config_id, manuscript("EXISTING"))
    repository.persist_journal_result(
        owner,
        config_id,
        {"outcome": "anomalous_empty", "manuscripts": [], "error_code": "raw secret details"},
    )
    assert [item["original_id"] for item in repository.latest_manuscripts(owner)] == ["EXISTING"]
    with repository.connection() as connection:
        log = connection.execute("SELECT success,error_message FROM query_logs ORDER BY id DESC").fetchone()
    assert log["success"] == 0
    assert log["error_message"] == "empty_result_anomaly"


def test_verified_empty_is_a_successful_observation(tmp_path):
    repository, owner, config_id = setup_repo(tmp_path)
    assert repository.persist_journal_result(owner, config_id, {"outcome": "verified_empty", "manuscripts": []}) == 0
    with repository.connection() as connection:
        log = connection.execute("SELECT success,manuscripts_found FROM query_logs").fetchone()
    assert dict(log) == {"success": 1, "manuscripts_found": 0}


def test_journal_persistence_rejects_cross_tenant_config(tmp_path):
    repository, owner, config_id = setup_repo(tmp_path)
    other = repository.create_user(
        "other@example.test", "Other", hash_password("correct horse battery staple1"), verified=True
    )
    with pytest.raises(LookupError):
        repository.persist_journal_result(
            other, config_id, {"outcome": "complete", "manuscripts": [manuscript("FOREIGN")]}
        )
