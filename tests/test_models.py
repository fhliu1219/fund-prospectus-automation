"""Tests for confidence-model defaults and data isolation."""

from prospectus_fetcher.models import (
    DocumentVerification,
    FetchResult,
    IdentityLevel,
)


def test_fetch_result_defaults_never_claim_unearned_verification():
    first = FetchResult("VUSXX", "ok")
    second = FetchResult("SPY", "ok")

    assert first.identity_level is IdentityLevel.UNKNOWN
    assert first.document_verification is DocumentVerification.NOT_CHECKED

    first.identity_evidence.append("example")
    first.warnings.append("example warning")
    assert second.identity_evidence == []
    assert second.warnings == []
