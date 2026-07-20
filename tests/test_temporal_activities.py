"""Offline Activity tests using SQLite and mocked SEC-facing components."""

from types import SimpleNamespace
from unittest.mock import Mock

import requests
from temporalio.testing import ActivityEnvironment

from prospectus_fetcher.models import Filing, IdentityLevel, ResolvedFund
from prospectus_fetcher.sqlite_operations import SQLiteOperationsStore
from prospectus_fetcher.temporal_activities import (
    ProspectusTemporalActivities,
    _classify_failure,
)
from prospectus_fetcher.temporal_contracts import (
    BatchWorkflowInput,
    BuildPackageInput,
    ClaimItemInput,
    ResolveFilingInput,
)
from tests.test_operations import manifest_for, successful_result


def test_activities_execute_one_item_through_durable_completion(tmp_path):
    database_path = str(tmp_path / "operations.db")
    manifest, manifest_path = manifest_for(tmp_path, "VUSXX")
    result = successful_result("VUSXX", manifest_path)
    fund = ResolvedFund(
        ticker="VUSXX",
        cik=891190,
        series_id="S000002233",
        class_id="C000005732",
        source="mf",
    )
    filing = Filing(
        registrant_cik=891190,
        accession="0000000001-26-000001",
        form="497K",
        date="2026-01-01",
        series_id=fund.series_id,
        class_id=fund.class_id,
        doc_url="https://www.sec.gov/example.htm",
        selection_reason="class-associated 497K",
        identity_level=IdentityLevel.CLASS,
        identity_evidence=["class-associated Atom feed"],
    )
    created_clients = []

    def store_factory():
        return SQLiteOperationsStore(database_path)

    def fetcher_factory(validation_policy, workspace_id):
        client = Mock()
        created_clients.append(client)
        resolver = Mock()
        resolver.resolve.return_value = fund
        edgar = Mock()
        edgar.find_prospectus.return_value = filing
        package_builder = SimpleNamespace(
            progress_callback=None,
            build=Mock(return_value=result),
        )
        return SimpleNamespace(
            client=client,
            resolver=resolver,
            edgar=edgar,
            package_builder=package_builder,
        )

    activities = ProspectusTemporalActivities(
        store_factory,
        fetcher_factory,
        lease_seconds=300,
    )
    environment = ActivityEnvironment()
    heartbeats = []
    environment.on_heartbeat = lambda *details: heartbeats.extend(details)

    plan = environment.run(
        activities.prepare_job,
        BatchWorkflowInput(
            idempotency_key="activity-contract",
            tickers=["VUSXX"],
            validation_policy="v7",
        ),
    )
    item = plan.items[0]
    claim = environment.run(
        activities.claim_item,
        ClaimItemInput(
            job_id=plan.job_id,
            item_id=item.item_id,
            ticker=item.ticker,
            owner_id="temporal:test",
        ),
    )
    selection = environment.run(
        activities.resolve_filing,
        ResolveFilingInput(ticker="VUSXX", validation_policy="v7"),
    )
    completed = environment.run(
        activities.build_and_persist,
        BuildPackageInput(
            job_id=plan.job_id,
            item_id=item.item_id,
            owner_id="temporal:test",
            validation_policy="v7",
            selection=selection,
        ),
    )

    assert claim.should_process is True
    assert completed.status == "verified"
    assert completed.document_verification == "document_verified"
    assert completed.artifact_count == len(manifest["documents"])
    assert "resolve_ticker" in heartbeats
    assert "package_persisted" in heartbeats
    assert len(created_clients) == 2
    assert all(client.close.call_count == 1 for client in created_clients)

    repeated = environment.run(
        activities.claim_item,
        ClaimItemInput(
            job_id=plan.job_id,
            item_id=item.item_id,
            ticker=item.ticker,
            owner_id="temporal:other",
        ),
    )
    assert repeated.should_process is False
    assert repeated.status == "verified"


def test_retry_classification_distinguishes_transient_sec_failures():
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "17"
    rate_limited = requests.HTTPError(response=response)

    classification = _classify_failure(rate_limited)

    assert classification.error_type == "sec_rate_limited"
    assert classification.retryable is True
    assert classification.next_retry_delay.total_seconds() == 17
    assert _classify_failure(requests.Timeout()).retryable is True


def test_retry_classification_does_not_retry_programming_errors():
    classification = _classify_failure(RuntimeError("unexpected defect"))

    assert classification.error_type == "pipeline_bug"
    assert classification.retryable is False
