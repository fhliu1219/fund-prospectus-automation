"""Tests for filing selection, Atom parsing, and prospectus lookup."""

import pytest
import responses

from prospectus_fetcher import config
from prospectus_fetcher.edgar import (
    EdgarClient,
    FilingRef,
    base_form,
    parse_atom,
    parse_filing_document_table,
    select_filing,
)
from prospectus_fetcher.models import DocumentVerification, Filing, IdentityLevel, ResolvedFund
from prospectus_fetcher.sec_client import SECClient
from prospectus_fetcher.sec_schema import SECResponseSchemaError


# --- pure selection logic (no network) -------------------------------------

def test_base_form_normalizes_amendments():
    assert base_form("497K/A") == "497K"
    assert base_form("N-1A/A") == "N-1A"
    assert base_form("485BPOS") == "485BPOS"


def test_select_prefers_higher_priority_even_if_older():
    filings = [
        FilingRef(form="497", date="2026-05-01", accession="a-1"),       # newer, lower priority
        FilingRef(form="497K", date="2025-12-19", accession="a-2"),      # older, higher priority
    ]
    chosen, reason = select_filing(filings)
    assert chosen.form == "497K"
    assert "skipped" in reason  # notes the newer 497 it passed over


def test_select_newest_within_chosen_form():
    filings = [
        FilingRef(form="497K", date="2024-12-20", accession="old"),
        FilingRef(form="497K", date="2025-12-19", accession="new"),
    ]
    chosen, _ = select_filing(filings)
    assert chosen.accession == "new"


def test_select_treats_amended_form_with_its_base():
    filings = [FilingRef(form="497K/A", date="2025-12-19", accession="a-1")]
    chosen, _ = select_filing(filings)
    assert chosen.form == "497K/A"  # actual form preserved, ranked as 497K


def test_select_returns_none_without_prospectus_forms():
    assert select_filing([FilingRef(form="NPORT-P", date="2026-01-01", accession="x")]) is None


# --- Atom parsing -----------------------------------------------------------

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <content type="application/atom+xml">
      <accession-number>0001193125-25-325229</accession-number>
      <filing-date>2025-12-19</filing-date>
      <filing-type>497K</filing-type>
      <filing-href>https://www.sec.gov/Archives/edgar/data/891190/000119312525325229/0001193125-25-325229-index.htm</filing-href>
    </content>
    <title>497K - Summary Prospectus</title>
  </entry>
  <entry>
    <content>
      <accession-number>0001111111-26-000001</accession-number>
      <filing-date>2026-01-31</filing-date>
      <filing-type>NPORT-P</filing-type>
    </content>
  </entry>
