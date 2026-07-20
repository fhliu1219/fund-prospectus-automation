"""Reusable assertions shared by SQLite and PostgreSQL repository tests."""

import pytest

from prospectus_fetcher.operations import IdempotencyConflict


def assert_scoped_idempotency_contract(store):
    first = store.create_job(
        "nightly-2026-07-20",
        ["vusxx", "VUSXX", "spy"],
        "v7",
    )
    repeated = store.create_job(
        "nightly-2026-07-20",
        ["VUSXX", "SPY"],
        "v7",
    )
    separate_scope = store.create_job(
        "nightly-2026-07-20",
        ["VUSXX", "SPY"],
        "v7",
        idempotency_scope="backfill",
    )

    assert repeated.job_id == first.job_id
    assert separate_scope.job_id != first.job_id
    assert first.idempotency_scope == "internal"
    assert separate_scope.idempotency_scope == "backfill"
    assert [item.ticker for item in store.list_items(first.job_id)] == [
        "VUSXX",
        "SPY",
    ]
    with pytest.raises(IdempotencyConflict):
        store.create_job(
            "nightly-2026-07-20",
            ["VUSXX", "QQQ"],
            "v7",
        )


def assert_active_claim_isolation_contract(first_store, second_store):
    job = first_store.create_job(
        "two-workers",
        ["VUSXX", "SPY"],
        "v7",
    )

    first_claim = first_store.claim_next_item(job.job_id, "worker-1", 60)
    second_claim = second_store.claim_next_item(job.job_id, "worker-2", 60)
    no_third_claim = first_store.claim_next_item(
        job.job_id,
        "worker-3",
        60,
    )

    assert first_claim is not None and first_claim.ticker == "VUSXX"
    assert second_claim is not None and second_claim.ticker == "SPY"
    assert first_claim.item_id != second_claim.item_id
    assert no_third_claim is None
