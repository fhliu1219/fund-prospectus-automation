"""Tests for the versioned corpus schema and checksum cache."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from prospectus_fetcher.corpus import (
    CorpusCache,
    CorpusSchemaError,
    RelevanceLabel,
    load_manifest,
    parse_manifest,
)


DOCUMENT = b"<html><body>Summary Prospectus EXMXX</body></html>"
HEADER = b"<!--<ACCESSION-NUMBER>0000000001-26-000001\n<CONFORMED-NAME>EXAMPLE-->"
DETAIL = b"<html><title>EDGAR Filing Documents for 0000000001-26-000001</title></html>"


def payload():
    base = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001"
    return {
        "schema_version": 1,
        "corpus_version": "2026-07-16",
        "target_case_count": 1,
        "cases": [
            {
                "case_id": "example-positive",
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
                    "summary": "Cover and class table identify the requested class.",
                    "category": "direct_class_evidence",
                    "observed_evidence": ["Ticker appears on the cover."],
                    "missing_evidence": [],
                    "contradictory_evidence": [],
                    "resolution_needed": None,
                },
            }
        ],
    }


def payload_v2():
    value = payload()
    value["schema_version"] = 2
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


def test_manifest_parses_independent_label_axes():
    manifest = parse_manifest(payload())

    assert manifest.target_case_count == 1
    assert manifest.cases[0].labels.relevance is RelevanceLabel.POSITIVE
    assert manifest.cases[0].class_id == "C000000001"


def test_manifest_v2_parses_scope_and_content_profile():
    manifest = parse_manifest(payload_v2())

    assert manifest.schema_version == 2
    assert manifest.cases[0].labels.document_scope.value == "ticker_specific"
    assert manifest.cases[0].labels.content_profile.contains_summary_prospectus


def test_manifest_v2_requires_scope_and_content_profile():
    value = payload()
    value["schema_version"] = 2

    with pytest.raises(CorpusSchemaError, match="content_profile"):
        parse_manifest(value)


def test_manifest_v2_rejects_incoherent_combined_package():
    value = payload_v2()
    value["cases"][0]["labels"]["document_kind"] = (
        "combined_prospectus_package"
    )

    with pytest.raises(CorpusSchemaError, match="combined package requires"):
        parse_manifest(value)


def test_manifest_v2_rejects_allowed_incomplete_content():
    value = payload_v2()
    value["cases"][0]["labels"]["document_kind"] = "unknown"
    value["cases"][0]["labels"]["content_profile"].update(
        {
            "contains_summary_prospectus": False,
            "contains_statutory_prospectus": False,
        }
    )

    with pytest.raises(CorpusSchemaError, match="allowed use requires"):
        parse_manifest(value)


def test_manifest_requires_reasoned_ambiguity():
    value = payload()
    value["cases"][0]["labels"]["relevance"] = "ambiguous"
    value["cases"][0]["labels"]["automatic_use"] = "review"

    with pytest.raises(CorpusSchemaError, match="ambiguous case requires"):
        parse_manifest(value)


def test_manifest_requires_observed_evidence_for_every_label():
    value = payload()
    value["cases"][0]["reason"]["observed_evidence"] = []

    with pytest.raises(CorpusSchemaError, match="observed_evidence must contain"):
        parse_manifest(value)


def test_manifest_rejects_sai_as_automatically_allowed():
    value = payload()
    value["cases"][0]["labels"]["document_kind"] = (
        "statement_of_additional_information"
    )

    with pytest.raises(CorpusSchemaError, match="cannot be allowed"):
        parse_manifest(value)


def test_manifest_rejects_document_outside_declared_accession():
    value = payload()
    value["cases"][0]["document_url"] = (
        "https://www.sec.gov/Archives/edgar/data/1/other/primary.htm"
    )

    with pytest.raises(CorpusSchemaError, match="expected SEC archive URL"):
        parse_manifest(value)


def test_manifest_rejects_invalid_filing_date():
    value = payload()
    value["cases"][0]["filing_date"] = "2026-02-30"

    with pytest.raises(CorpusSchemaError, match="invalid calendar date"):
        parse_manifest(value)


def test_manifest_rejects_negative_known_series_count():
    value = payload()
    value["cases"][0]["known_series_count"] = -1

    with pytest.raises(CorpusSchemaError, match="known_series_count"):
        parse_manifest(value)


def test_manifest_requires_complete_optional_filing_detail_resource():
    value = payload()
    value["cases"][0]["filing_detail_url"] = (
        "https://www.sec.gov/Archives/edgar/data/1/"
        "000000000126000001/0000000001-26-000001-index.html"
    )

    with pytest.raises(CorpusSchemaError, match="must appear together"):
        parse_manifest(value)


def test_manifest_rejects_duplicate_case_ids():
    value = payload()
    value["target_case_count"] = 2
    value["cases"].append(deepcopy(value["cases"][0]))

    with pytest.raises(CorpusSchemaError, match="duplicate"):
        parse_manifest(value)


def test_cache_fetches_once_and_reverifies_local_bytes(tmp_path):
    case = parse_manifest(payload()).cases[0]
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, HEADER]
    cache = CorpusCache(client, tmp_path)

    first = cache.read_case(case)
    second = cache.read_case(case)

    assert first == (DOCUMENT, HEADER.decode(), None)
    assert second == first
    assert client.get_bytes.call_count == 2


def test_cache_fetches_optional_filing_detail_once(tmp_path):
    value = payload()
    base = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001"
    value["cases"][0]["filing_detail_url"] = (
        base + "/0000000001-26-000001-index.html"
    )
    value["cases"][0]["filing_detail_sha256"] = hashlib.sha256(DETAIL).hexdigest()
    case = parse_manifest(value).cases[0]
    client = Mock()
    client.get_bytes.side_effect = [DOCUMENT, HEADER, DETAIL]
    cache = CorpusCache(client, tmp_path)

    first = cache.read_case(case)
    second = cache.read_case(case)

    assert first == (DOCUMENT, HEADER.decode(), DETAIL.decode())
    assert second == first
    assert client.get_bytes.call_count == 3


def test_cache_rejects_download_checksum_mismatch(tmp_path):
    case = parse_manifest(payload()).cases[0]
    client = Mock()
    client.get_bytes.return_value = b"changed"

    with pytest.raises(CorpusSchemaError, match="checksum mismatch"):
        CorpusCache(client, tmp_path).ensure_case(case)


def test_committed_holdout_is_disjoint_from_development_corpus():
    corpus_dir = Path(__file__).resolve().parents[1] / "corpus"
    development = load_manifest(corpus_dir / "manifest.json")
    holdout = load_manifest(corpus_dir / "holdout_manifest.json")

    assert len(development.cases) == 30
    assert len(holdout.cases) == 30
    assert {case.accession for case in development.cases}.isdisjoint(
        case.accession for case in holdout.cases
    )
    assert {case.document_sha256 for case in development.cases}.isdisjoint(
        case.document_sha256 for case in holdout.cases
    )


def test_v5_followup_is_disjoint_from_all_prior_corpora():
    corpus_dir = Path(__file__).resolve().parents[1] / "corpus"
    development = load_manifest(corpus_dir / "manifest.json")
    holdout = load_manifest(corpus_dir / "holdout_manifest.json")
    followup = load_manifest(corpus_dir / "v5_followup_manifest.json")

    assert len(followup.cases) == 30
    assert len({case.accession for case in followup.cases}) == 30
    assert len({case.document_sha256 for case in followup.cases}) == 30
    for prior in (development, holdout):
        assert {case.accession for case in followup.cases}.isdisjoint(
            case.accession for case in prior.cases
        )
        assert {case.document_sha256 for case in followup.cases}.isdisjoint(
            case.document_sha256 for case in prior.cases
        )


def test_v6_challenge_is_disjoint_from_all_prior_corpora():
    corpus_dir = Path(__file__).resolve().parents[1] / "corpus"
    challenge = load_manifest(corpus_dir / "v6_challenge_manifest.json")
    prior_corpora = (
        load_manifest(corpus_dir / "manifest.json"),
        load_manifest(corpus_dir / "holdout_manifest.json"),
        load_manifest(corpus_dir / "v5_followup_manifest.json"),
    )

    assert len(challenge.cases) == 30
    assert len({case.accession for case in challenge.cases}) == 30
    assert len({case.document_sha256 for case in challenge.cases}) == 30
    for prior in prior_corpora:
        assert {case.accession for case in challenge.cases}.isdisjoint(
            case.accession for case in prior.cases
        )
        assert {case.document_sha256 for case in challenge.cases}.isdisjoint(
            case.document_sha256 for case in prior.cases
        )


def test_v6_representative_is_disjoint_from_all_prior_corpora():
    corpus_dir = Path(__file__).resolve().parents[1] / "corpus"
    representative = load_manifest(corpus_dir / "v6_representative_manifest.json")
    prior_corpora = (
        load_manifest(corpus_dir / "manifest.json"),
        load_manifest(corpus_dir / "holdout_manifest.json"),
        load_manifest(corpus_dir / "v5_followup_manifest.json"),
        load_manifest(corpus_dir / "v6_challenge_manifest.json"),
    )

    assert len(representative.cases) == 50
    assert len({case.accession for case in representative.cases}) == 50
    assert len({case.document_sha256 for case in representative.cases}) == 50
    for prior in prior_corpora:
        assert {case.accession for case in representative.cases}.isdisjoint(
            case.accession for case in prior.cases
        )
        assert {case.document_sha256 for case in representative.cases}.isdisjoint(
            case.document_sha256 for case in prior.cases
        )


def test_v6_representative_provenance_matches_manifest():
    corpus_dir = Path(__file__).resolve().parents[1] / "corpus"
    representative = load_manifest(corpus_dir / "v6_representative_manifest.json")
    with open(
        corpus_dir / "v6_representative_provenance.json",
        encoding="utf-8",
    ) as handle:
        provenance = json.load(handle)

    mutual_funds = provenance["mutual_fund_sampling"]["accepted_tickers"]
    cik_only = provenance["cik_only_sampling"]["accepted_tickers"]
    assert provenance["policy_blind_collection"] is True
    assert len(mutual_funds) == 45
    assert len(cik_only) == 5
    assert set(mutual_funds).isdisjoint(cik_only)
    assert set(mutual_funds + cik_only) == {
        case.ticker for case in representative.cases
    }
