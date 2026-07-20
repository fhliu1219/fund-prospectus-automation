"""Tests for filing-specific series/class submission-header metadata."""

import pytest

from prospectus_fetcher.filing_identity import (
    FilingIdentityParseError,
    IdentityMetadataSource,
    parse_filing_identity_detail,
    parse_filing_identity_header,
    resolve_filing_identity,
)


HEADER = """
<html><head><!--
<SEC-HEADER>
<ACCESSION-NUMBER>0000000001-26-000001
<FILER><COMPANY-DATA>
<CONFORMED-NAME>EXAMPLE TRUST
<CENTRAL-INDEX-KEY>0000000001
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


DETAIL = """
<html>
<head><title>EDGAR Filing Documents for 0000000001-26-000001</title></head>
<body>
<table class="tableSeries" summary="Series and Classes/Contracts Table">
<tr><td class="CIKname">CIK
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=0000000001">0000000001</a>
</td><td></td><td></td><td></td></tr>
<tr>
<td class="seriesName">Series
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=S000000001">S000000001</a>
</td><td></td><td>Example Treasury Fund</td><td></td>
</tr>
<tr class="contractRow">
<td class="classContract">Class/Contract
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=C000000001">C000000001</a>
</td><td></td><td>Investor Shares</td><td>EXMXX</td>
</tr>
<tr class="contractRow">
<td class="classContract">Class/Contract
<a href="/cgi-bin/browse-edgar?action=getcompany&CIK=C000000002">C000000002</a>
</td><td></td><td>Institutional Shares</td><td></td>
</tr>
</table>
<span class="companyName">EXAMPLE TRUST (Filer)
<a href="/cgi-bin/browse-edgar?CIK=0000000001&action=getcompany">0000000001</a>
</span>
<a href="/cgi-bin/browse-edgar?filenum=033-00001&action=getcompany">033-00001</a>
</body>
</html>
"""


STANDALONE_DETAIL = """
<html>
<head><title>EDGAR Filing Documents for 0000000002-26-000002</title></head>
<body>
<span class="companyName">STANDALONE TRUST (Filer)
<a href="/cgi-bin/browse-edgar?CIK=0000000002&action=getcompany">0000000002</a>
</span>
<a href="/cgi-bin/browse-edgar?filenum=811-00002&action=getcompany">811-00002</a>
<a href="/cgi-bin/browse-edgar?filenum=033-00002&action=getcompany">033-00002</a>
</body>
</html>
"""


def test_parses_filing_specific_series_class_names_and_ticker():
    result = parse_filing_identity_header(HEADER)

    assert result.accession == "0000000001-26-000001"
    assert result.registrant_name == "EXAMPLE TRUST"
    assert result.registrant_cik == 1
    assert result.source == IdentityMetadataSource.SUBMISSION_HEADER
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


def test_parses_filing_detail_series_class_and_provenance():
    result = parse_filing_identity_detail(DETAIL)

    assert result.accession == "0000000001-26-000001"
    assert result.registrant_name == "EXAMPLE TRUST"
    assert result.registrant_cik == 1
    assert result.registration_file_numbers == ["033-00001"]
    assert result.source == IdentityMetadataSource.FILING_DETAIL_FALLBACK
    series, class_identity = result.find_class("C000000001")
    assert series.name == "Example Treasury Fund"
    assert class_identity.name == "Investor Shares"
    assert class_identity.ticker == "EXMXX"


def test_parses_standalone_detail_without_series_table():
    result = parse_filing_identity_detail(STANDALONE_DETAIL)

    assert result.registrant_name == "STANDALONE TRUST"
    assert result.registrant_cik == 2
    assert result.series == []
    assert result.registration_file_numbers == ["811-00002", "033-00002"]


def test_falls_back_to_detail_only_when_header_cannot_be_parsed():
    result = resolve_filing_identity("<html>no submission header</html>", DETAIL)

    assert result.source == IdentityMetadataSource.FILING_DETAIL_FALLBACK
    assert result.find_class("C000000001") is not None


def test_keeps_valid_header_as_primary_when_detail_agrees():
    result = resolve_filing_identity(HEADER, DETAIL)

    assert result.source == IdentityMetadataSource.SUBMISSION_HEADER


def test_rejects_disagreement_between_valid_header_and_detail():
    conflicting_detail = DETAIL.replace("Investor Shares", "Conflicting Shares")

    with pytest.raises(
        FilingIdentityParseError,
        match="disagree on series/class relationships",
    ):
        resolve_filing_identity(HEADER, conflicting_detail)
