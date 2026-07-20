"""Submit an idempotent prospectus batch to Temporal."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from .package import VALIDATION_POLICIES, V7_VALIDATION_POLICY
from .temporal_config import TemporalConnectionSettings, connect_temporal
from .temporal_contracts import (
    BatchWorkflowInput,
    BatchWorkflowResult,
    batch_workflow_id,
    normalized_tickers,
)
from .temporal_workflows import FundProspectusBatchWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a durable prospectus retrieval batch",
    )
    parser.add_argument(
        "tickers",
        nargs="+",
        help="fund tickers (space or comma separated)",
    )
    parser.add_argument(
        "--idempotency-key",
        required=True,
        help="caller-controlled key for this logical request",
    )
    parser.add_argument(
        "--idempotency-scope",
        default="internal",
        help="namespace for the idempotency key",
    )
    parser.add_argument(
        "--validation-policy",
        choices=VALIDATION_POLICIES,
        default=V7_VALIDATION_POLICY,
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=10,
        help="maximum concurrently active ticker child workflows",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait for the batch result instead of returning after submission",
    )
    return parser


def _tickers(raw_values):
    values = []
    for raw in raw_values:
        values.extend(raw.replace(",", " ").split())
    return normalized_tickers(values)


async def submit(args) -> int:
    connection = TemporalConnectionSettings.from_env()
    client = await connect_temporal(connection)
    value = BatchWorkflowInput(
        idempotency_key=args.idempotency_key,
        idempotency_scope=args.idempotency_scope,
        tickers=_tickers(args.tickers),
        validation_policy=args.validation_policy,
        max_concurrency=args.max_concurrency,
    )
    workflow_id = batch_workflow_id(value)
    try:
        handle = await client.start_workflow(
            FundProspectusBatchWorkflow.run,
            value,
            id=workflow_id,
            task_queue=connection.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        submitted = True
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(
            workflow_id,
            result_type=BatchWorkflowResult,
        )
        submitted = False

    if not args.wait:
        print(
            json.dumps(
                {
                    "workflow_id": workflow_id,
                    "submitted": submitted,
                    "namespace": connection.namespace,
                    "task_queue": connection.task_queue,
                },
                sort_keys=True,
            )
        )
        return 0

    try:
        result = await handle.result()
    except WorkflowFailureError as exc:
        print(f"workflow failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not _tickers(args.tickers):
        build_parser().error("at least one non-empty ticker is required")
    if not 1 <= args.max_concurrency <= 100:
        build_parser().error("--max-concurrency must be between 1 and 100")
    try:
        return asyncio.run(submit(args))
    except (OSError, RPCError, ValueError) as exc:
        print(f"submission failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
