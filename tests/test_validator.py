"""Tests for deterministic document classification and identity evidence."""

from prospectus_fetcher.models import DocumentKind, DocumentVerification
from prospectus_fetcher.validator import DocumentValidator, extract_text


def html(body: str) -> bytes:
    return f"<html><body>{body}</body></html>".encode()


def test_summary_prospectus_with_exact_ticker_is_verified():
    content = html(
        """
        <h1>Summary Prospectus</h1>
        <p>Vanguard Treasury Money Market Fund Investor Shares (VUSXX)</p>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses</h2>
        <h2>Principal Investment Strategies</h2>
        """
    )

    result = DocumentValidator().validate(content, "VUSXX", "C000005732")

    assert result.kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.verification is DocumentVerification.VERIFIED
    assert result.ticker_found is True
    assert "exact ticker VUSXX" in result.evidence[0]


def test_statutory_prospectus_with_exact_ticker_is_verified():
    content = html(
        """
        <h1>SPDR S&amp;P 500 ETF Trust (SPY)</h1>
        <p>Prospectus Dated January 26, 2026</p>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses of the Trust</h2>
        """
    )

    result = DocumentValidator().validate(content, "SPY")

    assert result.kind is DocumentKind.STATUTORY_PROSPECTUS
    assert result.verification is DocumentVerification.VERIFIED
    assert result.referenced_dates == ["2026-01-26"]


def test_supplement_is_classified_before_prospectus_title_signals():
    content = html(
        """
        <h1>SUPPLEMENT DATED JUNE 10, 2026 TO THE:</h1>
        <p>Summary Prospectus dated December 22, 2025 of
        Invesco QQQ Trust, Series 1 (QQQ)</p>
        <p>This supplement amends the Summary Prospectus.</p>
        <h2>Fees and Expenses</h2>
        """
    )

    result = DocumentValidator().validate(content, "QQQ", "C000271435")

    assert result.kind is DocumentKind.SUPPLEMENT
    assert result.verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.ticker_found is True
    assert result.referenced_dates == ["2026-06-10", "2025-12-22"]
    assert result.base_prospectus_dates == ["2025-12-22"]
    assert result.warnings == ["supplement requires a verified base prospectus"]


def test_supplement_extracts_multiple_prospectus_dates_without_own_date():
    content = html(
        """
        <h1>Supplement dated April 30, 2026, to the:</h1>
        <p>Prospectuses dated December 22, 2025 of Invesco QQQ Trust (QQQ)</p>
        <p>and Prospectuses dated December 19, 2025 of another fund</p>
        <p>This supplement amends the prospectuses.</p>
        """
    )

    result = DocumentValidator().validate(content, "QQQ")

    assert result.base_prospectus_dates == ["2025-12-22", "2025-12-19"]


def test_supplement_date_allows_statement_of_additional_information_phrase():
    content = html(
        """
        <h1>Supplement dated June 10, 2026 to the:</h1>
        <p>Prospectuses and Statement of Additional Information dated
        December 19, 2025, of Invesco NASDAQ 100 ETF (QQQ).</p>
        <p>This supplement updates the prospectuses.</p>
        """
    )

    result = DocumentValidator().validate(content, "QQQ")

    assert result.base_prospectus_dates == ["2025-12-19"]


def test_ticker_matching_uses_boundaries():
    content = html(
        """
        <h1>Summary Prospectus</h1>
        <p>Invesco NASDAQ 100 ETF (QQQM)</p>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses</h2>
        """
    )

    result = DocumentValidator().validate(content, "QQQ")

    assert result.kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.ticker_found is False
    assert result.verification is DocumentVerification.MANUAL_REVIEW_REQUIRED


def test_class_identifier_can_supply_direct_identity_evidence():
    content = html(
        """
        <h1>Summary Prospectus</h1>
        <p>SEC class C000005732</p>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses</h2>
        """
    )

    result = DocumentValidator().validate(content, "VUSXX", "C000005732")

    assert result.class_id_found is True
    assert result.verification is DocumentVerification.VERIFIED


def test_unknown_document_routes_to_manual_review():
    result = DocumentValidator().validate(html("<p>Administrative notice</p>"), "VUSXX")

    assert result.kind is DocumentKind.UNKNOWN
    assert result.verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.warnings == ["document type could not be established from content"]


def test_text_extraction_ignores_script_and_style_content():
    content = (
        b"<html><style>.x{content:'FAKE'}</style><script>VUSXX</script>"
        b"<body>Visible&nbsp;text</body></html>"
    )

    assert extract_text(content) == "Visible text"


def test_complete_title_can_follow_a_long_filing_cover_page():
    content = html(
        f"""
        <p>{'filing cover material ' * 700}</p>
        <h1>Prospectus dated January 26, 2026</h1>
        <p>SPDR S&amp;P 500 ETF Trust (SPY)</p>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses</h2>
        """
    )

    result = DocumentValidator().validate(content, "SPY")

    assert result.kind is DocumentKind.STATUTORY_PROSPECTUS
    assert result.verification is DocumentVerification.VERIFIED


def test_incidental_ticker_late_in_document_is_not_direct_identity_evidence():
    content = html(
        f"""
        <h1>Summary Prospectus</h1>
        <h2>Investment Objective</h2>
        <h2>Fees and Expenses</h2>
        <p>{'other fund information ' * 1500}</p>
        <p>Comparison index includes QQQ.</p>
        """
    )

    result = DocumentValidator().validate(content, "QQQ")

    assert result.kind is DocumentKind.SUMMARY_PROSPECTUS
    assert result.ticker_found is False
    assert result.verification is DocumentVerification.MANUAL_REVIEW_REQUIRED


def test_invalid_referenced_date_is_ignored():
    content = html(
        """
        <h1>Supplement dated February 30, 2026</h1>
        <p>This supplement updates the QQQ prospectus.</p>
        """
    )

    result = DocumentValidator().validate(content, "QQQ")

    assert result.kind is DocumentKind.SUPPLEMENT
    assert result.referenced_dates == []
