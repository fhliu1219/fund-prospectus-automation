"""Tests for corpus comparison reports and safety-first metrics."""

import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

from prospectus_fetcher.corpus import CorpusCache, parse_manifest
from prospectus_fetcher.corpus_evaluator import CorpusEvaluator, save_report


DOCUMENT = b"""
<html><body><h1>Summary Prospectus</h1>
<table><tr><td>Investor Shares</td><td>EXMXX</td></tr></table>
<p>Example Treasury Fund</p>
<h2>Investment Objective</h2><h2>Fees and Expenses</h2>
</body></html>
"""
HEADER = b"""
<!--
<ACCESSION-NUMBER>0000000001-26-000001
<CONFORMED-NAME>EXAMPLE TRUST
<SERIES><OWNER-CIK>0000000001
<SERIES-ID>S000000001
<SERIES-NAME>Example Treasury Fund
<CLASS-CONTRACT><CLASS-CONTRACT-ID>C000000001
<CLASS-CONTRACT-NAME>Investor Shares
<CLASS-CONTRACT-TICKER-SYMBOL>EXMXX
</CLASS-CONTRACT></SERIES>
-->
"""
DETAIL = b"""
<html>
<head><title>EDGAR Filing Documents for 0000000001-26-000001</title></head>
<body>
<table class="tableSeries" summary="Series and Classes/Contracts Table">
<tr><td class="CIKname">CIK
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=0000000001">0000000001</a>
</td><td></td><td></td><td></td></tr>
<tr><td class="seriesName">Series
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=S000000001">S000000001</a>
</td><td></td><td>Example Treasury Fund</td><td></td></tr>
<tr><td class="classContract">Class/Contract
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=C000000001">C000000001</a>
</td><td></td><td>Investor Shares</td><td>EXMXX</td></tr>
</table>
<span class="companyName">EXAMPLE TRUST (Filer)
<a href="/cgi-bin/browse-edgar?CIK=0000000001&action=getcompany">0000000001</a>
</span>
</body>
</html>
"""


def manifest_payload():
    base = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001"
    return {
        "schema_version": 1,
        "corpus_version": "test-v1",
        "target_case_count": 1,
        "cases": [
            {
                "case_id": "verified-summary",
                "provider": "Example",
                "ticker": "EXMXX",
                "registrant_cik": 1,
                "requested_cik": 1,
                "series_id": "S000000001",
                "class_id": "C000000001",
                "accession": "0000000001-26-000001",
                "form": "497K",
                "filing_date": "2026-01-01",
                "document_url": base + "/primary.htm",
                "document_sha256": hashlib.sha256(DOCUMENT).hexdigest(),
                "filing_header_url": base + "/0000000001-26-000001-index-headers.html",
                "filing_header_sha256": hashlib.sha256(HEADER).hexdigest(),
                "labels": {
                    "relevance": "positive",
                    "document_kind": "summary_prospectus",
                    "automatic_use": "allowed",
                },
                "reason": {
                    "summary": "Direct table and metadata evidence.",
                    "category": "direct_class_evidence",
                    "observed_evidence": ["Ticker appears in the class table."],
                    "missing_evidence": [],
                    "contradictory_evidence": [],
                    "resolution_needed": None,
                },
            }
        ],
    }


def manifest_payload_v2():
    value = manifest_payload()
    value["schema_version"] = 2
    value["corpus_version"] = "test-v2"
    value["cases"][0]["labels"].update(
        {
            "document_scope": "ticker_specific",
            "content_profile": {
                "contains_summary_prospectus": True,
                "contains_statutory_prospectus": False,
                "contains_sai": False,
                "is_supplement": False,
            },
        }
    )
    return value


def test_report_compares_current_and_shadow_policies(tmp_path):
    manifest = parse_manifest(manifest_payload())
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, HEADER]
    report = CorpusEvaluator(CorpusCache(client, tmp_path / "cache")).evaluate(manifest)

    assert report["current_metrics"]["case_count"] == 1
    assert report["shadow_metrics"]["false_positive_verification_count"] == 0
    assert report["shadow_metrics"]["positive_precision"] == 1.0
    assert report["cases"][0]["disagreements"]["shadow"] == []
    assert report["schema_version"] == 3
    assert report["cases"][0]["scenario"]["requested_identity_scope"] == "class"
    assert (
        report["cases"][0]["scenario"]["filing_metadata_breadth"]
        == "single_series_single_class"
    )
    assert report["cases"][0]["scenario"]["document_encoding"] == "html"
    assert report["shadow_slices"]["form"]["497K"]["case_count"] == 1
    assert (
        report["shadow_slices"]["requested_identity_scope"]["class"][
            "false_positive_verification_count"
        ]
        == 0
    )

    path = tmp_path / "reports" / "evaluation.json"
    save_report(report, path)
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["shadow_policy_version"] == "m6.2-shadow-v6"


def test_report_v2_measures_scope_and_content_characteristics(tmp_path):
    manifest = parse_manifest(manifest_payload_v2())
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, HEADER]

    report = CorpusEvaluator(CorpusCache(client, tmp_path / "cache")).evaluate(
        manifest
    )

    row = report["cases"][0]
    assert row["expected"]["document_scope"] == "ticker_specific"
    assert row["shadow"]["document_scope"] == "ticker_specific"
    assert row["shadow"]["content_profile"] == row["expected"]["content_profile"]
    assert row["disagreements"]["shadow"] == []
    assert (
        report["shadow_metrics"]["document_scope_confusion_matrix"]
        ["ticker_specific"]["ticker_specific"]
        == 1
    )
    assert (
        report["shadow_metrics"]["content_profile_confusion_matrices"]
        ["contains_summary_prospectus"]["true"]["true"]
        == 1
    )


def test_report_uses_filing_detail_when_header_is_technically_invalid(tmp_path):
    value = deepcopy(manifest_payload())
    base = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001"
    invalid_header = b"<html>submission header unavailable</html>"
    case = value["cases"][0]
    case["filing_header_sha256"] = hashlib.sha256(invalid_header).hexdigest()
    case["filing_detail_url"] = base + "/0000000001-26-000001-index.html"
    case["filing_detail_sha256"] = hashlib.sha256(DETAIL).hexdigest()
    manifest = parse_manifest(value)
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, invalid_header, DETAIL]

    report = CorpusEvaluator(CorpusCache(client, tmp_path / "cache")).evaluate(
        manifest
    )

    assert (
        report["cases"][0]["identity_metadata_source"]
        == "filing_detail_fallback"
    )
    assert report["cases"][0]["disagreements"]["shadow"] == []
