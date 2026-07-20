"""Temporal Activities that contain every network and persistence side effect."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import requests
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import DBAPIError, OperationalError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .artifact_store import ArtifactIntegrityError
from .cli import ProspectusFetcher
from .models import Filing, IdentityLevel, ResolvedFund
from .operations import (
    IdempotencyConflict,
    ItemStatus,
    JobItem,
    OperationsContractError,
    OperationsStore,
    load_result_manifest,
)
from .sec_schema import SECResponseSchemaError
from .temporal_contracts import (
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
from .temporal_workflows import (
    BUILD_PACKAGE_ACTIVITY,
    CLAIM_ITEM_ACTIVITY,
    PREPARE_JOB_ACTIVITY,
    READ_JOB_STATUS_ACTIVITY,
    RECORD_FAILURE_ACTIVITY,
    RESOLVE_FILING_ACTIVITY,
)


StoreFactory = Callable[[], AbstractContextManager]
FetcherFactory = Callable[[str, Optional[str]], ProspectusFetcher]
_TERMINAL_ITEM_STATUSES = {
    ItemStatus.VERIFIED,
    ItemStatus.REVIEW_REQUIRED,
    ItemStatus.FAILED,
}


@dataclass(frozen=True)
class _FailureClassification:
    error_type: str
    retryable: bool
    next_retry_delay: Optional[timedelta] = None


class ProspectusTemporalActivities:
    """Dependency-injected Activities suitable for PostgreSQL or local tests."""

    def __init__(
        self,
        store_factory: StoreFactory,
        fetcher_factory: FetcherFactory,
        lease_seconds: int = 1_800,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store_factory = store_factory
        self.fetcher_factory = fetcher_factory
        self.lease_seconds = lease_seconds

    @activity.defn(name=PREPARE_JOB_ACTIVITY)
    def prepare_job(self, value: BatchWorkflowInput) -> JobPlan:
        try:
            with self.store_factory() as store:
                job = store.create_job(
                    value.idempotency_key,
                    value.tickers,
                    value.validation_policy,
                    idempotency_scope=value.idempotency_scope,
                )
                items = store.list_items(job.job_id)
            return JobPlan(
                job_id=job.job_id,
                validation_policy=job.validation_policy,
                items=[
                    JobItemRef(
                        item_id=item.item_id,
                        ticker=item.ticker,
                        ordinal=item.ordinal,
                    )
                    for item in items
                ],
            )
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("prepare_job", exc)

    @activity.defn(name=CLAIM_ITEM_ACTIVITY)
    def claim_item(self, value: ClaimItemInput) -> ClaimItemResult:
        try:
            with self.store_factory() as store:
                item = _validated_item(
                    store,
                    value.job_id,
                    value.item_id,
                    value.ticker,
                )
                if item.status in _TERMINAL_ITEM_STATUSES:
                    return _claim_result(store, item, should_process=False)
                claimed = store.claim_item(
                    value.item_id,
                    value.owner_id,
                    self.lease_seconds,
                )
                if claimed is None:
                    latest = store.get_item(value.item_id)
                    if latest.status in _TERMINAL_ITEM_STATUSES:
                        return _claim_result(
                            store,
                            latest,
                            should_process=False,
                        )
                    raise ApplicationError(
                        f"{value.ticker}: item lease is owned by another worker",
                        type="item_lease_unavailable",
                    )
                return _claim_result(store, claimed, should_process=True)
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("claim_item", exc)

    @activity.defn(name=RESOLVE_FILING_ACTIVITY)
    def resolve_filing(
        self,
        value: ResolveFilingInput,
    ) -> FilingSelectionPayload:
        try:
            activity.heartbeat("resolve_ticker")
            fetcher = self.fetcher_factory(value.validation_policy, None)
            try:
                fund = fetcher.resolver.resolve(value.ticker)
                if fund is None:
                    raise ApplicationError(
                        (
                            f"could not resolve ticker {value.ticker} to an "
                            "SEC EDGAR entity"
                        ),
                        type="unresolved_ticker",
                        non_retryable=True,
                    )
                activity.heartbeat("discover_prospectus_filing")
                filing = fetcher.edgar.find_prospectus(fund)
                if filing is None:
                    raise ApplicationError(
                        f"no supported prospectus filing found for {value.ticker}",
                        type="no_supported_prospectus",
                        non_retryable=True,
                    )
                return _selection_payload(fund, filing)
            finally:
                _close_fetcher(fetcher)
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("resolve_filing", exc)

    @activity.defn(name=BUILD_PACKAGE_ACTIVITY)
    def build_and_persist(
        self,
        value: BuildPackageInput,
    ) -> TickerWorkflowResult:
        ticker = value.selection.ticker
        try:
            terminal = self._claim_or_terminal(
                value.job_id,
                value.item_id,
                ticker,
                value.owner_id,
            )
            if terminal is not None:
                return terminal

            activity.heartbeat("build_package_started")
            fetcher = self.fetcher_factory(
                value.validation_policy,
                value.item_id,
            )
            try:
                fetcher.package_builder.progress_callback = activity.heartbeat
                fund, filing = _selection_domain(value.selection)
                result = fetcher.package_builder.build(fund, filing)
                manifest = load_result_manifest(result)
            finally:
                _close_fetcher(fetcher)
            activity.heartbeat("package_built")

            with self.store_factory() as store:
                _validated_item(
                    store,
                    value.job_id,
                    value.item_id,
                    ticker,
                )
                renewed = store.claim_item(
                    value.item_id,
                    value.owner_id,
                    self.lease_seconds,
                )
                if renewed is None:
                    latest = store.get_item(value.item_id)
                    if latest.status in _TERMINAL_ITEM_STATUSES:
                        return _ticker_result(store, latest)
                    raise ApplicationError(
                        f"{ticker}: item lease could not be renewed",
                        type="item_lease_unavailable",
                    )
                completed = store.complete_item(
                    value.item_id,
                    value.owner_id,
                    result,
                    manifest,
                )
                outcome = _ticker_result(store, completed)
            activity.heartbeat("package_persisted")
            return outcome
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("build_and_persist", exc)

    @activity.defn(name=RECORD_FAILURE_ACTIVITY)
    def record_failure(
        self,
        value: RecordFailureInput,
    ) -> TickerWorkflowResult:
        try:
            with self.store_factory() as store:
                item = _validated_item(
                    store,
                    value.job_id,
                    value.item_id,
                    value.ticker,
                )
                if item.status in _TERMINAL_ITEM_STATUSES:
                    return _ticker_result(store, item)
                claimed = store.claim_item(
                    value.item_id,
                    value.owner_id,
                    self.lease_seconds,
                )
                if claimed is None:
                    raise ApplicationError(
                        f"{value.ticker}: item lease could not be acquired",
                        type="item_lease_unavailable",
                    )
                failed = store.fail_item(
                    value.item_id,
                    value.owner_id,
                    value.error,
                )
                return _ticker_result(store, failed)
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("record_failure", exc)

    @activity.defn(name=READ_JOB_STATUS_ACTIVITY)
    def read_job_status(self, value: ReadJobInput) -> str:
        try:
            with self.store_factory() as store:
                return store.get_job(value.job_id).status.value
        except ApplicationError:
            raise
        except Exception as exc:
            _raise_activity_error("read_job_status", exc)

    def _claim_or_terminal(
        self,
        job_id: str,
        item_id: str,
        ticker: str,
        owner_id: str,
    ) -> Optional[TickerWorkflowResult]:
        with self.store_factory() as store:
            item = _validated_item(store, job_id, item_id, ticker)
            if item.status in _TERMINAL_ITEM_STATUSES:
                return _ticker_result(store, item)
            claimed = store.claim_item(
                item_id,
                owner_id,
                self.lease_seconds,
            )
            if claimed is not None:
                return None
            latest = store.get_item(item_id)
            if latest.status in _TERMINAL_ITEM_STATUSES:
                return _ticker_result(store, latest)
            raise ApplicationError(
                f"{ticker}: item lease is owned by another worker",
                type="item_lease_unavailable",
            )


def _validated_item(
    store: OperationsStore,
    job_id: str,
    item_id: str,
    ticker: str,
) -> JobItem:
    job = store.get_job(job_id)
    item = store.get_item(item_id)
    if item.job_id != job.job_id:
        raise OperationsContractError(
            f"item {item_id} does not belong to job {job_id}"
        )
    if item.ticker != ticker:
        raise OperationsContractError(
            f"item {item_id} belongs to {item.ticker}, not {ticker}"
        )
    return item


def _claim_result(
    store: OperationsStore,
    item: JobItem,
    should_process: bool,
) -> ClaimItemResult:
    result = _ticker_result(store, item)
    return ClaimItemResult(
        should_process=should_process,
        status=result.status,
        attempt_count=item.attempt_count,
        document_verification=result.document_verification,
        artifact_count=result.artifact_count,
        error=result.error,
    )


def _ticker_result(
    store: OperationsStore,
    item: JobItem,
) -> TickerWorkflowResult:
    manifest = store.persisted_manifest(item.item_id)
    package = manifest.get("package", {}) if manifest else {}
    return TickerWorkflowResult(
        item_id=item.item_id,
        ticker=item.ticker,
        status=item.status.value,
        document_verification=package.get("verification"),
        artifact_count=len(store.artifact_records(item.item_id)),
        error=item.error,
    )


def _selection_payload(
    fund: ResolvedFund,
    filing: Filing,
) -> FilingSelectionPayload:
    return FilingSelectionPayload(
        ticker=fund.ticker,
        cik=fund.cik,
        series_id=fund.series_id,
        class_id=fund.class_id,
        mapping_source=fund.source,
        registrant_cik=filing.registrant_cik,
        accession=filing.accession,
        form=filing.form,
        filing_date=filing.date,
        filing_series_id=filing.series_id,
        filing_class_id=filing.class_id,
        filing_detail_url=filing.filing_detail_url,
        document_url=filing.doc_url,
        fund_name=filing.fund_name,
        selection_reason=filing.selection_reason,
        heuristic_used=filing.heuristic_used,
        identity_level=filing.identity_level.value,
        identity_evidence=list(filing.identity_evidence),
        warnings=list(filing.warnings),
    )


def _selection_domain(
    value: FilingSelectionPayload,
):
    fund = ResolvedFund(
        ticker=value.ticker,
        cik=value.cik,
        series_id=value.series_id,
        class_id=value.class_id,
        source=value.mapping_source,
    )
    filing = Filing(
        registrant_cik=value.registrant_cik,
        accession=value.accession,
        form=value.form,
        date=value.filing_date,
        series_id=value.filing_series_id,
        class_id=value.filing_class_id,
        filing_detail_url=value.filing_detail_url,
        doc_url=value.document_url,
        fund_name=value.fund_name,
        selection_reason=value.selection_reason,
        heuristic_used=value.heuristic_used,
        identity_level=IdentityLevel(value.identity_level),
        identity_evidence=list(value.identity_evidence),
        warnings=list(value.warnings),
    )
    return fund, filing


def _raise_activity_error(stage: str, exc: Exception) -> None:
    classification = _classify_failure(exc)
    raise ApplicationError(
        f"{stage}: {type(exc).__name__}: {exc}",
        type=classification.error_type,
        non_retryable=not classification.retryable,
        next_retry_delay=classification.next_retry_delay,
    ) from exc


def _classify_failure(exc: Exception) -> _FailureClassification:
    source = exc
    if isinstance(exc, ArtifactIntegrityError) and exc.__cause__ is not None:
        source = exc.__cause__

    if isinstance(source, requests.HTTPError):
        response = source.response
        status = response.status_code if response is not None else None
        if status == 429:
            return _FailureClassification(
                "sec_rate_limited",
                True,
                _retry_after(response.headers.get("Retry-After")),
            )
        if status is not None and 500 <= status <= 599:
            return _FailureClassification("sec_server_error", True)
        return _FailureClassification("sec_http_error", False)
    if isinstance(source, (requests.Timeout, requests.ConnectionError)):
        return _FailureClassification("sec_transport_error", True)
    if isinstance(source, (OperationalError,)):
        return _FailureClassification("database_unavailable", True)
    if isinstance(source, DBAPIError) and source.connection_invalidated:
        return _FailureClassification("database_connection_lost", True)
    if isinstance(source, ClientError):
        status = source.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        )
        if status == 429 or (status is not None and 500 <= status <= 599):
            return _FailureClassification("artifact_store_transient", True)
        return _FailureClassification("artifact_store_rejected", False)
    if isinstance(source, BotoCoreError):
        return _FailureClassification("artifact_store_transient", True)
    if isinstance(
        source,
        (
            IdempotencyConflict,
            OperationsContractError,
            SECResponseSchemaError,
            ArtifactIntegrityError,
            KeyError,
            ValueError,
        ),
    ):
        return _FailureClassification("deterministic_contract_error", False)
    return _FailureClassification("pipeline_bug", False)


def _retry_after(value: Optional[str]) -> Optional[timedelta]:
    if not value:
        return None
    stripped = value.strip()
    try:
        seconds = max(0, int(stripped))
        return timedelta(seconds=min(seconds, 900))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    seconds = max(
        0,
        int((retry_at - datetime.now(timezone.utc)).total_seconds()),
    )
    return timedelta(seconds=min(seconds, 900))


def _close_fetcher(fetcher) -> None:
    client = getattr(fetcher, "client", None)
    close = getattr(client, "close", None)
    if callable(close):
        close()
