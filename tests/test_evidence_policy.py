"""Tests for location-aware shadow evidence and explicit contradictions."""

from prospectus_fetcher.corpus import AutomaticUseLabel, RelevanceLabel
from prospectus_fetcher.evidence_policy import (
    ShadowCandidate,
    ShadowEvidencePolicy,
    ShadowSelectionStatus,
    select_shadow_candidate,
)
from prospectus_fetcher.filing_identity import parse_filing_identity_header
from prospectus_fetcher.models import DocumentKind, DocumentScope


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


def test_supplement_to_exact_series_name_is_positive_without_ticker():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Supplement to the Example&reg; Treasury Fund Summary Prospectus</h1>"
            "<p>Portfolio management information is replaced.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any(
        signal.code == "supplement_series_subject" for signal in result.signals
    )


def test_supplement_fund_list_can_establish_exact_series_subject():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Supplement dated July 1, 2026</h1>"
            "<table><tr><td>Other Fund</td><td>Example Treasury Fund</td></tr></table>"
            "<p>Management information is replaced.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any(
        signal.code == "supplement_series_subject"
        and signal.location.value == "class_table"
        for signal in result.signals
    )


def test_universal_supplement_scope_requires_matching_registrant():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>EXAMPLE TRUST</h1>"
            "<p>Supplement dated July 1, 2026 to the SAI for all series.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any(
        signal.code == "supplement_universal_registrant_scope"
        for signal in result.signals
    )


def test_universal_scope_without_matching_registrant_remains_ambiguous():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>UNRELATED TRUST</h1>"
            "<p>Supplement dated July 1, 2026 to the SAI for all series.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert result.automatic_use is AutomaticUseLabel.REVIEW


def test_universal_scope_cannot_override_metadata_class_contradiction():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>EXAMPLE TRUST</h1>"
            "<p>Supplement dated July 1, 2026 to the SAI for all series.</p>"
        ),
        "EXMXX",
        "C000000999",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.NEGATIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


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


