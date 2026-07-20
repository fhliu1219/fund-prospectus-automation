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


def assert_direct_claim_contract(first_store, second_store):
    job = first_store.create_job(
        "direct-claim",
        ["VUSXX", "SPY"],
        "v7",
    )
    vusxx, spy = first_store.list_items(job.job_id)

    claimed = first_store.claim_item(vusxx.item_id, "temporal-vusxx", 60)
    blocked = second_store.claim_item(vusxx.item_id, "other-worker", 60)
    spy_claim = second_store.claim_item(spy.item_id, "temporal-spy", 60)
    renewed = first_store.claim_item(vusxx.item_id, "temporal-vusxx", 60)

    assert claimed is not None and claimed.ticker == "VUSXX"
    assert blocked is None
    assert spy_claim is not None and spy_claim.ticker == "SPY"
    assert renewed is not None
    assert renewed.lease_owner == "temporal-vusxx"
    assert renewed.attempt_count == claimed.attempt_count
    assert first_store.get_item(vusxx.item_id) == renewed
