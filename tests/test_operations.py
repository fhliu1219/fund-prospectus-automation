"""Persistent operations tests: idempotency, leases, artifacts, and review."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from prospectus_fetcher.models import (
    DocumentKind,
    DocumentVerification,
    FetchResult,
)
from prospectus_fetcher.operations import (
    IdempotencyConflict,
    ItemStatus,
    JobStatus,
    LeaseOwnershipError,
    OperationsContractError,
    PersistentJobRunner,
)
from prospectus_fetcher.sqlite_operations import (
    _MIGRATION_1,
    SCHEMA_VERSION,
    SQLiteOperationsStore,
)
from tests.operations_contract import (
    assert_active_claim_isolation_contract,
    assert_direct_claim_contract,
    assert_scoped_idempotency_contract,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def manifest_for(tmp_path, ticker, verification="document_verified"):
    document_path = tmp_path / f"{ticker}.html"
    document_path.write_text(f"<html>{ticker}</html>")
    document_sha = hashlib.sha256(document_path.read_bytes()).hexdigest()
    value = {
        "schema_version": 2,
        "ticker": ticker,
        "selection": {
            "form": "497K",
            "filing_date": "2026-01-01",
            "reason": "test",
        },
        "identity": {
            "level": "class",
            "registrant_cik": 1,
            "series_id": "S000000001",
            "class_id": "C000000001",
            "mapping_source": "mf",
            "evidence": ["class feed"],
        },
        "package": {
            "kind": "summary_prospectus",
            "verification": verification,
            "validation_policy": "v7",
            "validation_policy_version": "m6.3-shadow-v7",
            "evidence": ["ticker_in_table"],
            "warnings": [],
        },
        "documents": [
            {
                "role": "primary_prospectus",
                "kind": "summary_prospectus",
                "accession": "0000000001-26-000001",
                "form": "497K",
                "date": "2026-01-01",
                "path": str(document_path),
                "verification": verification,
                "source_url": "https://www.sec.gov/example.htm",
                "archive_index_url": "https://www.sec.gov/index.json",
                "size_bytes": document_path.stat().st_size,
                "sha256": document_sha,
                "evidence": [],
                "warnings": [],
                "referenced_dates": [],
                "base_prospectus_dates": [],
                "contradictions": [],
            }
        ],
        "recovery": {"searches": [], "candidates": []},
    }
    manifest_path = tmp_path / f"{ticker}-manifest.json"
    manifest_path.write_text(json.dumps(value))
    return value, manifest_path


def successful_result(
    ticker,
    manifest_path,
    verification=DocumentVerification.VERIFIED,
    warnings=None,
):
    return FetchResult(
        ticker=ticker,
        status="ok",
        form="497K",
        date="2026-01-01",
        path=str(manifest_path.with_name(f"{ticker}.html")),
        manifest_path=str(manifest_path),
        document_kind=DocumentKind.SUMMARY_PROSPECTUS,
        document_verification=verification,
        warnings=list(warnings or []),
    )


def test_schema_migration_is_idempotent_across_reopen(tmp_path):
    path = tmp_path / "operations.db"
    with SQLiteOperationsStore(str(path)) as first:
        first.create_job("nightly-1", ["VUSXX"], "v7")

    with SQLiteOperationsStore(str(path)) as second:
        versions = second.connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        jobs = second.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    assert [row["version"] for row in versions] == list(
        range(1, SCHEMA_VERSION + 1)
    )
    assert jobs == 1


def test_schema_migration_preserves_v1_jobs_items_and_artifacts(tmp_path):
    path = tmp_path / "operations.db"
    timestamp = "2026-07-20T12:00:00.000000+00:00"
    connection = sqlite3.connect(path)
    connection.executescript(_MIGRATION_1)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (1, timestamp),
    )
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, idempotency_key, request_fingerprint, validation_policy,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("00000000-0000-4000-8000-000000000001", "legacy", "fingerprint",
         "v7", "completed", timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO job_items(
            item_id, job_id, ticker, ordinal, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000001",
            "VUSXX",
            0,
            "verified",
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO artifacts(
            artifact_id, item_id, role, accession, source_url, local_path,
            size_bytes, sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000002",
            "primary_prospectus",
            "0000000001-26-000001",
            "https://www.sec.gov/example.htm",
            "/legacy/VUSXX.html",
            10,
            "0" * 64,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()

    with SQLiteOperationsStore(str(path)) as store:
        job = store.get_job("00000000-0000-4000-8000-000000000001")
        artifacts = store.artifact_records(
            "00000000-0000-4000-8000-000000000002"
        )
        versions = store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert job.idempotency_scope == "internal"
    assert [row["version"] for row in versions] == [1, 2]
    assert artifacts[0]["storage_provider"] == "legacy-local"
    assert artifacts[0]["storage_namespace"] == "legacy"
    assert artifacts[0]["object_key"] == "/legacy/VUSXX.html"


def test_store_refuses_a_database_from_a_newer_schema_version(tmp_path):
    path = tmp_path / "operations.db"
    with SQLiteOperationsStore(str(path)) as store:
        with store.connection:
            store.connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (SCHEMA_VERSION + 1, "2026-07-20T12:00:00.000000+00:00"),
            )

    with pytest.raises(OperationsContractError, match="newer than supported"):
        SQLiteOperationsStore(str(path))


def test_idempotency_key_reuses_only_identical_normalized_request(tmp_path):
    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        assert_scoped_idempotency_contract(store)


def test_expired_lease_is_reclaimed_and_old_owner_cannot_finalize(tmp_path):
    clock = MutableClock()
    with SQLiteOperationsStore(
        str(tmp_path / "operations.db"),
        clock=clock,
    ) as store:
        job = store.create_job("lease-case", ["VUSXX"], "v7")
        first = store.claim_next_item(job.job_id, "worker-1", lease_seconds=30)

        assert first is not None
        assert first.attempt_count == 1
        assert store.claim_next_item(job.job_id, "worker-2", 30) is None

        clock.advance(31)
        reclaimed = store.claim_next_item(job.job_id, "worker-2", 30)
        assert reclaimed is not None
        assert reclaimed.item_id == first.item_id
        assert reclaimed.attempt_count == 2
        with pytest.raises(LeaseOwnershipError):
            store.fail_item(first.item_id, "worker-1", "late failure")


def test_unknown_job_cannot_be_claimed(tmp_path):
    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        with pytest.raises(KeyError, match="unknown job"):
            store.claim_next_item("missing-job", "worker-1", 30)


def test_separate_sqlite_connections_do_not_claim_the_same_active_item(
    tmp_path,
):
    path = str(tmp_path / "operations.db")
    with SQLiteOperationsStore(path) as first, SQLiteOperationsStore(path) as second:
        assert_active_claim_isolation_contract(first, second)


def test_direct_item_claim_targets_one_ticker_and_is_owner_safe(tmp_path):
    path = str(tmp_path / "operations.db")
    with SQLiteOperationsStore(path) as first, SQLiteOperationsStore(path) as second:
        assert_direct_claim_contract(first, second)


def test_persistent_runner_records_mixed_results_and_resumes_idempotently(tmp_path):
    vusxx_manifest, vusxx_path = manifest_for(tmp_path, "VUSXX")
    qqq_manifest, qqq_path = manifest_for(
        tmp_path,
        "QQQ",
        verification="manual_review_required",
    )
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="v7")
    fetcher.fetch.side_effect = [
        successful_result("VUSXX", vusxx_path),
        successful_result(
            "QQQ",
            qqq_path,
            verification=DocumentVerification.MANUAL_REVIEW_REQUIRED,
            warnings=["supplement relationship needs review"],
        ),
        FetchResult("ZZZZ", "error", error="unresolved ticker"),
    ]

    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(
            store,
            fetcher,
            worker_id="worker-1",
        )
        job = runner.submit(
            "mixed-2026-07-20",
            ["VUSXX", "QQQ", "ZZZZ"],
            "v7",
        )
        completed = runner.run(job.job_id)
        items = store.list_items(job.job_id)

        assert completed.status is JobStatus.COMPLETED_WITH_ERRORS
        assert [item.status for item in items] == [
            ItemStatus.VERIFIED,
            ItemStatus.REVIEW_REQUIRED,
            ItemStatus.FAILED,
        ]
        assert [item.attempt_count for item in items] == [1, 1, 1]
        assert store.persisted_manifest(items[0].item_id) == vusxx_manifest
        assert store.persisted_manifest(items[1].item_id) == qqq_manifest
        assert store.persisted_manifest(items[2].item_id) is None
        assert len(store.artifact_records(items[0].item_id)) == 1
        assert len(store.artifact_records(items[1].item_id)) == 1
        artifact = store.artifact_records(items[0].item_id)[0]
        assert artifact["storage_provider"] == "local-cas"
        assert artifact["object_key"].startswith("sha256/")
        assert "local_path" not in artifact
        stored_identity = store.connection.execute(
            """
            SELECT registrant_cik, series_id, class_id, identity_level,
                   document_verification, policy_version
            FROM job_items
            WHERE item_id = ?
            """,
            (items[0].item_id,),
        ).fetchone()
        assert dict(stored_identity) == {
            "registrant_cik": 1,
            "series_id": "S000000001",
            "class_id": "C000000001",
            "identity_level": "class",
            "document_verification": "document_verified",
            "policy_version": "m6.3-shadow-v7",
        }
        reviews = store.list_review_tasks()
        assert [(review.ticker, review.status.value) for review in reviews] == [
            ("QQQ", "pending")
        ]
        assert "supplement relationship needs review" in reviews[0].reason

        rerun = runner.run(job.job_id)
        assert rerun.status is JobStatus.COMPLETED_WITH_ERRORS
        assert fetcher.fetch.call_count == 3


@pytest.mark.parametrize(
    ("verification", "expected_status", "expected_item_status"),
    [
        (
            DocumentVerification.VERIFIED,
            JobStatus.COMPLETED,
            ItemStatus.VERIFIED,
        ),
        (
            DocumentVerification.MANUAL_REVIEW_REQUIRED,
            JobStatus.REVIEW_REQUIRED,
            ItemStatus.REVIEW_REQUIRED,
        ),
    ],
)
def test_runner_derives_terminal_job_status_from_successful_items(
    tmp_path,
    verification,
    expected_status,
    expected_item_status,
):
    manifest, manifest_path = manifest_for(
        tmp_path,
        "VUSXX",
        verification=verification.value,
    )
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="v7")
    fetcher.fetch.return_value = successful_result(
        "VUSXX",
        manifest_path,
        verification=verification,
    )

    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(store, fetcher, worker_id="worker-1")
        job = runner.submit(
            f"terminal-{verification.value}",
            ["VUSXX"],
            "v7",
        )

        completed = runner.run(job.job_id)
        item = store.list_items(job.job_id)[0]

        assert completed.status is expected_status
        assert item.status is expected_item_status
        assert store.persisted_manifest(item.item_id) == manifest
        assert len(store.list_review_tasks()) == (
            1
            if verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
            else 0
        )


def test_runner_marks_missing_success_manifest_as_failed_and_continues(tmp_path):
    valid_manifest, valid_path = manifest_for(tmp_path, "SPY")
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="v7")
    fetcher.fetch.side_effect = [
        FetchResult(
            "VUSXX",
            "ok",
            document_verification=DocumentVerification.VERIFIED,
        ),
        successful_result("SPY", valid_path),
    ]

    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(store, fetcher, worker_id="worker-1")
        job = runner.submit("manifest-case", ["VUSXX", "SPY"], "v7")

        result = runner.run(job.job_id)
        items = store.list_items(job.job_id)

        assert result.status is JobStatus.COMPLETED_WITH_ERRORS
        assert items[0].status is ItemStatus.FAILED
        assert "no manifest path" in (items[0].error or "")
        assert items[1].status is ItemStatus.VERIFIED


def test_runner_rejects_fetcher_policy_mismatch(tmp_path):
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="legacy")
    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(store, fetcher)

        with pytest.raises(OperationsContractError, match="does not match"):
            runner.submit("policy-mismatch", ["VUSXX"], "v7")


def test_runner_rejects_a_manifest_from_a_different_policy(tmp_path):
    _, manifest_path = manifest_for(tmp_path, "VUSXX")
    manifest = json.loads(manifest_path.read_text())
    manifest["package"]["validation_policy"] = "legacy"
    manifest_path.write_text(json.dumps(manifest))
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="v7")
    fetcher.fetch.return_value = successful_result("VUSXX", manifest_path)

    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(store, fetcher, worker_id="worker-1")
        job = runner.submit("manifest-policy-case", ["VUSXX"], "v7")

        completed = runner.run(job.job_id)
        item = store.list_items(job.job_id)[0]

        assert completed.status is JobStatus.COMPLETED_WITH_ERRORS
        assert item.status is ItemStatus.FAILED
        assert "validation policy does not match" in (item.error or "")
        assert store.persisted_manifest(item.item_id) is None


def test_artifact_checksum_drift_fails_the_item_without_persisting_artifact(
    tmp_path,
):
    _, manifest_path = manifest_for(tmp_path, "VUSXX")
    manifest = json.loads(manifest_path.read_text())
    manifest["documents"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    fetcher = Mock()
    fetcher.package_builder = SimpleNamespace(validation_policy="v7")
    fetcher.fetch.return_value = successful_result("VUSXX", manifest_path)

    with SQLiteOperationsStore(str(tmp_path / "operations.db")) as store:
        runner = PersistentJobRunner(store, fetcher, worker_id="worker-1")
        job = runner.submit("checksum-case", ["VUSXX"], "v7")

        completed = runner.run(job.job_id)
        item = store.list_items(job.job_id)[0]

        assert completed.status is JobStatus.COMPLETED_WITH_ERRORS
        assert item.status is ItemStatus.FAILED
        assert "integrity mismatch" in (item.error or "")
        assert store.artifact_records(item.item_id) == []
