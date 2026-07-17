"""Tests for filing-specific series/class submission-header metadata."""

import pytest

from prospectus_fetcher.filing_identity import (
    FilingIdentityParseError,
    parse_filing_identity_header,
)


HEADER = """
<html><head><!--
<SEC-HEADER>
<ACCESSION-NUMBER>0000000001-26-000001
<FILER><COMPANY-DATA>
<CONFORMED-NAME>EXAMPLE TRUST
</COMPANY-DATA></FILER>
<SERIES-AND-CLASSES-CONTRACTS-DATA>
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
</CLASS-CONTRACT>
</SERIES>
</SERIES-AND-CLASSES-CONTRACTS-DATA>
</SEC-HEADER>
--></head></html>
"""


def test_parses_filing_specific_series_class_names_and_ticker():
    result = parse_filing_identity_header(HEADER)

    assert result.accession == "0000000001-26-000001"
    assert result.registrant_name == "EXAMPLE TRUST"
    assert result.series[0].series_id == "S000000001"
    assert result.series[0].name == "Example Treasury Fund"
    assert result.series[0].owner_cik == 1
    assert result.series[0].classes[0].name == "Investor Shares"
    assert result.series[0].classes[0].ticker == "EXMXX"
    assert result.series[0].classes[1].ticker is None
    series, class_identity = result.find_class("c000000001")
    assert series.series_id == "S000000001"
    assert class_identity.ticker == "EXMXX"


def test_parses_html_escaped_preformatted_header():
    page = HEADER.replace("<!--", "<pre>").replace("-->", "</pre>")
    page = page.replace("<SEC-HEADER>", "&lt;SEC-HEADER&gt;")

    result = parse_filing_identity_header(page)

    assert result.accession == "0000000001-26-000001"


def test_rejects_duplicate_class_ids():
    duplicate = HEADER.replace(
        "C000000002", "C000000001"
    )

    with pytest.raises(FilingIdentityParseError, match="duplicate class ID"):
        parse_filing_identity_header(duplicate)


def test_allows_registrant_header_without_series_metadata():
    page = """
    <!--
    <ACCESSION-NUMBER>0000000001-26-000001
    <CONFORMED-NAME>STANDALONE TRUST
    -->
    """

    result = parse_filing_identity_header(page)

    assert result.registrant_name == "STANDALONE TRUST"
    assert result.series == []