</feed>"""


def test_parse_atom_extracts_filings():
    refs = parse_atom(ATOM)
    assert len(refs) == 2
    first = refs[0]
    assert first.accession == "0001193125-25-325229"
    assert first.form == "497K"
    assert first.filing_href.endswith("-index.htm")


def test_atom_history_follows_validated_next_links(edgar, monkeypatch):
    next_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        "&CIK=C000005732&type=497K%25&output=atom&start=100"
    )
    first_page = ATOM.replace(
        "</feed>",
        f'<link rel="next" href="{next_url.replace("&", "&amp;")}" /></feed>',
    )
    second_page = ATOM.replace(
        "0001193125-25-325229",
        "0001193125-24-000001",
    ).replace("2025-12-19", "2024-12-19")
    calls = []

    def get_text(url, params=None):
        calls.append((url, params))
        return first_page if params is not None else second_page

    monkeypatch.setattr(edgar.client, "get_text", get_text)

    refs = edgar._atom_form_history("C000005732", "497K", "2026-06-10")

    assert [ref.accession for ref in refs if ref.form == "497K"] == [
        "0001193125-25-325229",
        "0001193125-24-000001",
    ]
    assert calls[1] == (next_url, None)


def test_atom_history_rejects_next_link_that_changes_identity(edgar, monkeypatch):
    bad_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        "&CIK=C000000001&type=497K&output=atom&start=100"
    )
    page = ATOM.replace(
        "</feed>",
        f'<link rel="next" href="{bad_url.replace("&", "&amp;")}" /></feed>',
    )
    monkeypatch.setattr(edgar.client, "get_text", lambda url, params=None: page)

    with pytest.raises(SECResponseSchemaError, match="changed the class/series identifier"):
        edgar._atom_form_history("C000005732", "497K", "")


def test_parse_filing_document_table_extracts_type_and_description():
    detail = """
    <html><table summary="Document Format Files">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
      <tr><td>1</td><td>Summary prospectus</td>
          <td><a href="/Archives/example/main.htm">main.htm</a></td>
          <td>497K</td><td>1200</td></tr>
    </table></html>
    """

    documents = parse_filing_document_table(detail)

    assert documents["main.htm"].document_type == "497K"
    assert documents["main.htm"].description == "Summary prospectus"


# --- end-to-end lookup paths (HTTP mocked) ----------------------------------

SUBMISSIONS_VUSXX = {
    "name": "VANGUARD ADMIRAL FUNDS",
    "filings": {
        "recent": {
            "accessionNumber": ["0001193125-25-325229"],
            "filingDate": ["2025-12-19"],
            "form": ["497K"],
            "primaryDocument": ["f43673d1.htm"],
            "primaryDocDescription": ["VANGUARD TREASURY MONEY MARKET FUND"],
        },
        "files": [],
    },
}


@pytest.fixture
def edgar(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    return EdgarClient(SECClient())


@responses.activate
def test_find_prospectus_prefers_class_path(edgar):
    responses.add(responses.GET, config.BROWSE_EDGAR_URL, body=ATOM, status=200)
    responses.add(
        responses.GET,
        config.SUBMISSIONS_URL.format(cik=891190),
        json=SUBMISSIONS_VUSXX,
        status=200,
    )

    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")
    filing = edgar.find_prospectus(fund)

    assert filing.form == "497K"
    assert filing.date == "2025-12-19"
    assert filing.fund_name == "VANGUARD TREASURY MONEY MARKET FUND"
    # archive URL uses the unpadded registrant CIK (891190), not the accession prefix
    assert filing.doc_url == (
        "https://www.sec.gov/Archives/edgar/data/891190/"
        "000119312525325229/f43673d1.htm"
    )
    assert filing.heuristic_used is False
    assert filing.series_id == "S000002233"
    assert filing.class_id == "C000005732"
    assert filing.identity_level is IdentityLevel.CLASS
    assert filing.document_verification is DocumentVerification.NOT_CHECKED
    assert "C000005732" in filing.identity_evidence[0]
    browse_calls = [
        call for call in responses.calls if call.request.url.startswith(config.BROWSE_EDGAR_URL)
    ]
    assert len(browse_calls) == 1
    assert "CIK=C000005732" in browse_calls[0].request.url


def test_find_prospectus_uses_series_when_class_id_is_unavailable(edgar, monkeypatch):
    calls = []

    def atom_filings(identifier):
        calls.append(identifier)
        return [
            FilingRef(
                form="497K",
                date="2026-01-01",
                accession="0000000000-26-000001",
                primary_document="series-prospectus.htm",
            )
        ]

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", None, "mf")

    filing = edgar.find_prospectus(fund)

    assert calls == ["S000002233"]
    assert filing.identity_level is IdentityLevel.SERIES
    assert filing.document_verification is DocumentVerification.NOT_CHECKED
    assert filing.warnings == ["class identifier unavailable; selected at series level"]


def test_class_per_form_recovery_remains_class_level(edgar, monkeypatch):
    feed_calls = []
    form_calls = []

    def atom_filings(identifier):
        feed_calls.append(identifier)
        return [FilingRef(form="NPORT-P", date="2026-01-01", accession="ignored")]

    def atom_filings_by_form(identifier):
        form_calls.append(identifier)
        return [
            FilingRef(
                form="497K",
                date="2025-12-19",
                accession="0000000000-25-000001",
                primary_document="class-prospectus.htm",
            )
        ]

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    monkeypatch.setattr(edgar, "_atom_filings_by_form", atom_filings_by_form)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    filing = edgar.find_prospectus(fund)

    assert feed_calls == ["C000005732"]
    assert form_calls == ["C000005732"]
    assert filing.identity_level is IdentityLevel.CLASS
    assert filing.warnings == []


def test_empty_class_feed_falls_back_to_series_with_warning(edgar, monkeypatch):
    feed_calls = []
    form_calls = []

    def atom_filings(identifier):
        feed_calls.append(identifier)
        if identifier == "C000005732":
            return []
        return [
            FilingRef(
                form="497K",
                date="2025-12-19",
                accession="0000000000-25-000002",
                primary_document="series-prospectus.htm",
            )
        ]

    def atom_filings_by_form(identifier):
        form_calls.append(identifier)
        return []

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    monkeypatch.setattr(edgar, "_atom_filings_by_form", atom_filings_by_form)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    filing = edgar.find_prospectus(fund)

    assert feed_calls == ["C000005732", "S000002233"]
    assert form_calls == ["C000005732"]
    assert filing.identity_level is IdentityLevel.SERIES
    assert filing.class_id == "C000005732"
    assert filing.warnings == [
        "class-level lookup for C000005732 found no qualifying prospectus; "
        "fell back to series S000002233"
    ]


def test_class_candidate_wins_before_series_form_priority(edgar, monkeypatch):
    calls = []

    def atom_filings(identifier):
        calls.append(identifier)
        if identifier != "C000005732":
            raise AssertionError("series feed must not be queried after a class candidate wins")
        return [
            FilingRef(
                form="497",
                date="2026-01-01",
                accession="0000000000-26-000003",
                primary_document="class-supplement.htm",
            )
        ]

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    filing = edgar.find_prospectus(fund)

    assert calls == ["C000005732"]
    assert filing.form == "497"
    assert filing.identity_level is IdentityLevel.CLASS


def test_class_request_failure_does_not_silently_fall_back(edgar, monkeypatch):
    def atom_filings(identifier):
        raise RuntimeError(f"class feed unavailable for {identifier}")

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    with pytest.raises(RuntimeError, match="class feed unavailable"):
        edgar.find_prospectus(fund)


@responses.activate
def test_malformed_class_feed_does_not_silently_fall_back(edgar):
    malformed = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><filing-date>2026-01-01</filing-date><filing-type>497K</filing-type></entry>
    </feed>
    """
    responses.add(responses.GET, config.BROWSE_EDGAR_URL, body=malformed, status=200)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    with pytest.raises(SECResponseSchemaError, match="missing accession-number"):
        edgar.find_prospectus(fund)

    assert len(responses.calls) == 1
    assert "CIK=C000005732" in responses.calls[0].request.url


