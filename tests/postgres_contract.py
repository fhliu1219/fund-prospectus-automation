"""Opt-in PostgreSQL migration, conformance, and contention tests.

Run explicitly after starting docker-compose.postgres.yml:
RUN_POSTGRES_TESTS=1 pytest tests/postgres_contract.py -q
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from prospectus_fetcher.artifact_store import (
    LocalContentAddressedArtifactStore,
)
from prospectus_fetcher.models import (
    DocumentVerification,
    FetchResult,
)
from prospectus_fetcher.operations import (
    ItemStatus,
    JobStatus,
    LeaseOwnershipError,
    OperationsContractError,
    PersistentJobRunner,
)
from prospectus_fetcher.operations_schema import job_items
from prospectus_fetcher.postgres_operations import (
    EXPECTED_ALEMBIC_REVISION,
    PostgreSQLOperationsStore,
)
from tests.operations_contract import (
    assert_active_claim_isolation_contract,
    assert_scoped_idempotency_contract,
)
from tests.test_operations import manifest_for, successful_result


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_TESTS") != "1",
    reason="set RUN_POSTGRES_TESTS=1 for PostgreSQL contract tests",
)

DATABASE_URL = os.environ.get(
    "PROSPECTUS_DATABASE_URL",
    "postgresql+psycopg://prospectus:prospectus@localhost:55432/prospectus_test",
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(connection=None):
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


@pytest.fixture
def postgres_engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        command.upgrade(_alembic_config(connection), "head")
    try:
        yield engine
    finally:
        engine.dispose()


def _store(engine, tmp_path):
    return PostgreSQLOperationsStore(
        engine=engine,
        artifact_store=LocalContentAddressedArtifactStore(
            tmp_path / "objects"
        ),
    )


def test_alembic_upgrade_downgrade_and_schema_verification(
    postgres_engine,
    tmp_path,
):
    inspector = inspect(postgres_engine)
    assert {
        "jobs",
        "job_items",
        "artifacts",
        "review_tasks",
        "review_events",
        "alembic_version",
    }.issubset(inspector.get_table_names())

    with postgres_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == EXPECTED_ALEMBIC_REVISION

    with postgres_engine.begin() as connection:
        command.downgrade(_alembic_config(connection), "base")
    assert "jobs" not in inspect(postgres_engine).get_table_names()
    with pytest.raises(OperationsContractError, match="schema revision"):
        _store(postgres_engine, tmp_path)

    with postgres_engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")
        command.check(_alembic_config(connection))
    with _store(postgres_engine, tmp_path) as store:
        assert store.create_job("after-upgrade", ["VUSXX"], "v7").status is (
            JobStatus.SUBMITTED
        )


def test_postgres_matches_scoped_idempotency_contract(postgres_engine, tmp_path):
    with _store(postgres_engine, tmp_path) as store:
        assert_scoped_idempotency_contract(store)


def test_postgres_matches_active_claim_contract(postgres_engine, tmp_path):
    with _store(postgres_engine, tmp_path) as first, _store(
        postgres_engine,
        tmp_path,
    ) as second:
        assert_active_claim_isolation_contract(first, second)


def test_skip_locked_claims_each_item_once_under_contention(
    postgres_engine,
    tmp_path,
):
    store = _store(postgres_engine, tmp_path)
    tickers = [f"T{number:04d}" for number in range(40)]
    job = store.create_job("contention", tickers, "v7")
    claimed = []
    claimed_lock = threading.Lock()

    def consume(worker_number):
        while True:
            item = store.claim_next_item(
                job.job_id,
                f"worker-{worker_number}",
                300,
            )
            if item is None:
                return
            with claimed_lock:
                claimed.append(item.item_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(consume, range(8)))

    assert len(claimed) == 40
    assert len(set(claimed)) == 40
    assert all(
        item.status is ItemStatus.RUNNING
        for item in store.list_items(job.job_id)
    )


def test_postgres_lease_recovery_uses_database_time(postgres_engine, tmp_path):
    with _store(postgres_engine, tmp_path) as store:
        job = store.create_job("lease-recovery", ["VUSXX"], "v7")
        first = store.claim_next_item(job.job_id, "worker-1", 300)
        assert first is not None

        with postgres_engine.begin() as connection:
            connection.exec_driver_sql(
                """
                UPDATE job_items
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE item_id = %s
                """,
                (first.item_id,),
            )

        reclaimed = store.claim_next_item(job.job_id, "worker-2", 300)
        assert reclaimed is not None
        assert reclaimed.item_id == first.item_id
        assert reclaimed.attempt_count == 2
        with pytest.raises(LeaseOwnershipError):
            store.fail_item(first.item_id, "worker-1", "late failure")


def test_postgres_persists_mixed_results_and_content_addressed_artifacts(
    postgres_engine,
    tmp_path,
):
    vusxx_manifest, vusxx_path = manifest_for(tmp_path, "VUSXX")
    _, qqq_path = manifest_for(
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

    with _store(postgres_engine, tmp_path) as store:
        runner = PersistentJobRunner(store, fetcher, worker_id="worker-1")
        job = runner.submit(
            "mixed-postgres",
            ["VUSXX", "QQQ", "ZZZZ"],
            "v7",
            idempotency_scope="nightly",
        )
        completed = runner.run(job.job_id)
        items = store.list_items(job.job_id)

        assert completed.status is JobStatus.COMPLETED_WITH_ERRORS
        assert [item.status for item in items] == [
            ItemStatus.VERIFIED,
            ItemStatus.REVIEW_REQUIRED,
            ItemStatus.FAILED,
        ]
        assert store.persisted_manifest(items[0].item_id) == vusxx_manifest
        artifact = store.artifact_records(items[0].item_id)[0]
        assert artifact["storage_provider"] == "local-cas"
        assert artifact["object_key"].startswith("sha256/")
        assert [task.ticker for task in store.list_review_tasks()] == ["QQQ"]

        runner.run(job.job_id)
        assert fetcher.fetch.call_count == 3
