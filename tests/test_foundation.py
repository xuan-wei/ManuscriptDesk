from pathlib import Path

import pytest

from manuscript_desk.repository import Repository
from manuscript_desk.schema import (
    SchemaError,
    assert_schema,
    connect,
    create_database,
    migrate_database,
)
from manuscript_desk.secrets_store import SecretStore
from manuscript_desk.security import (
    generate_otp,
    hash_password,
    normalize_username,
    otp_hmac,
    random_token,
    token_hash,
    verify_otp,
    verify_password,
)
from manuscript_desk.timezones import (
    grouped_timezones,
    profile_timezone_choices,
    timezone_names,
    validate_timezone,
)


def make_repo(tmp_path: Path) -> Repository:
    path = tmp_path / "test.db"
    create_database(path)
    return Repository(path)


def add_user(repo: Repository, email: str) -> int:
    return repo.create_user(email, email.split("@")[0], hash_password("correct horse battery staple1"), verified=True)


def add_config(repo: Repository, owner: int, name: str) -> int:
    return repo.create_journal_config(
        owner,
        {
            "journal_name": name,
            "journal_url": "https://example.test/journal",
            "username": "account@example.test",
            "provider": "scholarone",
        },
    )


def add_manuscript(repo: Repository, owner: int, config_id: int, title: str, status: str = "Under Review") -> None:
    repo.save_manuscript(
        owner,
        config_id,
        {
            "original_id": "COLLIDING-ID",
            "manuscript_id": "COLLIDING-ID",
            "title": title,
            "journal": "Shared Journal",
            "status": status,
            "detailed_status": status,
            "recorded_at": "2026-08-30 10:00:00",
        },
    )


def test_schema_creation_and_rejection(tmp_path):
    path = tmp_path / "tenant.db"
    create_database(path)
    with connect(path) as conn:
        assert_schema(conn)
        conn.execute("UPDATE app_schema SET version=999 WHERE singleton=1")
        conn.commit()
        with pytest.raises(SchemaError):
            assert_schema(conn)
    with pytest.raises(FileExistsError):
        create_database(path)


def test_schema_v4_migrates_request_payload_and_token_scope(tmp_path):
    path = tmp_path / "v4.db"
    create_database(path)
    with connect(path) as conn:
        conn.execute("ALTER TABLE automation_run_requests RENAME TO automation_run_requests_v5")
        conn.execute(
            """CREATE TABLE automation_run_requests AS
               SELECT id,owner_user_id,job_id,kind,source,scheduled_for,status,
                      requested_by_user_id,claimed_by,lease_expires_at,created_at,updated_at
               FROM automation_run_requests_v5"""
        )
        conn.execute("DROP TABLE automation_run_requests_v5")
        conn.execute("UPDATE app_schema SET version=4,application_compatibility='manuscript-desk-multitenant-v4'")
        user_id = conn.execute(
            """INSERT INTO users(email,username,display_name,password_hash,password_scheme,email_verified_at)
               VALUES('old@example.test','old','Old','hash','legacy_sha256',CURRENT_TIMESTAMP)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO api_tokens(owner_user_id,token_hash,token_prefix,label,scopes)
               VALUES(?,?,?,?,?)""",
            (user_id, "token-hash", "prefix", "Old", "starred:read starred:renew"),
        )
        conn.commit()

    migrate_database(path)

    with connect(path) as conn:
        assert_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(automation_run_requests)")}
        assert "payload_json" in columns
        assert conn.execute("SELECT scopes FROM api_tokens").fetchone()[0] == "starred:read starred:update"


def test_schema_v5_migrates_hidden_and_journey_overlays(tmp_path):
    path = tmp_path / "v5.db"
    create_database(path)
    with connect(path) as conn:
        conn.execute("DROP TABLE submission_journey_members")
        conn.execute("DROP TABLE submission_journeys")
        conn.execute("DROP TABLE manuscript_hides")
        conn.execute("UPDATE app_schema SET version=5,application_compatibility='manuscript-desk-multitenant-v5'")
        conn.commit()

    migrate_database(path)

    with connect(path) as conn:
        assert_schema(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"manuscript_hides", "submission_journeys", "submission_journey_members"} <= tables