def test_empty_class_and_series_feeds_do_not_broaden_to_registrant(edgar, monkeypatch):
    feed_calls = []
    form_calls = []

    def atom_filings(identifier):
        feed_calls.append(identifier)
        return []

    def atom_filings_by_form(identifier):
        form_calls.append(identifier)
        return []

    def unexpected_submissions(cik):
        raise AssertionError(f"must not broaden lookup to registrant CIK {cik}")

    monkeypatch.setattr(edgar, "_atom_filings", atom_filings)
    monkeypatch.setattr(edgar, "_atom_filings_by_form", atom_filings_by_form)
    monkeypatch.setattr(edgar, "_get_submissions", unexpected_submissions)
    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")

    filing = edgar.find_prospectus(fund)

    assert filing is None
    assert feed_calls == ["C000005732", "S000002233"]
    assert form_calls == ["C000005732", "S000002233"]


def test_related_candidates_stay_in_selected_class_scope(edgar, monkeypatch):
    refs = [
        FilingRef("497K", "2026-06-10", "selected", primary_document="selected.htm"),
        FilingRef("497", "2026-07-01", "future", primary_document="future.htm"),
        FilingRef("497K", "2025-12-19", "base", primary_document="base.htm"),
        FilingRef("485BPOS", "2025-01-01", "older", primary_document="older.htm"),
        FilingRef("NPORT-P", "2025-12-31", "not-prospectus", primary_document="x.htm"),
    ]
    calls = []

    def all_refs(identifier, dateb):
        calls.append((identifier, dateb))
        return refs

    monkeypatch.setattr(edgar, "_all_atom_prospectus_refs", all_refs)
    fund = ResolvedFund("QQQ", 1, "S1", "C1", "mf")
    selected = Filing(
        registrant_cik=1,
        accession="selected",
        form="497K",
        date="2026-06-10",
        identity_level=IdentityLevel.CLASS,
        identity_evidence=["class C1"],
    )

    candidates = edgar.related_prospectus_refs(fund, selected)

    assert calls == [("C1", "2026-06-10")]
    assert [candidate.accession for candidate in candidates] == ["base", "older"]
    resolved = edgar.resolve_related_prospectus(fund, selected, candidates[0])
    assert resolved.identity_level is IdentityLevel.CLASS
    assert resolved.class_id == "C1"


