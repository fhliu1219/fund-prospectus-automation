"""Versioned, byte-free payloads exchanged through Temporal history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional


TEMPORAL_PAYLOAD_SCHEMA_VERSION = 1
BATCH_WORKFLOW_NAME = "FundProspectusBatchWorkflow.v1"
TICKER_WORKFLOW_NAME = "FundProspectusTickerWorkflow.v1"
TEMPORAL_TASK_QUEUE = "fund-prospectus-v1"


@dataclass(frozen=True)
class BatchWorkflowInput:
    idempotency_key: str
    tickers: List[str]
    validation_policy: str
    idempotency_scope: str = "internal"
    max_concurrency: int = 10
    schema_version: int = TEMPORAL_PAYLOAD_SCHEMA_VERSION


@dataclass(frozen=True)
class JobItemRef:
    item_id: str
    ticker: str
    ordinal: int


@dataclass(frozen=True)
class JobPlan:
    job_id: str
    validation_policy: str
    items: List[JobItemRef]


@dataclass(frozen=True)
class TickerWorkflowInput:
    job_id: str
    item_id: str
    ticker: str
    validation_policy: str
    schema_version: int = TEMPORAL_PAYLOAD_SCHEMA_VERSION


@dataclass(frozen=True)
class ClaimItemInput:
    job_id: str
    item_id: str
    ticker: str
    owner_id: str


@dataclass(frozen=True)
class ClaimItemResult:
    should_process: bool
    status: str
    attempt_count: int
    document_verification: Optional[str] = None
    artifact_count: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class ResolveFilingInput:
    ticker: str
    validation_policy: str


@dataclass(frozen=True)
class FilingSelectionPayload:
    ticker: str
    cik: int
    series_id: Optional[str]
    class_id: Optional[str]
    mapping_source: str
    registrant_cik: int
    accession: str
    form: str
    filing_date: str
    filing_series_id: Optional[str]
    filing_class_id: Optional[str]
    filing_detail_url: Optional[str]
    document_url: Optional[str]
    fund_name: Optional[str]
    selection_reason: str
    heuristic_used: bool
    identity_level: str
    identity_evidence: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class BuildPackageInput:
    job_id: str
    item_id: str
    owner_id: str
    validation_policy: str
    selection: FilingSelectionPayload


@dataclass(frozen=True)
class RecordFailureInput:
    job_id: str
    item_id: str
    ticker: str
    owner_id: str
    error: str


@dataclass(frozen=True)
class ReadJobInput:
    job_id: str


@dataclass(frozen=True)
class TickerWorkflowResult:
    item_id: str
    ticker: str
    status: str
    document_verification: Optional[str] = None
    artifact_count: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class BatchWorkflowResult:
    job_id: str
    status: str
    items: List[TickerWorkflowResult]


def normalized_tickers(tickers: List[str]) -> List[str]:
    values: List[str] = []
    seen = set()
    for raw in tickers:
        ticker = raw.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            values.append(ticker)
    return values


def batch_workflow_id(value: BatchWorkflowInput) -> str:
    """Build a stable ID while allowing PostgreSQL to detect key conflicts."""
    tickers = normalized_tickers(value.tickers)
    request = json.dumps(
        {
            "tickers": tickers,
            "validation_policy": value.validation_policy,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key = (
        f"{value.idempotency_scope.strip()}|{value.idempotency_key.strip()}"
    ).encode("utf-8")
    key_digest = hashlib.sha256(key).hexdigest()[:16]
    request_digest = hashlib.sha256(request).hexdigest()[:16]
    return f"prospectus-batch-{key_digest}-{request_digest}"