def test_schema_v6_migrates_shortcut_settings(tmp_path):
    path = tmp_path / "v6.db"
    create_database(path)
    with connect(path) as conn:
        conn.execute("DROP TABLE user_shortcut_settings")
        conn.execute("UPDATE app_schema SET version=6,application_compatibility='manuscript-desk-multitenant-v6'")
        conn.commit()

    migrate_database(path)

    with connect(path) as conn:
        assert_schema(conn)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_shortcut_settings'"
        ).fetchone()
        assert table is not None
        assert conn.execute("SELECT COUNT(*) FROM user_shortcut_settings").fetchone()[0] == 0


def test_shortcut_settings_are_owner_scoped(tmp_path):
    repo = make_repo(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    assert repo.get_shortcut_settings(alice) is None
    repo.save_shortcut_base_url(alice, "https://desk.example.test/prefix")
    assert repo.get_shortcut_settings(alice)["base_url"] == "https://desk.example.test/prefix"
    assert repo.get_shortcut_settings(bob) is None
    repo.save_shortcut_base_url(alice, "https://new.example.test")
    assert repo.get_shortcut_settings(alice)["base_url"] == "https://new.example.test"
    with pytest.raises(ValueError):
        repo.save_shortcut_base_url(alice, "   ")


def test_profile_timezone_choices_are_deliberate_and_valid():
    choices = profile_timezone_choices()
    assert len(choices) == 24
    assert len({value for value, _ in choices}) == 24
    assert {"UTC", "Asia/Shanghai", "America/New_York", "Pacific/Auckland"} <= {value for value, _ in choices}
    assert all(validate_timezone(value) == value for value, _ in choices)
    assert validate_timezone("Pacific/Chatham") == "Pacific/Chatham"


def test_hidden_manuscripts_and_submission_journeys_persist(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config_id = add_config(repo, owner, "Journal")
    for original_id, status in (("FIRST", "Reject"), ("SECOND", "Under Review"), ("THIRD", "Revision")):
        repo.save_manuscript(
            owner,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": f"{original_id} title",
                "status": status,
                "detailed_status": status,
            },
        )

    repo.set_hidden(owner, "FIRST", True)
    repo.save_manuscript(
        owner,
        config_id,
        {
            "original_id": "FIRST",
            "manuscript_id": "FIRST.R1",
            "version_number": 2,
            "title": "FIRST updated",
            "status": "Revision",
            "detailed_status": "Major Revision",
        },
    )
    assert [item["original_id"] for item in repo.latest_manuscripts(owner)] == ["THIRD", "SECOND"]
    assert repo.latest_manuscripts(owner, visibility="hidden")[0]["title"] == "FIRST updated"

    journey = repo.create_submission_journey(owner, ["FIRST", "SECOND", "THIRD"])
    assert journey["representative"]["original_id"] == "THIRD"
    assert [member["original_id"] for member in journey["members"]] == ["FIRST", "SECOND", "THIRD"]
    cards = repo.manuscript_cards(owner)
    assert len(cards) == 1 and cards[0]["kind"] == "journey"
    assert cards[0]["representative"]["original_id"] == "THIRD"
    assert repo.statistics(owner)["ongoing"] == 1

    reordered = repo.replace_submission_journey_members(
        owner, journey["id"], ["THIRD", "SECOND", "FIRST"], journey["revision"]
    )
    assert reordered["representative"]["original_id"] == "FIRST"
    repo.set_submission_journey_hidden(owner, journey["id"], True)
    assert repo.manuscript_cards(owner) == []
    assert repo.manuscript_cards(owner, view="hidden")[0]["journey_id"] == journey["id"]


def test_journeys_are_tenant_scoped_and_single_membership(tmp_path):
    repo = make_repo(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    alice_config = add_config(repo, alice, "Alice")
    bob_config = add_config(repo, bob, "Bob")
    for owner, config_id in ((alice, alice_config), (bob, bob_config)):
        for original_id in ("ONE", "TWO"):
            repo.save_manuscript(
                owner,
                config_id,
                {
                    "original_id": original_id,
                    "manuscript_id": original_id,
                    "title": original_id,
                    "status": "Under Review",
                    "detailed_status": "Under Review",
                },
            )
    journey = repo.create_submission_journey(alice, ["ONE", "TWO"])
    assert repo.submission_journey(bob, journey["id"]) is None
    with pytest.raises(Exception):
        repo.create_submission_journey(alice, ["ONE", "TWO"])
    bob_journey = repo.create_submission_journey(bob, ["ONE", "TWO"])
    assert bob_journey["id"] != journey["id"]


def test_journey_lifecycle_conflicts_hidden_representative_and_auto_dissolve(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config_id = add_config(repo, owner, "Journal")
    ids = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH"]
    statuses = ["Reject", "Reject", "Revision", "Under Review", "Accept"]
    for original_id, status in zip(ids, statuses, strict=False):
        repo.save_manuscript(
            owner,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": f"{original_id} title",
                "status": status,
                "detailed_status": status,
            },
        )

    journey = repo.create_submission_journey(owner, ids)
    assert journey["member_count"] == 5
    assert journey["representative"]["original_id"] == "FIFTH"
    assert repo.ungrouped_manuscripts(owner) == []
    repo.set_hidden(owner, "FIFTH", True)
    visible = repo.manuscript_cards(owner)
    assert len(visible) == 1
    assert visible[0]["representative"]["original_id"] == "FIFTH"
    assert visible[0]["representative"]["is_hidden"] is True

    reordered = repo.replace_submission_journey_members(owner, journey["id"], list(reversed(ids)), journey["revision"])
    assert reordered["representative"]["original_id"] == "FIRST"
    with pytest.raises(RuntimeError):
        repo.replace_submission_journey_members(owner, journey["id"], ids, journey["revision"])

    current = reordered
    for original_id in ("FIFTH", "FOURTH", "THIRD"):
        current = repo.unlink_submission_journey_member(owner, journey["id"], original_id)
        assert current is not None
    assert [member["position"] for member in current["members"]] == [0, 1]
    assert repo.unlink_submission_journey_member(owner, journey["id"], "SECOND") is None
    assert repo.submission_journey(owner, journey["id"]) is None
    assert {card["representative"]["original_id"] for card in repo.manuscript_cards(owner)} == {
        "FIRST",
        "SECOND",
        "THIRD",
        "FOURTH",
    }
    assert [item["original_id"] for item in repo.latest_manuscripts(owner, visibility="hidden")] == ["FIFTH"]


def test_whole_journey_hide_applies_to_flat_manuscript_projection(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config_id = add_config(repo, owner, "Journal")
    for original_id in ("ONE", "TWO"):
        repo.save_manuscript(
            owner,
            config_id,
            {
                "original_id": original_id,
                "manuscript_id": original_id,
                "title": original_id,
                "status": "Under Review",
                "detailed_status": "Under Review",
            },
        )
    journey = repo.create_submission_journey(owner, ["ONE", "TWO"])
    repo.set_submission_journey_hidden(owner, journey["id"], True)

    assert repo.latest_manuscripts(owner) == []
    assert {item["original_id"] for item in repo.latest_manuscripts(owner, visibility="hidden")} == {"ONE", "TWO"}


def test_tenants_with_colliding_names_and_ids_are_isolated(tmp_path):
    repo = make_repo(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    alice_config = add_config(repo, alice, "Shared Journal")
    bob_config = add_config(repo, bob, "Shared Journal")
    add_manuscript(repo, alice, alice_config, "Alice paper", "Revision")
    add_manuscript(repo, bob, bob_config, "Bob paper", "Accept")

    assert [item["title"] for item in repo.latest_manuscripts(alice)] == ["Alice paper"]
    assert [item["title"] for item in repo.latest_manuscripts(bob)] == ["Bob paper"]
    assert [item["title"] for item in repo.manuscript_history(alice, "COLLIDING-ID")] == ["Alice paper"]
    assert repo.statistics(alice)["revision"] == 1
    assert repo.statistics(alice)["accepted"] == 0
    assert repo.statistics(bob)["accepted"] == 1


def test_latest_manuscript_uses_highest_version_not_last_insert(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config = add_config(repo, owner, "Journal")

    for manuscript_id, version, status, recorded_at in (
        ("PAPER.R2", 3, "Accept", "2026-08-30 07:00:00"),
        ("PAPER.R1", 2, "Revision", "2026-08-30 07:00:01"),
        ("PAPER", 1, "Revision", "2026-08-30 07:00:02"),
    ):
        repo.save_manuscript(
            owner,
            config,
            {
                "original_id": "PAPER",
                "manuscript_id": manuscript_id,
                "title": "Accepted paper",
                "status": status,
                "detailed_status": status,
                "version_number": version,
                "recorded_at": recorded_at,
            },
        )

    latest = repo.latest_manuscripts(owner)
    assert len(latest) == 1
    assert latest[0]["manuscript_id"] == "PAPER.R2"
    assert latest[0]["version_number"] == 3
    assert latest[0]["status"] == "Accept"
    assert repo.statistics(owner)["accepted"] == 1


def test_save_manuscript_persists_provider_metadata_and_recovers_current_version_date(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config = add_config(repo, owner, "Journal")
    repo.save_manuscript(
        owner,
        config,
        {
            "original_id": "PAPER",
            "manuscript_id": "PAPER.R1",
            "version_number": 2,
            "title": "Paper",
            "status": "Under Review",
            "detailed_status": "Under Review",
            "submitted_date": "21-Aug-2026",
            "decision_date": "",
            "status_date": "22-Aug-2026",
            "SE_or_DE": "Editor Name",
            "author_role": "author",
            "authorship": "Corresponding Author",
            "workflow_folder": "Submissions Being Processed",
            "final_disposition": "",
            "recorded_at": "2026-08-21 10:00:00",
        },
    )
    repo.save_manuscript(
        owner,
        config,
        {
            "original_id": "PAPER",
            "manuscript_id": "PAPER.R1",
            "version_number": 2,
            "title": "Paper",
            "status": "Under Review",
            "detailed_status": "Under Review",
            "recorded_at": "2026-08-22 10:00:00",
        },
    )

    current = repo.latest_manuscripts(owner)[0]
    assert current["submitted_date"] == "21-Aug-2026"
    with repo.connection() as conn:
        persisted = conn.execute(
            "SELECT * FROM manuscript_records WHERE owner_user_id=? AND original_id=? ORDER BY id LIMIT 1",
            (owner, "PAPER"),
        ).fetchone()
    assert persisted["status_date"] == "22-Aug-2026"
    assert persisted["editor"] == "Editor Name"
    assert persisted["author_role"] == "author"
    assert persisted["authorship"] == "Corresponding Author"
    assert persisted["workflow_folder"] == "Submissions Being Processed"


def test_current_status_age_uses_current_version_occurrence_and_semantic_date(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config = add_config(repo, owner, "Journal")

    snapshots = (
        ("PAPER", 1, "Revision", "Major Revision", "2025-08-02 07:00:00"),
        ("PAPER", 1, "Revision", "Major Revision", "2025-08-03 07:00:00"),
        ("PAPER.R1", 2, "Revision", "Major Revision", "2026-04-01 07:00:00"),
        ("PAPER.R1", 2, "Under Review", "Under Review", "2026-04-02 07:00:00"),
        ("PAPER.R1", 2, "Revision", "Major Revision", "2026-04-28 07:00:00"),
        ("PAPER.R1", 2, "Revision", "Major Revision", "2026-04-29 07:00:00"),
    )
    ids = []
    for manuscript_id, version, status, detail, recorded_at in snapshots:
        ids.append(
            repo.save_manuscript(
                owner,
                config,
                {
                    "original_id": "PAPER",
                    "manuscript_id": manuscript_id,
                    "title": "Status age paper",
                    "status": status,
                    "detailed_status": detail,
                    "version_number": version,
                    "recorded_at": recorded_at,
                },
            )
        )
    with repo.connection() as conn:
        conn.execute(
            "UPDATE manuscript_records SET status_date='26-Apr-2026',decision_date='27-Apr-2026' WHERE id=?",
            (ids[-2],),
        )

    current = repo.latest_manuscripts(owner)[0]
    assert current["version_number"] == 2
    assert current["status"] == "Revision"
    assert current["status_pair_first_seen_at"] == "27-Apr-2026"


def test_manuscript_history_returns_only_status_transitions(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config = add_config(repo, owner, "Journal")

    snapshots = (
        ("Under Review", "2026-08-01 07:00:00"),
        ("Under Review", "2026-08-02 07:00:00"),
        ("Revision", "2026-08-03 07:00:00"),
        ("Revision", "2026-08-04 07:00:00"),
        ("Under Review", "2026-08-05 07:00:00"),
    )
    for status, recorded_at in snapshots:
        repo.save_manuscript(
            owner,
            config,
            {
                "original_id": "TRANSITION-ID",
                "manuscript_id": "TRANSITION-ID.R1",
                "title": "Transition paper",
                "status": status,
                "detailed_status": status,
                "version_number": 2,
                "recorded_at": recorded_at,
            },
        )

    history = repo.manuscript_history(owner, "TRANSITION-ID")
    assert [row["detailed_status"] for row in history] == [
        "Under Review",
        "Revision",
        "Under Review",
    ]
    assert [row["recorded_at"] for row in history] == [
        "2026-08-01 07:00:00",
        "2026-08-03 07:00:00",
        "2026-08-05 07:00:00",
    ]


def test_stars_and_acknowledgements_are_independent(tmp_path):
    repo = make_repo(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    alice_config = add_config(repo, alice, "Journal")
    bob_config = add_config(repo, bob, "Journal")
    add_manuscript(repo, alice, alice_config, "Alice paper")
    add_manuscript(repo, bob, bob_config, "Bob paper")

    assert repo.toggle_star(alice, "COLLIDING-ID") is True
    assert repo.starred_ids(alice) == {"COLLIDING-ID"}
    assert repo.starred_ids(bob) == set()
    assert repo.acknowledge(alice, "COLLIDING-ID") is True
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manuscript_acks WHERE owner_user_id=?", (alice,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM manuscript_acks WHERE owner_user_id=?", (bob,)).fetchone()[0] == 0


def test_cross_tenant_config_update_fails(tmp_path):
    repo = make_repo(tmp_path)
    alice = add_user(repo, "alice@example.test")
    bob = add_user(repo, "bob@example.test")
    config = add_config(repo, alice, "Alice Journal")
    changed = repo.update_journal_config(
        bob,
        config,
        {
            "journal_name": "Stolen",
            "journal_url": "https://evil.test",
            "username": "x",
        },
    )
    assert changed is False
    assert repo.list_journal_configs(alice)[0]["journal_name"] == "Alice Journal"


def test_browser_journal_projection_excludes_internal_secret_fields(tmp_path):
    repo = make_repo(tmp_path)
    owner = add_user(repo, "alice@example.test")
    config_id = repo.create_journal_config(
        owner,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test/journal",
            "short_name": "JRN",
            "description": "Original",
            "username": "alice",
            "provider": "scholarone",
            "credential_ref": f"users/{owner}/credentials/opaque.secret",
        },
    )

    browser = repo.list_journal_configs_for_browser(owner)[0]
    assert browser["credential_configured"] is True
    assert browser["enabled"] is True
    assert "owner_user_id" not in browser
    assert "credential_ref" not in browser

    assert repo.update_journal_config(
        owner,
        config_id,
        {
            "journal_name": "Journal",
            "journal_url": "https://example.test/journal",
            "short_name": "NEW",
            "description": "",
            "username": "alice",
            "provider": "scholarone",
            "enabled": True,
        },
    )
    updated = repo.list_journal_configs(owner)[0]
    assert updated["short_name"] == "NEW"
    assert updated["description"] == ""
    assert updated["credential_ref"].endswith("opaque.secret")


def test_all_iana_timezones_are_grouped_and_validated():
    names = timezone_names()
    assert len(names) == len(set(names))
    assert list(names) == sorted(names)
    for name in ("UTC", "Asia/Kathmandu", "Pacific/Chatham", "America/Argentina/Buenos_Aires"):
        assert name in names
        assert validate_timezone(name) == name

    grouped = grouped_timezones()
    flattened = [name for _, group in grouped for name in group]
    assert flattened == list(dict.fromkeys(flattened))
    assert set(flattened) == set(names)
    assert {label for label, _ in grouped} >= {"UTC", "Asia", "America", "Pacific"}

    for invalid in ("", "Not/A_Zone", "UTC+8", "Asia/Shanghai\x00", "x" * 256):
        with pytest.raises(ValueError):
            validate_timezone(invalid)


def test_session_stores_only_hash(tmp_path):
    repo = make_repo(tmp_path)
    user = add_user(repo, "alice@example.test")
    token = random_token()
    csrf = random_token()
    repo.create_session(user, token, csrf)
    with repo.connection() as conn:
        row = conn.execute("SELECT token_hash,csrf_secret FROM user_sessions").fetchone()
    assert row["token_hash"] == token_hash(token)
    assert row["csrf_secret"] == csrf
    assert token not in tuple(row)
    assert repo.get_active_session(token)["user_id"] == user


def test_new_password_policy_requires_length_letter_and_number():
    valid_hash = hash_password("password1")
    assert verify_password("password1", valid_hash, "argon2id").valid
    assert verify_password("Password123", hash_password("Password123"), "argon2id").valid

    for invalid in (
        "abc123",  # fewer than 8 characters
        "abcdefgh",  # no number
        "12345678",  # no letter
        "!!!!!!!!1",  # number but no letter
        "password",  # letters but no number
    ):
        with pytest.raises(ValueError):
            hash_password(invalid)


def test_legacy_password_verification_upgrades():
    import hashlib

    password = "short-old"
    legacy = hashlib.sha256(password.encode()).hexdigest()
    result = verify_password(password, legacy, "legacy_sha256")
    assert result.valid
    assert result.upgraded_hash
    assert verify_password(password, result.upgraded_hash, "argon2id").valid
    assert not verify_password("wrong password", legacy, "legacy_sha256").valid


def test_secret_store_uses_opaque_references_and_strict_permissions(tmp_path):
    root = tmp_path / "secrets"
    store = SecretStore(root)
    reference = store.write("123e4567-e89b-12d3-a456-426614174000", "credentials", "not-a-real-secret")
    assert "not-a-real-secret" not in reference
    target = root / reference
    assert target.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "users").stat().st_mode & 0o777 == 0o700
    assert target.parent.parent.stat().st_mode & 0o777 == 0o700
    assert store.read(reference) == "not-a-real-secret"
    store.delete(reference)
    assert not target.exists()
    with pytest.raises(ValueError):
        store.read("../outside")


def test_username_normalization_and_login(tmp_path):
    repo = make_repo(tmp_path)
    user = repo.create_user(
        "alice@example.test",
        "Alice",
        hash_password("correct horse battery staple1"),
        username="Alice_User",
        verified=True,
    )
    assert normalize_username(" Alice_User ") == "alice_user"
    assert repo.get_user_for_login("ALICE_USER")["id"] == user
    assert repo.get_user_for_login("alice@example.test")["id"] == user
    with pytest.raises(ValueError):
        normalize_username("api")
    with pytest.raises(Exception):
        repo.create_user(
            "other@example.test",
            "Other",
            hash_password("correct horse battery staple1"),
            username="ALICE_USER",
            verified=True,
        )


def test_otp_is_hmac_only_and_verifiable():
    key = b"test-key-not-a-production-secret"
    code = generate_otp()
    digest = otp_hmac(key, "Alice@Example.Test", "register", code)
    assert code not in digest
    assert verify_otp(key, "alice@example.test", "register", code, digest)
    assert not verify_otp(key, "alice@example.test", "register", "000000" if code != "000000" else "999999", digest)
