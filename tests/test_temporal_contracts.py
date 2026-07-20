"""Offline tests for Temporal payload, configuration, and runtime contracts."""

from dataclasses import asdict

import pytest

from prospectus_fetcher.temporal_config import TemporalConnectionSettings
from prospectus_fetcher.temporal_contracts import (
    BatchWorkflowInput,
    FilingSelectionPayload,
    batch_workflow_id,
    normalized_tickers,
)
from prospectus_fetcher.temporal_worker import TemporalWorkerSettings


def test_normalized_tickers_are_ordered_uppercase_and_unique():
    assert normalized_tickers([" vusxx ", "QQQ", "VUSXX", ""]) == [
        "VUSXX",
        "QQQ",
    ]


def test_batch_workflow_id_is_stable_but_request_sensitive():
    first = BatchWorkflowInput(
        idempotency_key="nightly-2026-07-20",
        tickers=["vusxx", "QQQ", "VUSXX"],
        validation_policy="v7",
    )
    same = BatchWorkflowInput(
        idempotency_key="nightly-2026-07-20",
        tickers=["VUSXX", "QQQ"],
        validation_policy="v7",
    )
    changed = BatchWorkflowInput(
        idempotency_key="nightly-2026-07-20",
        tickers=["VUSXX"],
        validation_policy="v7",
    )

    assert batch_workflow_id(first) == batch_workflow_id(same)
    assert batch_workflow_id(first) != batch_workflow_id(changed)


def test_filing_selection_payload_contains_metadata_not_document_bytes():
    payload = FilingSelectionPayload(
        ticker="VUSXX",
        cik=891190,
        series_id="S000002233",
        class_id="C000005732",
        mapping_source="mf",
        registrant_cik=891190,
        accession="0001193125-26-000001",
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
        identity_evidence=["class-associated Atom feed"],
        warnings=[],
    )

    assert not any(isinstance(value, bytes) for value in asdict(payload).values())
    assert "document_bytes" not in asdict(payload)
    assert payload.accession == "0001193125-26-000001"


def test_temporal_connection_defaults_are_local_and_unauthenticated():
    settings = TemporalConnectionSettings.from_env({})

    assert settings.address == "localhost:7233"
    assert settings.namespace == "default"
    assert settings.tls_config() is False


def test_temporal_api_key_enables_tls_without_exposing_secret():
    settings = TemporalConnectionSettings.from_env(
        {
            "TEMPORAL_ADDRESS": "example.tmprl.cloud:7233",
            "TEMPORAL_NAMESPACE": "production.example",
            "TEMPORAL_API_KEY": "secret-value",
        }
    )

    assert settings.tls_config() is True
    assert "secret-value" not in repr(settings)


def test_temporal_mtls_requires_certificate_and_key_together(tmp_path):
    with pytest.raises(ValueError, match="must be set together"):
        TemporalConnectionSettings.from_env(
            {"TEMPORAL_CLIENT_CERT": str(tmp_path / "client.pem")}
        )

    certificate = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    certificate.write_text("certificate")
    key.write_text("private key")
    settings = TemporalConnectionSettings.from_env(
        {
            "TEMPORAL_CLIENT_CERT": str(certificate),
            "TEMPORAL_CLIENT_KEY": str(key),
        }
    )

    assert settings.tls_config().client_cert == b"certificate"


def test_worker_settings_require_explicit_s3_encryption():
    with pytest.raises(ValueError, match="PROSPECTUS_S3_ENCRYPTION"):
        TemporalWorkerSettings.from_env(
            {
                "PROSPECTUS_ARTIFACT_BACKEND": "s3",
                "PROSPECTUS_S3_BUCKET": "prospectus-artifacts",
            }
        )


def test_worker_settings_accept_local_defaults():
    settings = TemporalWorkerSettings.from_env({})

    assert settings.artifact_backend == "local"
    assert settings.activity_workers == 4
    assert settings.lease_seconds == 1_800
