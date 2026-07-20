"""Durable job contracts and a storage-neutral persistent pipeline runner."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

from .models import DocumentVerification, FetchResult


class JobStatus(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


class ItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFIED = "verified"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different normalized request."""


class LeaseOwnershipError(RuntimeError):
    """A worker attempted to finalize work it no longer owns."""


class OperationsContractError(ValueError):
    """Persisted job input or output violated the operations contract."""


@dataclass(frozen=True)
class OperationsJob:
    job_id: str
    idempotency_scope: str
    idempotency_key: str
    request_fingerprint: str
    validation_policy: str
    status: JobStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JobItem:
    item_id: str
    job_id: str
    ticker: str
    ordinal: int
    status: ItemStatus
    attempt_count: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[str]
    error: Optional[str]
    manifest_path: Optional[str]


@dataclass(frozen=True)
class ReviewTask:
    review_id: str
    item_id: str
    job_id: str
    ticker: str
    status: ReviewStatus
    reason: str
    created_at: str
    resolved_at: Optional[str]


@dataclass(frozen=True)
class CompletionRecord:
    status: ItemStatus
    error: Optional[str]
    result_payload: Dict[str, Any]
    manifest_payload: Optional[dict]
    identity: dict
    package: dict


class OperationsStore(Protocol):
    def create_job(
        self,
        idempotency_key: str,
        tickers: Sequence[str],
        validation_policy: str,
        idempotency_scope: str = "internal",
    ) -> OperationsJob:
        ...

    def get_job(self, job_id: str) -> OperationsJob:
        ...

    def list_items(self, job_id: str) -> List[JobItem]:
        ...

    def claim_next_item(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> Optional[JobItem]:
        ...

    def complete_item(
        self,
        item_id: str,
        worker_id: str,
        result: FetchResult,
        manifest: Optional[dict],
    ) -> JobItem:
        ...

    def fail_item(
        self,
        item_id: str,
        worker_id: str,
        error: str,
    ) -> JobItem:
        ...

    def list_review_tasks(
        self,
        status: ReviewStatus = ReviewStatus.PENDING,
    ) -> List[ReviewTask]:
        ...

    def artifact_records(self, item_id: str) -> List[dict]:
        ...

    def persisted_manifest(self, item_id: str) -> Optional[dict]:
        ...


class PersistentJobRunner:
    """Run existing ticker retrieval through durable, leased work items."""

    def __init__(
        self,
        store: OperationsStore,
        fetcher,
        worker_id: Optional[str] = None,
        lease_seconds: int = 300,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.fetcher = fetcher
        self.worker_id = worker_id or f"local-{uuid.uuid4()}"
        self.lease_seconds = lease_seconds

    def submit(
        self,
        idempotency_key: str,
        tickers: Sequence[str],
        validation_policy: str,
        idempotency_scope: str = "internal",
    ) -> OperationsJob:
        configured_policy = getattr(
            getattr(self.fetcher, "package_builder", None),
            "validation_policy",
            None,
        )
        if configured_policy and configured_policy != validation_policy:
            raise OperationsContractError(
                f"fetcher policy {configured_policy!r} does not match job policy "
                f"{validation_policy!r}"
            )
        return self.store.create_job(
            idempotency_key,
            tickers,
            validation_policy,
            idempotency_scope,
        )

    def run(self, job_id: str) -> OperationsJob:
        while True:
            item = self.store.claim_next_item(
                job_id,
                self.worker_id,
                self.lease_seconds,
            )
            if item is None:
                return self.store.get_job(job_id)

            try:
                result = self.fetcher.fetch(item.ticker)
                manifest = self._load_manifest(result)
                self.store.complete_item(
                    item.item_id,
                    self.worker_id,
                    result,
                    manifest,
                )
            except Exception as exc:
                self.store.fail_item(
                    item.item_id,
                    self.worker_id,
                    f"{type(exc).__name__}: {exc}",
                )

    @staticmethod
    def _load_manifest(result: FetchResult) -> Optional[dict]:
        if not result.ok:
            return None
        if not result.manifest_path:
            raise OperationsContractError(
                f"{result.ticker}: successful result has no manifest path"
            )
        path = Path(result.manifest_path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationsContractError(
                f"{result.ticker}: could not read package manifest {path}: {exc}"
            ) from exc
        if not isinstance(value, dict) or value.get("ticker") != result.ticker:
            raise OperationsContractError(
                f"{result.ticker}: package manifest ticker does not match result"
            )
        return value


def normalize_tickers(tickers: Sequence[str]) -> List[str]:
    values: List[str] = []
    seen = set()
    for raw in tickers:
        ticker = raw.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            values.append(ticker)
    if not values:
        raise OperationsContractError("at least one ticker is required")
    return values


def normalize_idempotency_scope(value: str) -> str:
    scope = value.strip()
    if not scope:
        raise OperationsContractError("idempotency_scope must not be empty")
    if len(scope) > 128:
        raise OperationsContractError(
            "idempotency_scope must not exceed 128 characters"
        )
    return scope


def request_fingerprint(
    tickers: Sequence[str],
    validation_policy: str,
) -> str:
    payload = json.dumps(
        {
            "tickers": list(tickers),
            "validation_policy": validation_policy,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def completion_record(
    claimed_ticker: str,
    job_policy: str,
    result: FetchResult,
    manifest: Optional[dict],
) -> CompletionRecord:
    if result.ticker != claimed_ticker:
        raise OperationsContractError(
            f"result ticker {result.ticker!r} does not match claimed ticker "
            f"{claimed_ticker!r}"
        )

    if result.ok:
        if not isinstance(manifest, dict):
            raise OperationsContractError(
                f"{result.ticker}: successful result requires a manifest"
            )
        status = (
            ItemStatus.VERIFIED
            if result.document_verification is DocumentVerification.VERIFIED
            else ItemStatus.REVIEW_REQUIRED
        )
        error = None
    else:
        if manifest is not None:
            raise OperationsContractError(
                f"{result.ticker}: failed result must not include a manifest"
            )
        status = ItemStatus.FAILED
        error = result.error or "retrieval failed without an error message"

    identity = manifest.get("identity", {}) if manifest else {}
    package = manifest.get("package", {}) if manifest else {}
    if manifest is not None:
        if manifest.get("ticker") != result.ticker:
            raise OperationsContractError(
                "manifest ticker does not match the completed result"
            )
        if not isinstance(identity, dict):
            raise OperationsContractError("manifest.identity must be an object")
        if not isinstance(package, dict):
            raise OperationsContractError("manifest.package must be an object")
        if package.get("validation_policy") != job_policy:
            raise OperationsContractError(
                "manifest validation policy does not match the job"
            )
        if package.get("verification") != result.document_verification.value:
            raise OperationsContractError(
                "manifest verification does not match the completed result"
            )

    return CompletionRecord(
        status=status,
        error=error,
        result_payload={
            "ticker": result.ticker,
            "status": result.status,
            "form": result.form,
            "filing_date": result.date,
            "path": result.path,
            "error": result.error,
            "document_kind": result.document_kind.value,
            "document_verification": result.document_verification.value,
            "warnings": result.warnings,
        },
        manifest_payload=manifest,
        identity=identity,
        package=package,
    )