def test_related_candidates_have_no_fixed_limit_and_prioritize_target_date(
    edgar, monkeypatch
):
    refs = [
        FilingRef(
            "497K",
            f"2025-01-{day:02d}",
            f"0000000001-25-{day:06d}",
            primary_document=f"candidate-{day}.htm",
        )
        for day in range(1, 26)
    ]
    monkeypatch.setattr(
        edgar,
        "_all_atom_prospectus_refs",
        lambda identifier, dateb: refs,
    )
    fund = ResolvedFund("QQQ", 1, "S1", "C1", "mf")
    selected = Filing(
        registrant_cik=1,
        accession="0000000001-26-000001",
        form="497K",
        date="2026-06-10",
        identity_level=IdentityLevel.CLASS,
    )

    candidates = edgar.related_prospectus_refs(
        fund,
        selected,
        target_dates=["2025-01-15"],
    )

    assert len(candidates) == 25
    assert candidates[0].date == "2025-01-15"


def test_registrant_related_candidates_include_historical_submission_batches(
    edgar, monkeypatch
):
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [],
                "filingDate": [],
                "form": [],
            },
            "files": [{"name": "CIK0000000001-submissions-001.json"}],
        }
    }
    historical = {
        "accessionNumber": ["0000000001-20-000001"],
        "filingDate": ["2020-01-02"],
        "form": ["485BPOS"],
        "primaryDocument": ["historical.htm"],
    }
    monkeypatch.setattr(edgar, "_get_submissions", lambda cik: submissions)
    monkeypatch.setattr(
        edgar,
        "_get_submission_batch",
        lambda name: (historical, "historical batch"),
    )
    fund = ResolvedFund("SPY", 1, source="ticker_txt")
    selected = Filing(
        registrant_cik=1,
        accession="0000000001-26-000001",
        form="497K",
        date="2026-06-10",
        identity_level=IdentityLevel.REGISTRANT,
    )

    refs = edgar.related_prospectus_refs(fund, selected)

    assert [ref.accession for ref in refs] == ["0000000001-20-000001"]


def test_accession_inventory_uses_sec_document_types_to_exclude_exhibits(
    edgar, monkeypatch
):
    index_payload = {
        "directory": {
            "item": [
                {"name": "0000000001-26-000001-index.html", "size": 0},
                {"name": "r1.htm", "size": 100},
                {"name": "main.htm", "size": 2000},
                {"name": "sibling.htm", "size": 1800},
                {"name": "ex99.htm", "size": 900},
                {"name": "graphic.htm", "size": 500},
                {"name": "logo.jpg", "size": 300},
            ]
        }
    }
    detail = """
    <html><table summary="Document Format Files">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
      <tr><td>1</td><td>Primary</td><td><a href="main.htm">main.htm</a></td><td>497K</td><td>2000</td></tr>
      <tr><td>2</td><td>Sibling</td><td><a href="sibling.htm">sibling.htm</a></td><td>497K</td><td>1800</td></tr>
      <tr><td>3</td><td>Exhibit</td><td><a href="ex99.htm">ex99.htm</a></td><td>EX-99</td><td>900</td></tr>
      <tr><td>4</td><td>Graphic</td><td><a href="graphic.htm">graphic.htm</a></td><td>GRAPHIC</td><td>500</td></tr>
    </table></html>
    """
    monkeypatch.setattr(edgar.client, "get_json", lambda url: index_payload)
    monkeypatch.setattr(edgar.client, "get_text", lambda url: detail)
    filing = Filing(
        registrant_cik=1,
        accession="0000000001-26-000001",
        form="497K",
        date="2026-01-01",
    )

    inventory = edgar.accession_document_inventory(filing)
    eligible = [document.name for document in inventory.documents if document.eligible]
    reasons = {document.name: document.exclusion_reason for document in inventory.documents}

    assert eligible == ["main.htm", "sibling.htm"]
    assert "exhibit" in reasons["ex99.htm"]
    assert "not a supported prospectus" in reasons["graphic.htm"]
    assert "XBRL" in reasons["r1.htm"]