def test_sai_is_relevant_but_disallowed_as_a_prospectus():
    content = html(
        "<h1>Statement of Additional Information</h1>"
        "<table><tr><td>Investor Shares</td><td>EXMXX</td></tr></table>"
        "<p>Example Treasury Fund</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert (
        result.document_kind
        is DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION
    )
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def test_summary_remains_summary_when_it_references_its_sai():
    content = complete(
        "<p>Example Treasury Fund (EXMXX)</p>"
        "<p>The Statement of Additional Information is incorporated by reference.</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS


def test_cik_only_registrant_instrument_match_accepts_late_ticker():
    content = html(
        "<h1>SPDR S&P 500 ETF Trust</h1>"
        "<p>Statutory Prospectus</p>"
        "<h2>Fees and Expenses</h2><p>" + ("registration material " * 1000) + "</p>"
        "<p>The units trade under ticker SPY.</p>"
        "<h2>Investment Objective</h2>"
    )
    filing_metadata = parse_filing_identity_header(
        "<!--<ACCESSION-NUMBER>0000000001-26-000001"
        "\n<CONFORMED-NAME>SPDR S&P 500 ETF TRUST-->"
    )

    result = ShadowEvidencePolicy().evaluate(
        content,
        "SPY",
        None,
        None,
        filing_metadata,
        requested_cik=884394,
        registrant_cik=884394,
        known_series_count=0,
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.automatic_use is AutomaticUseLabel.ALLOWED
    assert any(
        signal.code == "registrant_instrument_match" for signal in result.signals
    )


def test_cik_only_match_stays_ambiguous_for_known_multi_series_registrant():
    content = html(
        "<h1>Example Trust</h1><p>Statutory Prospectus</p>"
        "<h2>Fees and Expenses</h2><p>EXMXX</p>"
        "<h2>Investment Objective</h2>"
    )
    filing_metadata = parse_filing_identity_header(
        "<!--<ACCESSION-NUMBER>0000000001-26-000001"
        "\n<CONFORMED-NAME>EXAMPLE TRUST-->"
    )

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        None,
        None,
        filing_metadata,
        requested_cik=1,
        registrant_cik=1,
        known_series_count=2,
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert result.automatic_use is AutomaticUseLabel.REVIEW


def test_inline_xbrl_uses_visible_content_and_structured_metadata():
    content = b"""
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
      <head><title>Prospectus - Investment Objective</title></head>
      <body>
        <div style="display: none">
          <ix:header><ix:hidden>
            <ix:nonNumeric name="dei:DocumentType">485BPOS</ix:nonNumeric>
          </ix:hidden></ix:header>
        </div>
        <table><tr><td>Fund /Ticker</td><td>Example Treasury Fund /EXMXX</td></tr></table>
        <div>Investment Objective</div>
        <div>Fees and Expenses</div>
        <div>Principal Investment Strategies</div>
        <div>Principal Risks</div>
      </body>
    </html>
    """

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="485BPOS",
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_kind is DocumentKind.STATUTORY_PROSPECTUS
    assert result.automatic_use is AutomaticUseLabel.ALLOWED
    assert {signal.code for signal in result.signals} >= {
        "xbrl_document_type",
        "prospectus_html_title",
        "declared_filing_form",
    }


def test_hidden_inline_xbrl_ticker_is_not_visible_identity_evidence():
    content = b"""
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
      <head><title>Prospectus</title></head>
      <body>
        <ix:header><ix:hidden>
          <ix:nonNumeric name="example:Ticker">EXMXX</ix:nonNumeric>
        </ix:hidden></ix:header>
        <p>Another fund is the subject of this document.</p>
        <div>Investment Objective</div><div>Fees and Expenses</div>
      </body>
    </html>
    """

    result = ShadowEvidencePolicy().evaluate(
        content, "EXMXX", "C000000001", "S000000001", metadata()
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert any(
        value == "document does not contain exact ticker EXMXX"
        for value in result.missing_evidence
    )


def test_declared_form_alone_does_not_establish_document_kind():
    result = ShadowEvidencePolicy().evaluate(
        html("<p>EXMXX administrative filing material.</p>"),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="485BPOS",
    )

    assert result.document_kind is DocumentKind.UNKNOWN
    assert any(
        signal.code == "declared_filing_form" for signal in result.signals
    )
    assert result.automatic_use is AutomaticUseLabel.REVIEW


def test_mixed_registration_package_preserves_complete_prospectus_and_sai():
    content = html(
        "<h1>Prospectus</h1>"
        "<table><tr><td>Example Treasury Fund</td><td>EXMXX</td></tr></table>"
        "<h2>Statement of Additional Information</h2>"
        "<p>Investment Advisory and Other Services</p>"
        "<h2>Investment Objective</h2><h2>Fees and Expenses</h2>"
        "<h2>Principal Investment Strategies</h2><h2>Principal Risks</h2>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="485BPOS",
    )

    assert (
        result.document_kind
        is DocumentKind.COMBINED_PROSPECTUS_PACKAGE
    )
    assert result.content_profile.contains_statutory_prospectus
    assert result.content_profile.contains_sai
    assert not result.content_profile.is_supplement
    assert result.document_scope is DocumentScope.TICKER_SPECIFIC
    assert result.automatic_use is AutomaticUseLabel.ALLOWED


def test_summary_reference_does_not_claim_substantive_sai_content():
    result = ShadowEvidencePolicy().evaluate(
        complete(
            "<p>Example Treasury Fund (EXMXX)</p>"
            "<p>The Statement of Additional Information is incorporated "
            "by reference.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS
    assert not result.content_profile.contains_sai
    assert not result.content_profile.contains_statutory_prospectus


def test_prospectus_supplement_phrase_is_top_level_supplement():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Prospectus Supplement</h1>"
            "<p>Example Treasury Fund EXMXX is updated.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.content_profile.is_supplement
    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def test_appendix_a_fund_list_establishes_multi_fund_supplement_scope():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Supplement dated July 1, 2026</h1>"
            "<p>This change applies to the Funds listed in Appendix A.</p>"
            "<h2>Appendix A</h2>"
            "<p>Other Fund</p><p>Example Treasury Fund</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_scope is DocumentScope.MULTI_FUND
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any(
        signal.code == "supplement_appendix_subject"
        for signal in result.signals
    )


def test_exhaustive_fund_list_can_prove_requested_fund_exclusion():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Prospectus Supplement</h1>"
            "<p>For the following funds: Other Income Fund; Other Bond Fund "
            "1. The principal risk disclosure is replaced.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.NEGATIVE
    assert result.document_scope is DocumentScope.MULTI_FUND
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any("exhaustive covered-fund list" in item for item in result.contradictions)


def test_non_exhaustive_fund_mentions_do_not_prove_exclusion():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Prospectus Supplement</h1>"
            "<p>Other Income Fund and Other Bond Fund are discussed below.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.AMBIGUOUS
    assert not result.contradictions
    assert result.automatic_use is AutomaticUseLabel.REVIEW


def test_supplement_detection_uses_bounded_cover_not_first_section_split():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<h1>Portfolio Manager Changes</h1>"
            "<p>Example Treasury Fund Supplement dated July 1, 2026 "
            "to the Prospectus and Summary Prospectus.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.document_kind is DocumentKind.SUPPLEMENT
    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def test_497k_fund_summary_heading_can_establish_complete_summary():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<p>Example Treasury Fund (EXMXX) Fund Summary</p>"
            "<p>Before you invest, review the Fund's prospectus.</p>"
            "<h2>Investment Goal</h2><h2>Fee Table</h2>"
            "<h2>Principal Investment Strategies</h2>"
            "<h2>Risks of Investing</h2>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="497K",
    )

    assert result.document_kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.automatic_use is AutomaticUseLabel.ALLOWED


def test_untagged_appended_sai_is_detected_as_combined_content():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<p>Prospectus</p><p>Example Treasury Fund EXMXX</p>"
            "<p>Investment Objective</p><p>Fees and Expenses</p>"
            "<p>Principal Investment Strategies</p><p>Risk Factors</p>"
            "<p>Statement of Additional Information</p>"
            "<p>This Statement of Additional Information is not a prospectus "
            "and should be read in conjunction with it.</p>"
            "<p>Investment Restrictions</p><p>Portfolio Transactions</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="497",
    )

    assert result.document_kind is DocumentKind.COMBINED_PROSPECTUS_PACKAGE
    assert result.content_profile.contains_statutory_prospectus
    assert result.content_profile.contains_sai
    assert result.automatic_use is AutomaticUseLabel.ALLOWED


def test_top_level_sai_reference_does_not_become_statutory_prospectus():
    result = ShadowEvidencePolicy().evaluate(
        html(
            "<p>Statement of Additional Information</p>"
            "<p>This Statement of Additional Information is not a prospectus "
            "and should be read with the Fund's prospectus.</p>"
            "<p>Example Treasury Fund EXMXX</p>"
            "<p>Investment Objective</p><p>Fees and Expenses</p>"
            "<p>Principal Investment Strategies</p><p>Risk Factors</p>"
            "<p>Investment Restrictions</p><p>Portfolio Transactions</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
        declared_form="497",
    )

    assert (
        result.document_kind
        is DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION
    )
    assert not result.content_profile.contains_statutory_prospectus
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def test_requested_ticker_on_one_of_several_class_covers_is_strong():
    content = html(
        "<p>Example Treasury Fund Class /Ticker Institutional /OTHER</p>"
        "<p>Summary Prospectus</p><p>Before you invest, review the prospectus.</p>"
        "<p>Example Treasury Fund Class /Ticker Investor /EXMXX</p>"
        "<p>Summary Prospectus</p><p>Before you invest, review the prospectus.</p>"
        "<p>Investment Objective</p><p>Fees and Expenses</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert any(
        signal.code == "ticker_in_prospectus_class_cover"
        for signal in result.signals
    )


def test_exact_series_cover_roster_can_exclude_requested_class():
    filing_metadata = parse_filing_identity_header(
        """
        <!--
        <ACCESSION-NUMBER>0000000001-26-000001
        <CONFORMED-NAME>EXAMPLE TRUST
        <SERIES>
        <OWNER-CIK>0000000001
        <SERIES-ID>S000000001
        <SERIES-NAME>Example Treasury Fund
        <CLASS-CONTRACT>
        <CLASS-CONTRACT-ID>C000000001
        <CLASS-CONTRACT-NAME>Investor Shares
        <CLASS-CONTRACT-TICKER-SYMBOL>EXMXX
        </CLASS-CONTRACT>
        <CLASS-CONTRACT>
        <CLASS-CONTRACT-ID>C000000002
        <CLASS-CONTRACT-NAME>Institutional Shares
        <CLASS-CONTRACT-TICKER-SYMBOL>OTHER
        </CLASS-CONTRACT>
        </SERIES>
        -->
        """
    )
    content = html(
        "<p>Example Treasury Fund Institutional Shares: OTHER</p>"
        "<p>Summary Prospectus</p><p>Before you invest, review the prospectus.</p>"
        "<p>Investment Objective</p><p>Fees and Expenses</p>"
    )

    result = ShadowEvidencePolicy().evaluate(
        content,
        "EXMXX",
        "C000000001",
        "S000000001",
        filing_metadata,
    )

    assert result.relevance is RelevanceLabel.NEGATIVE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED
    assert any("lists sibling classes" in item for item in result.contradictions)


def test_legal_name_abbreviation_supports_universal_supplement_scope():
    filing_metadata = parse_filing_identity_header(
        """
        <!--
        <ACCESSION-NUMBER>0000000001-26-000001
        <CONFORMED-NAME>EXAMPLE CO I
        <SERIES>
        <OWNER-CIK>0000000001
        <SERIES-ID>S000000001
        <SERIES-NAME>Example Treasury Fund
        <CLASS-CONTRACT>
        <CLASS-CONTRACT-ID>C000000001
        <CLASS-CONTRACT-NAME>Investor Shares
        <CLASS-CONTRACT-TICKER-SYMBOL>EXMXX
        </CLASS-CONTRACT>
        </SERIES>
        -->
        """
    )

    result = ShadowEvidencePolicy().evaluate(
        html(
            "<p>Example Company I, and each series thereof</p>"
            "<p>Supplement dated July 1, 2026 to each Fund's prospectus.</p>"
        ),
        "EXMXX",
        "C000000001",
        "S000000001",
        filing_metadata,
    )

    assert result.relevance is RelevanceLabel.POSITIVE
    assert result.document_scope is DocumentScope.REGISTRANT_WIDE
    assert result.automatic_use is AutomaticUseLabel.DISALLOWED


def _qualified_for_scope(scope):
    evaluation = ShadowEvidencePolicy().evaluate(
        complete("<h1>Example Treasury Fund EXMXX</h1>"),
        "EXMXX",
        "C000000001",
        "S000000001",
        metadata(),
    )
    evaluation.document_scope = scope
    return evaluation


def test_shadow_candidate_selection_prefers_unique_ticker_specific_sibling():
    selection = select_shadow_candidate(
        [
            ShadowCandidate(
                "combined.htm",
                _qualified_for_scope(DocumentScope.MULTI_FUND),
            ),
            ShadowCandidate(
                "narrow.htm",
                _qualified_for_scope(DocumentScope.TICKER_SPECIFIC),
            ),
        ]
    )

    assert selection.status is ShadowSelectionStatus.SELECTED
    assert selection.selected_name == "narrow.htm"


def test_shadow_candidate_selection_accepts_multi_fund_fallback():
    selection = select_shadow_candidate(
        [
            ShadowCandidate(
                "combined.htm",
                _qualified_for_scope(DocumentScope.MULTI_FUND),
            )
        ]
    )

    assert selection.status is ShadowSelectionStatus.SELECTED
    assert selection.selected_name == "combined.htm"


def test_shadow_candidate_selection_keeps_equal_scope_tie_in_review():
    selection = select_shadow_candidate(
        [
            ShadowCandidate(
                "one.htm",
                _qualified_for_scope(DocumentScope.TICKER_SPECIFIC),
            ),
            ShadowCandidate(
                "two.htm",
                _qualified_for_scope(DocumentScope.TICKER_SPECIFIC),
            ),
        ]
    )

    assert selection.status is ShadowSelectionStatus.REVIEW
    assert selection.selected_name is None


def test_shadow_candidate_selection_error_prevents_uniqueness_claim():
    selection = select_shadow_candidate(
        [
            ShadowCandidate(
                "combined.htm",
                _qualified_for_scope(DocumentScope.MULTI_FUND),
            ),
            ShadowCandidate("broken.htm", error="timeout"),
        ]
    )

    assert selection.status is ShadowSelectionStatus.REVIEW
    assert "could not be evaluated" in selection.reason
