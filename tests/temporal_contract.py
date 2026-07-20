"""Opt-in Temporal server contract.

Run with:
RUN_TEMPORAL_TESTS=1 pytest tests/temporal_contract.py -q

The Temporal SDK downloads an ephemeral test server on the first run.
"""

import asyncio
import os

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from prospectus_fetcher.temporal_contracts import (
    BatchWorkflowInput,
    BuildPackageInput,
    ClaimItemInput,
    ClaimItemResult,
    FilingSelectionPayload,
    JobItemRef,
    JobPlan,
    ReadJobInput,
    RecordFailureInput,
    ResolveFilingInput,
    TickerWorkflowResult,
)
from prospectus_fetcher.temporal_workflows import (
    BUILD_PACKAGE_ACTIVITY,
    CLAIM_ITEM_ACTIVITY,
    PREPARE_JOB_ACTIVITY,
    READ_JOB_STATUS_ACTIVITY,
    RECORD_FAILURE_ACTIVITY,
    RESOLVE_FILING_ACTIVITY,
    FundProspectusBatchWorkflow,
    FundProspectusTickerWorkflow,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TEMPORAL_TESTS") != "1",
    reason="set RUN_TEMPORAL_TESTS=1 for Temporal server contract tests",
)


def test_workflow_retries_only_the_failed_stage():
    asyncio.run(_run_retry_contract())


async def _run_retry_contract():
    attempts = {
        "prepare": 0,
        "claim": 0,
        "resolve": 0,
        "build": 0,
        "record_failure": 0,
        "read": 0,
    }

    @activity.defn(name=PREPARE_JOB_ACTIVITY)
    async def prepare(value: BatchWorkflowInput) -> JobPlan:
        attempts["prepare"] += 1
        return JobPlan(
            job_id="job-1",
            validation_policy=value.validation_policy,
            items=[
                JobItemRef(
                    item_id="item-1",
                    ticker=value.tickers[0],
                    ordinal=0,
                )
            ],
        )

    @activity.defn(name=CLAIM_ITEM_ACTIVITY)
    async def claim(value: ClaimItemInput) -> ClaimItemResult:
        attempts["claim"] += 1
        return ClaimItemResult(
            should_process=True,
            status="running",
            attempt_count=1,
        )

    @activity.defn(name=RESOLVE_FILING_ACTIVITY)
    async def resolve(value: ResolveFilingInput) -> FilingSelectionPayload:
        attempts["resolve"] += 1
        return FilingSelectionPayload(
            ticker=value.ticker,
            cik=891190,
            series_id="S000002233",
            class_id="C000005732",
            mapping_source="mf",
            registrant_cik=891190,
            accession="0000000001-26-000001",
            form="497K",
            filing_date="2026-01-01",
            filing_series_id="S000002233",
            filing_class_id="C000005732",
            filing_detail_url="https://www.sec.gov/filing",
            document_url="https://www.sec.gov/document.htm",
            fund_name="Vanguard Treasury Money Market Fund",
            selection_reason="class-associated 497K",
            heuristic_used=False,
            identity_level="class",
        )

    @activity.defn(name=BUILD_PACKAGE_ACTIVITY)
    async def build(value: BuildPackageInput) -> TickerWorkflowResult:
        attempts["build"] += 1
        if attempts["build"] == 1:
            raise ApplicationError(
                "temporary artifact-store outage",
                type="artifact_store_transient",
            )
        return TickerWorkflowResult(
            item_id=value.item_id,
            ticker=value.selection.ticker,
            status="verified",
            document_verification="document_verified",
            artifact_count=1,
        )

    @activity.defn(name=RECORD_FAILURE_ACTIVITY)
    async def record_failure(
        value: RecordFailureInput,
    ) -> TickerWorkflowResult:
        attempts["record_failure"] += 1
        return TickerWorkflowResult(
            item_id=value.item_id,
            ticker=value.ticker,
            status="failed",
            error=value.error,
        )

    @activity.defn(name=READ_JOB_STATUS_ACTIVITY)
    async def read(value: ReadJobInput) -> str:
        attempts["read"] += 1
        return "completed"

    server_path = os.environ.get("TEMPORAL_TEST_SERVER_PATH")
    environment_options = {}
    if server_path:
        environment_options["test_server_existing_path"] = server_path
    async with await WorkflowEnvironment.start_time_skipping(
        **environment_options
    ) as environment:
        async with Worker(
            environment.client,
            task_queue="temporal-contract-test",
            workflows=[
                FundProspectusBatchWorkflow,
                FundProspectusTickerWorkflow,
            ],
            activities=[
                prepare,
                claim,
                resolve,
                build,
                record_failure,
                read,
            ],
        ):
            handle = await environment.client.start_workflow(
                FundProspectusBatchWorkflow.run,
                BatchWorkflowInput(
                    idempotency_key="temporal-contract",
                    tickers=["VUSXX"],
                    validation_policy="v7",
                    max_concurrency=1,
                ),
                id="temporal-contract-test",
                task_queue="temporal-contract-test",
            )
            result = await asyncio.wait_for(handle.result(), timeout=30)

    assert result.status == "completed"
    assert result.items[0].status == "verified"
    assert attempts == {
        "prepare": 1,
        "claim": 1,
        "resolve": 1,
        "build": 2,
        "record_failure": 0,
        "read": 1,
    }