@responses.activate
def test_find_prospectus_registrant_path_picks_485bpos(edgar):
    submissions = {
        "name": "SPDR S&P 500 ETF TRUST",
        "filings": {
            "recent": {
                "accessionNumber": ["0001193125-26-022316", "0001193125-26-010000"],
                "filingDate": ["2026-01-26", "2026-02-01"],
                "form": ["485BPOS", "497"],
                "primaryDocument": ["d77353d485bpos.htm", "supp.htm"],
                "primaryDocDescription": ["SPDR S&P 500 ETF TRUST", "Supplement"],
            },
            "files": [],
        },
    }
    responses.add(
        responses.GET, config.SUBMISSIONS_URL.format(cik=884394), json=submissions, status=200
    )

    fund = ResolvedFund("SPY", 884394, source="ticker_txt")
    filing = edgar.find_prospectus(fund)

    # 485BPOS wins over the newer 497, and the reason flags the skipped 497
    assert filing.form == "485BPOS"
    assert "skipped" in filing.selection_reason
    assert filing.doc_url.endswith("/d77353d485bpos.htm")
    assert filing.identity_level is IdentityLevel.REGISTRANT
    assert filing.document_verification is DocumentVerification.NOT_CHECKED


@responses.activate
def test_registrant_lookup_rejects_misaligned_submissions_arrays(edgar):
    malformed = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001193125-26-022316"],
                "filingDate": [],
                "form": ["485BPOS"],
            },
            "files": [],
        }
    }
    url = config.SUBMISSIONS_URL.format(cik=884394)
    responses.add(responses.GET, url, json=malformed, status=200)

    with pytest.raises(SECResponseSchemaError, match="array length 0"):
        edgar.find_prospectus(ResolvedFund("SPY", 884394, source="ticker_txt"))


@responses.activate
def test_primary_doc_falls_back_to_index_heuristic(edgar):
    # Class filing whose accession is NOT in submissions -> index.json heuristic
    atom = ATOM.replace("0001193125-25-325229", "0009999999-25-000001")
    responses.add(responses.GET, config.BROWSE_EDGAR_URL, body=atom, status=200)
    responses.add(
        responses.GET,
        config.SUBMISSIONS_URL.format(cik=891190),
        json={"name": "X", "filings": {"recent": {"accessionNumber": [], "filingDate": [],
              "form": [], "primaryDocument": [], "primaryDocDescription": []}, "files": []}},
        status=200,
    )
    index_json = {
        "directory": {
            "item": [
                {"name": "R1.htm", "size": "99999"},        # XBRL viewer, excluded
                {"name": "0009999999-25-000001-index.htm", "size": "5000"},  # index, excluded
                {"name": "prospectus.htm", "size": "80000"},  # winner (largest content)
                {"name": "exhibit.htm", "size": "1000"},
            ]
        }
    }
    responses.add(
        responses.GET,
        "https://www.sec.gov/Archives/edgar/data/891190/000999999925000001/index.json",
        json=index_json,
        status=200,
    )

    fund = ResolvedFund("VUSXX", 891190, "S000002233", "C000005732", "mf")
    filing = edgar.find_prospectus(fund)

    assert filing.doc_url.endswith("/prospectus.htm")
    assert filing.heuristic_used is True
    assert filing.identity_level is IdentityLevel.CLASS
    assert filing.warnings == ["primary document selected by fallback size heuristic"]
