"""Tests for corpus comparison reports and safety-first metrics."""

import hashlib
import json
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


def test_report_compares_current_and_shadow_policies(tmp_path):
    manifest = parse_manifest(manifest_payload())
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, HEADER]
    report = CorpusEvaluator(CorpusCache(client, tmp_path / "cache")).evaluate(manifest)

    assert report["current_metrics"]["case_count"] == 1
    assert report["shadow_metrics"]["false_positive_verification_count"] == 0
    assert report["shadow_metrics"]["positive_precision"] == 1.0
    assert report["cases"][0]["disagreements"]["shadow"] == []

    path = tmp_path / "reports" / "evaluation.json"
    save_report(report, path)
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["shadow_policy_version"] == "m6-shadow-v1"
