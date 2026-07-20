"""Deterministic Temporal workflows for durable prospectus retrieval."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from .temporal_contracts import (
        TEMPORAL_PAYLOAD_SCHEMA_VERSION,
        BATCH_WORKFLOW_NAME,
        TICKER_WORKFLOW_NAME,
        BatchWorkflowInput,
        BatchWorkflowResult,
        BuildPackageInput,
        ClaimItemInput,
        ClaimItemResult,
        FilingSelectionPayload,
        JobPlan,
        ReadJobInput,
        RecordFailureInput,
        ResolveFilingInput,
        TickerWorkflowInput,
        TickerWorkflowResult,
    )


PREPARE_JOB_ACTIVITY = "prepare_prospectus_job.v1"
CLAIM_ITEM_ACTIVITY = "claim_prospectus_item.v1"
RESOLVE_FILING_ACTIVITY = "resolve_prospectus_filing.v1"
BUILD_PACKAGE_ACTIVITY = "build_and_persist_prospectus_package.v1"
RECORD_FAILURE_ACTIVITY = "record_prospectus_failure.v1"
READ_JOB_STATUS_ACTIVITY = "read_prospectus_job_status.v1"

_DATABASE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)
_SEC_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
)
_BUILD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
)


@workflow.defn(name=BATCH_WORKFLOW_NAME)
class FundProspectusBatchWorkflow:
    """Create one bounded child workflow per normalized ticker."""

    @workflow.run
    async def run(self, value: BatchWorkflowInput) -> BatchWorkflowResult:
        _validate_batch_input(value)
        plan = await workflow.execute_activity(
            PREPARE_JOB_ACTIVITY,
            value,
            result_type=JobPlan,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=_DATABASE_RETRY,
        )

        results = []
        for offset in range(0, len(plan.items), value.max_concurrency):
            handles = []
            for item in plan.items[offset : offset + value.max_concurrency]:
                child_input = TickerWorkflowInput(
                    job_id=plan.job_id,
                    item_id=item.item_id,
                    ticker=item.ticker,
                    validation_policy=plan.validation_policy,
                )
                handles.append(
                    await workflow.start_child_workflow(
                        FundProspectusTickerWorkflow.run,
                        child_input,
                        id=f"prospectus-item-{item.item_id}",
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                )
            results.extend(await asyncio.gather(*handles))

        status = await workflow.execute_activity(
            READ_JOB_STATUS_ACTIVITY,
            ReadJobInput(job_id=plan.job_id),
            result_type=str,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DATABASE_RETRY,
        )
        return BatchWorkflowResult(
            job_id=plan.job_id,
            status=status,
            items=results,
        )


@workflow.defn(name=TICKER_WORKFLOW_NAME)
class FundProspectusTickerWorkflow:
    """Preserve selection before retrying package construction and storage."""

    @workflow.run
    async def run(self, value: TickerWorkflowInput) -> TickerWorkflowResult:
        _validate_ticker_input(value)
        owner_id = f"temporal:{workflow.info().workflow_id}"
        claim = await workflow.execute_activity(
            CLAIM_ITEM_ACTIVITY,
            ClaimItemInput(
                job_id=value.job_id,
                item_id=value.item_id,
                ticker=value.ticker,
                owner_id=owner_id,
            ),
            result_type=ClaimItemResult,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DATABASE_RETRY,
        )
        if not claim.should_process:
            return TickerWorkflowResult(
                item_id=value.item_id,
                ticker=value.ticker,
                status=claim.status,
                document_verification=claim.document_verification,
                artifact_count=claim.artifact_count,
                error=claim.error,
            )

        try:
            selection = await workflow.execute_activity(
                RESOLVE_FILING_ACTIVITY,
                ResolveFilingInput(
                    ticker=value.ticker,
                    validation_policy=value.validation_policy,
                ),
                result_type=FilingSelectionPayload,
                start_to_close_timeout=timedelta(minutes=3),
                schedule_to_close_timeout=timedelta(minutes=10),
                retry_policy=_SEC_RETRY,
            )
            return await workflow.execute_activity(
                BUILD_PACKAGE_ACTIVITY,
                BuildPackageInput(
                    job_id=value.job_id,
                    item_id=value.item_id,
                    owner_id=owner_id,
                    validation_policy=value.validation_policy,
                    selection=selection,
                ),
                result_type=TickerWorkflowResult,
                start_to_close_timeout=timedelta(minutes=20),
                schedule_to_close_timeout=timedelta(minutes=45),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_BUILD_RETRY,
            )
        except ActivityError as exc:
            return await workflow.execute_activity(
                RECORD_FAILURE_ACTIVITY,
                RecordFailureInput(
                    job_id=value.job_id,
                    item_id=value.item_id,
                    ticker=value.ticker,
                    owner_id=owner_id,
                    error=_activity_error(exc),
                ),
                result_type=TickerWorkflowResult,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=_DATABASE_RETRY,
            )


def _validate_batch_input(value: BatchWorkflowInput) -> None:
    if value.schema_version != TEMPORAL_PAYLOAD_SCHEMA_VERSION:
        raise ApplicationError(
            f"unsupported Temporal payload schema {value.schema_version}",
            type="invalid_workflow_input",
            non_retryable=True,
        )
    if not value.idempotency_key.strip():
        raise ApplicationError(
            "idempotency_key must not be empty",
            type="invalid_workflow_input",
            non_retryable=True,
        )
    if not value.tickers:
        raise ApplicationError(
            "at least one ticker is required",
            type="invalid_workflow_input",
            non_retryable=True,
        )
    if not 1 <= value.max_concurrency <= 100:
        raise ApplicationError(
            "max_concurrency must be between 1 and 100",
            type="invalid_workflow_input",
            non_retryable=True,
        )


def _validate_ticker_input(value: TickerWorkflowInput) -> None:
    if value.schema_version != TEMPORAL_PAYLOAD_SCHEMA_VERSION:
        raise ApplicationError(
            f"unsupported Temporal payload schema {value.schema_version}",
            type="invalid_workflow_input",
            non_retryable=True,
        )
    if not value.job_id or not value.item_id or not value.ticker:
        raise ApplicationError(
            "job_id, item_id, and ticker are required",
            type="invalid_workflow_input",
            non_retryable=True,
        )


def _activity_error(error: ActivityError) -> str:
    cause = error.cause
    detail = str(cause if cause is not None else error).strip()
    return detail[:4_000] or "Temporal Activity failed without an error message"
