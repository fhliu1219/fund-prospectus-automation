"""Document-package assembly tests: validation, linkage, and manifests."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from prospectus_fetcher.downloader import Downloader
from prospectus_fetcher.edgar import ArchiveDocumentRef, ArchiveInventory, FilingRef
from prospectus_fetcher.filing_identity import (
    ClassIdentity,
    FilingIdentityMetadata,
    FilingIdentityParseError,
    SeriesIdentity,
)
from prospectus_fetcher.models import (
    CandidateDisposition,
    CandidatePurpose,
    DocumentKind,
    DocumentRole,
    DocumentVerification,
    Filing,
    IdentityLevel,
    ResolvedFund,
)
from prospectus_fetcher.package import DocumentPackageBuilder


SUMMARY = b"""
<html><body>
<h1>Summary Prospectus dated December 22, 2025</h1>
<p>Vanguard Treasury Money Market Fund (VUSXX)</p>
<h2>Investment Objective</h2>
<h2>Fees and Expenses</h2>
<h2>Principal Investment Strategies</h2>
</body></html>
"""

SUPPLEMENT = b"""
<html><body>
<h1>Supplement dated June 10, 2026 to the Summary Prospectus dated December 22, 2025</h1>
<p>Invesco QQQ Trust, Series 1 (QQQ)</p>
<p>This supplement amends the prospectus.</p>
</body></html>
"""

QQQ_BASE = b"""
<html><body>
<h1>Summary Prospectus dated December 22, 2025</h1>
<p>Invesco QQQ Trust, Series 1 (QQQ)</p>
<h2>Investment Objective</h2>
<h2>Fees and Expenses</h2>
<h2>Principal Investment Strategies</h2>
</body></html>
"""

SPY_PROSPECTUS = b"""
<html><body>
<h1>Prospectus dated January 26, 2026</h1>
<p>SPDR S&amp;P 500 ETF Trust (SPY)</p>
<h2>Investment Objective</h2>
<h2>Fees and Expenses</h2>
<h2>Principal Risks</h2>
</body></html>
"""

UNKNOWN_PRIMARY = b"<html><body><p>Administrative cover page</p></body></html>"

V7_FUND_SUMMARY = b"""
<html><body>
<h1>Example Fund</h1>
<table><tr><td>Admiral Shares</td><td>VUSXX</td></tr></table>
<h2>Fund Summary</h2>
<h2>Investment Goal</h2>
<h2>Fees and Expenses</h2>
<h2>Principal Risks</h2>
<h2>Performance Information</h2>
</body></html>
"""


def filing(
    accession: str,
    url: str,
    date: str,
    identity: IdentityLevel = IdentityLevel.CLASS,
) -> Filing:
    return Filing(
        registrant_cik=1,
        accession=accession,
        form="497K",
        date=date,
        class_id="C1" if identity is IdentityLevel.CLASS else None,
        doc_url=url,
        selection_reason="test selection",
        identity_level=identity,
        identity_evidence=[f"selected at {identity.value} level"],
    )


def builder(tmp_path, content_by_url, candidates=()):
    client = Mock()
    client.get_bytes.side_effect = lambda url: content_by_url[url]
    downloader = Downloader(client, output_dir=str(tmp_path))
    edgar = Mock()
    refs = [
        FilingRef(
            candidate.form,
            candidate.date,
            candidate.accession,
            primary_document=candidate.doc_url,
        )
        for candidate in candidates
    ]
    filings = {candidate.accession: candidate for candidate in candidates}
    edgar.related_prospectus_refs.return_value = refs
    edgar.resolve_related_prospectus.side_effect = (
        lambda fund, selected, ref: filings[ref.accession]
    )
    return DocumentPackageBuilder(edgar, downloader), edgar


def archive_inventory(*documents):
    return ArchiveInventory(
        index_url="https://www.sec.gov/example/index.json",
        documents=list(documents),
        warnings=[],
    )


def test_complete_summary_creates_verified_single_document_package(tmp_path):
    selected = filing("selected", "selected-url", "2025-12-22")
    package_builder, edgar = builder(tmp_path, {"selected-url": SUMMARY})

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.document_verification is DocumentVerification.VERIFIED
    assert len(result.documents) == 1
    assert result.documents[0].role is DocumentRole.PRIMARY_PROSPECTUS
    assert result.path == result.documents[0].path
    assert result.documents[0].size_bytes == len(SUMMARY)
    assert result.documents[0].sha256 == hashlib.sha256(SUMMARY).hexdigest()
    assert result.documents[0].source_url == "selected-url"
    assert result.documents[0].archive_index_url == "selected-url/index.json"
    assert result.manifest_path.endswith("VUSXX/manifest.json")
    edgar.related_prospectus_refs.assert_not_called()


def test_supplement_package_includes_date_linked_verified_base(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    base = filing("base", "base-url", "2025-12-19")
    package_builder, edgar = builder(
        tmp_path,
        {"supplement-url": SUPPLEMENT, "base-url": QQQ_BASE},
        [base],
    )

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.document_verification is DocumentVerification.VERIFIED
    assert [document.role for document in result.documents] == [
        DocumentRole.SUPPLEMENT,
        DocumentRole.BASE_PROSPECTUS,
    ]
    assert "2025-12-22" in result.document_evidence[-1]
    assert not any("requires a verified base" in warning for warning in result.warnings)
    edgar.related_prospectus_refs.assert_called_once_with(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"),
        selected,
        ["2025-12-22"],
    )

    with open(result.manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["schema_version"] == 2
    assert manifest["identity"]["level"] == "class"
    assert manifest["identity"]["registrant_cik"] == 1
    assert manifest["identity"]["series_id"] == "S1"
    assert manifest["identity"]["class_id"] == "C1"
    assert manifest["identity"]["mapping_source"] == "mf"
    assert manifest["package"]["verification"] == "document_verified"
    assert manifest["package"]["validation_policy"] == "legacy"
    assert manifest["package"]["validation_policy_version"] == "legacy"
    assert manifest["documents"][0]["source_url"] == "supplement-url"
    assert manifest["documents"][0]["archive_index_url"] == "supplement-url/index.json"
    assert len(manifest["documents"][0]["sha256"]) == 64
    assert [item["role"] for item in manifest["documents"]] == [
        "supplement",
        "base_prospectus",
    ]


def test_supplement_without_base_is_saved_for_manual_review(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    package_builder, _ = builder(tmp_path, {"supplement-url": SUPPLEMENT})

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    assert result.status == "ok"
    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert len(result.documents) == 1
    assert any("no verified complete base" in warning for warning in result.warnings)


def test_failed_candidate_does_not_prevent_later_verified_base(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    broken = filing("broken", "missing-url", "2026-01-01")
    base = filing("base", "base-url", "2025-12-19")
    package_builder, _ = builder(
        tmp_path,
        {"supplement-url": SUPPLEMENT, "base-url": QQQ_BASE},
        [broken, base],
    )

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_verification is DocumentVerification.VERIFIED
    assert [document.accession for document in result.documents] == [
        "supplement",
        "base",
    ]
    assert any("broken" in warning for warning in result.warnings)


def test_candidate_discovery_failure_produces_reviewable_manifest(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    package_builder, edgar = builder(tmp_path, {"supplement-url": SUPPLEMENT})
    edgar.related_prospectus_refs.side_effect = RuntimeError("feed unavailable")

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    assert result.status == "ok"
    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.manifest_path
    assert any("feed unavailable" in warning for warning in result.warnings)


def test_base_without_matching_referenced_date_requires_review(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    unrelated_date_base = QQQ_BASE.replace(b"December 22, 2025", b"November 1, 2025")
    base = filing("base", "base-url", "2025-11-01")
    package_builder, _ = builder(
        tmp_path,
        {"supplement-url": SUPPLEMENT, "base-url": unrelated_date_base},
        [base],
    )

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    assert len(result.documents) == 2
    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert any("lacks a matching referenced date" in warning for warning in result.warnings)


def test_one_verified_sibling_replaces_unverified_primary(tmp_path):
    selected = filing("selected", "primary.htm", "2026-01-01")
    package_builder, edgar = builder(
        tmp_path,
        {"primary.htm": UNKNOWN_PRIMARY, "sibling.htm": SUMMARY},
    )
    edgar.accession_document_inventory.return_value = archive_inventory(
        ArchiveDocumentRef("primary.htm", "primary.htm", len(UNKNOWN_PRIMARY), eligible=True),
        ArchiveDocumentRef("sibling.htm", "sibling.htm", len(SUMMARY), eligible=True),
        ArchiveDocumentRef(
            "ex99.htm",
            "ex99.htm",
            100,
            eligible=False,
            exclusion_reason="SEC document type EX-99 is an exhibit",
        ),
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_verification is DocumentVerification.VERIFIED
    assert result.documents[0].source_url == "sibling.htm"
    assert open(result.path, "rb").read() == SUMMARY
    selected_candidates = [
        candidate
        for candidate in result.candidate_evaluations
        if candidate.disposition is CandidateDisposition.SELECTED
    ]
    assert [candidate.name for candidate in selected_candidates] == ["sibling.htm"]
    assert result.candidate_searches[0].complete is True
    assert result.candidate_searches[0].purpose is CandidatePurpose.PRIMARY_RECOVERY

    with open(result.manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["recovery"]["candidates"][1]["sha256"]
    assert manifest["recovery"]["candidates"][2]["disposition"] == "excluded"


def test_no_verified_sibling_keeps_primary_and_requires_review(tmp_path):
    selected = filing("selected", "primary.htm", "2026-01-01")
    package_builder, edgar = builder(tmp_path, {"primary.htm": UNKNOWN_PRIMARY})
    edgar.accession_document_inventory.return_value = archive_inventory(
        ArchiveDocumentRef("primary.htm", "primary.htm", len(UNKNOWN_PRIMARY), eligible=True),
        ArchiveDocumentRef(
            "ex99.htm",
            "ex99.htm",
            100,
            eligible=False,
            exclusion_reason="SEC document type EX-99 is an exhibit",
        ),
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.documents[0].source_url == "primary.htm"
    assert any("no accession sibling passed" in warning for warning in result.warnings)


def test_multiple_verified_siblings_do_not_break_ambiguity_tie(tmp_path):
    selected = filing("selected", "primary.htm", "2026-01-01")
    package_builder, edgar = builder(
        tmp_path,
        {
            "primary.htm": UNKNOWN_PRIMARY,
            "first.htm": SUMMARY,
            "second.htm": SUMMARY,
        },
    )
    edgar.accession_document_inventory.return_value = archive_inventory(
        ArchiveDocumentRef("primary.htm", "primary.htm", len(UNKNOWN_PRIMARY), eligible=True),
        ArchiveDocumentRef("first.htm", "first.htm", len(SUMMARY), eligible=True),
        ArchiveDocumentRef("second.htm", "second.htm", len(SUMMARY), eligible=True),
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.documents[0].source_url == "primary.htm"
    assert not any(
        candidate.disposition is CandidateDisposition.SELECTED
        for candidate in result.candidate_evaluations
    )
    assert sum(
        candidate.disposition is CandidateDisposition.QUALIFIED
        for candidate in result.candidate_evaluations
    ) == 2
    assert any("2 accession siblings" in warning for warning in result.warnings)


def test_one_qualified_sibling_is_not_selected_when_another_cannot_be_evaluated(
    tmp_path,
):
    selected = filing("selected", "primary.htm", "2026-01-01")
    package_builder, edgar = builder(
        tmp_path,
        {"primary.htm": UNKNOWN_PRIMARY, "verified.htm": SUMMARY},
    )
    edgar.accession_document_inventory.return_value = archive_inventory(
        ArchiveDocumentRef("primary.htm", "primary.htm", len(UNKNOWN_PRIMARY), eligible=True),
        ArchiveDocumentRef("verified.htm", "verified.htm", len(SUMMARY), eligible=True),
        ArchiveDocumentRef("unavailable.htm", "unavailable.htm", 500, eligible=True),
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"), selected
    )

    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.documents[0].source_url == "primary.htm"
    assert not any(
        candidate.disposition is CandidateDisposition.SELECTED
        for candidate in result.candidate_evaluations
    )
    assert any(
        candidate.disposition is CandidateDisposition.ERROR
        for candidate in result.candidate_evaluations
    )
    assert any("uniqueness is unproven" in warning for warning in result.warnings)


def test_base_search_stops_downloading_after_verified_date_match(tmp_path):
    selected = filing("supplement", "supplement-url", "2026-06-10")
    first = filing("first", "first-url", "2025-12-21")
    matching = filing("matching", "matching-url", "2025-12-19")
    unevaluated = filing("unevaluated", "unevaluated-url", "2025-01-01")
    package_builder, _ = builder(
        tmp_path,
        {
            "supplement-url": SUPPLEMENT,
            "first-url": QQQ_BASE.replace(b"December 22, 2025", b"December 21, 2025"),
            "matching-url": QQQ_BASE,
            "unevaluated-url": QQQ_BASE,
        },
        [first, matching, unevaluated],
    )

    result = package_builder.build(
        ResolvedFund("QQQ", 1, "S1", "C1", "mf"), selected
    )

    requested_urls = [
        call.args[0] for call in package_builder.downloader.client.get_bytes.call_args_list
    ]
    assert "unevaluated-url" not in requested_urls
    assert result.candidate_searches[0].discovered_count == 3
    assert result.candidate_searches[0].evaluated_count == 2
    assert result.candidate_searches[0].stop_reason == "verified date-linked base found"


def test_cik_only_document_can_be_verified_without_upgrading_identity(tmp_path):
    selected = filing(
        "spy", "spy-url", "2026-01-26", identity=IdentityLevel.REGISTRANT
    )
    selected.form = "485BPOS"
    package_builder, _ = builder(tmp_path, {"spy-url": SPY_PROSPECTUS})

    result = package_builder.build(
        ResolvedFund("SPY", 884394, source="ticker_txt"), selected
    )

    assert result.identity_level is IdentityLevel.REGISTRANT
    assert result.document_kind is DocumentKind.STATUTORY_PROSPECTUS
    assert result.document_verification is DocumentVerification.VERIFIED
    assert "exact ticker SPY" in result.document_evidence[0]


def test_v7_feature_flag_controls_package_and_records_policy(tmp_path):
    selected = filing("selected", "selected-url", "2026-01-01")
    client = Mock()
    client.get_bytes.return_value = V7_FUND_SUMMARY
    downloader = Downloader(client, output_dir=str(tmp_path))
    edgar = Mock()
    edgar.resolver.series_for_cik.return_value = {"S1"}
    edgar.filing_identity_metadata.return_value = FilingIdentityMetadata(
        accession="selected",
        registrant_name="Example Trust",
        registrant_cik=1,
        series=[
            SeriesIdentity(
                series_id="S1",
                name="Example Fund",
                owner_cik=1,
                classes=[ClassIdentity("C1", "Admiral Shares", "VUSXX")],
            )
        ],
    )
    package_builder = DocumentPackageBuilder(
        edgar,
        downloader,
        validation_policy="v7",
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"),
        selected,
    )

    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.document_verification is DocumentVerification.VERIFIED
    edgar.accession_document_inventory.assert_not_called()
    with open(result.manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["package"]["validation_policy"] == "v7"
    assert manifest["package"]["validation_policy_version"] == "m6.3-shadow-v7"


def test_v7_metadata_failure_fails_closed_to_review(tmp_path):
    selected = filing("selected", "selected-url", "2026-01-01")
    client = Mock()
    client.get_bytes.return_value = SUMMARY
    downloader = Downloader(client, output_dir=str(tmp_path))
    edgar = Mock()
    edgar.resolver.series_for_cik.return_value = {"S1"}
    edgar.filing_identity_metadata.side_effect = FilingIdentityParseError(
        "metadata unavailable"
    )
    edgar.accession_document_inventory.return_value = archive_inventory()
    package_builder = DocumentPackageBuilder(
        edgar,
        downloader,
        validation_policy="v7",
    )

    result = package_builder.build(
        ResolvedFund("VUSXX", 1, "S1", "C1", "mf"),
        selected,
    )

    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert any("metadata unavailable" in warning for warning in result.warnings)
    edgar.accession_document_inventory.assert_not_called()


def test_v7_programming_error_is_not_hidden_as_manual_review(tmp_path):
    selected = filing("selected", "selected-url", "2026-01-01")
    client = Mock()
    client.get_bytes.return_value = SUMMARY
    downloader = Downloader(client, output_dir=str(tmp_path))
    edgar = Mock()
    edgar.resolver.series_for_cik.return_value = {"S1"}
    edgar.filing_identity_metadata.return_value = FilingIdentityMetadata(
        accession="selected",
        registrant_name="Example Trust",
    )
    package_builder = DocumentPackageBuilder(
        edgar,
        downloader,
        validation_policy="v7",
    )
    package_builder.evidence_policy = Mock(
        evaluate=Mock(side_effect=RuntimeError("policy defect"))
    )

    with pytest.raises(RuntimeError, match="policy defect"):
        package_builder.build(
            ResolvedFund("VUSXX", 1, "S1", "C1", "mf"),
            selected,
        )
