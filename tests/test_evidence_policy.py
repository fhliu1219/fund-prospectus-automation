"""Tests for location-aware shadow evidence and explicit contradictions."""

from prospectus_fetcher.corpus import AutomaticUseLabel, RelevanceLabel
from prospectus_fetcher.evidence_policy import ShadowEvidencePolicy
from prospectus_fetcher.filing_identity import parse_filing_identity_header
from prospectus_fetcher.models import DocumentKind


def metadata(ticker="EXMXX", class_id="C000000001"):
    return parse_filing_identity_header(
        f"""
        <!--
        <ACCESSION-NUMBER>0000000001-26-000001
        <CONFORMED-NAME>EXAMPLE TRUST
        <SERIES>
        <OWNER-CIK>0000000001
        <SERIES-ID>S000000001
        <SERIES-NAME>Example Treasury Fund
        <CLASS-CONTRACT>
        <CLASS-CONTRACT-ID>{class_id}
        <CLASS-CONTRACT-NAME>Investor Shares
        <CLASS-CONTRACT-TICKER-SYMBOL>{ticker}
        </CLASS-CONTRACT>
        </SERIES>
        -->
        """
    )


def html(body):
    return f"<html><body>{body}</body></html>".encode()


def complete(body):
    return html(
        f"<h1>Summary Prospectus</h1>{body}"
        "<h2>Investment Objective</h2><h2>Fees and Expenses</h2>"
    )


def test_shadow_requires_location_aware_document_and_metadata_signals():
    content = complete(
        "<table><tr><th>Class</th><th>Ticker</th></tr>"
        "<tr><td>Investor Shares</td><td>EXMXX</td></tr></table>"
        "<p>Example Treasury Fund</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.automatic_use is AutomaticUseLabel.ALLOWED
    assert {signal.code for signal in result.signals} >= {
        "metadata_class_match",
        "metadata_ticker_match",
        "ticker_in_table",
        "series_name_match",
    }


def test_late_incidental_ticker_remains_ambiguous_not_positive():
    content = html(
        "<h1>Summary Prospectus</h1>"
        "<p>Another fund is the subject of this document.</p>"
        "<h2>Investment Objective</h2><h2>Fees and Expenses</h2>"
        "<h2>Additional Information</h2>"
        "<p>The comparison benchmark includes EXMXX.</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert result.automatic_use is AutomaticUseLabel.REVIEW
    assert any(signal.code == "ticker_in_body_incidental" for signal in result.signals)


def test_absent_requested_class_in_authoritative_header_is_negative():
    content = complete("<h1>EXMXX</h1><p>Example Treasury Fund</p>")

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        "C000000999",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.NEGATIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert "does not include requested class ID" in result.contradictions[0]


def test_metadata_ticker_mismatch_is_explicit_contradiction():
    result = ShadowEvidencePolicy().evaluate(
        complete("<h1>EXMXX</h1><p>Example Treasury Fund</p>"),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(ticker="OTHER"),
    )

    assert result.relevance is RelevanceLabel.NEGATIVE
    assert any("maps C000000001 to OTHER" in value for value in result.contradictions)


def test_relevant_supplement_is_positive_but_disallowed_standalone():
    content = html(
        "<h1>Supplement dated July 1, 2026</h1>"
        "<table><tr><td>Investor Shares</td><td>EXMXX</td></tr></table>"
        "<p>Example Treasury Fund</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def test_ambiguous_supplement_routes_to_review():
    result = ShadowEvidencePolicy().evaluate(
        html("<h1>Supplement dated July 1, 2026</h1><p>General update.</p>"),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.automatic_use is AutomaticUseLabel.REVIEW


def test_generic_prospectus_disclaimer_is_not_an_exclusion():
    content = html(
        "<h1>Statutory Prospectus</h1><p>SPDR S&P 500 ETF Trust (SPY)</p>"
        "<p>This prospectus does not include all information about the Trust.</p>"
        "<h2>Investment Objective</h2><h2>Fees and Expenses</h2>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "SPY", None, None, parse_filing_identity_header(
            "<!--<ACCESSION-NUMBER>0000000001-26-000001"
            "\n<CONFORMED-NAME>SPDR S&P 500 ETF TRUST-->"
        )
    )

    assert not result.contradictions
