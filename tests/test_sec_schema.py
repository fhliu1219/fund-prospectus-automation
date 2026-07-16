"""SEC response contracts: malformed external data must fail explicitly."""

import pytest

from prospectus_fetcher.edgar import parse_atom
from prospectus_fetcher.sec_schema import (
    SECResponseSchemaError,
    validate_archive_index,
    validate_filing_table,
    validate_mf_tickers,
    validate_submissions,
    validate_ticker_text,
)


MF_EMPTY = {
    "fields": ["cik", "seriesId", "classId", "symbol"],
    "data": [],
}

EMPTY_ATOM = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

EMPTY_SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": [],
            "filingDate": [],
            "form": [],
        },
        "files": [],
    }
}


def test_valid_empty_responses_are_not_schema_failures():
    validate_mf_tickers(MF_EMPTY)
    validate_ticker_text("")
    validate_submissions(EMPTY_SUBMISSIONS, "submissions")

    assert parse_atom(EMPTY_ATOM) == []
    assert validate_archive_index({"directory": {"item": []}}, "index") == []


def test_mf_mapping_requires_all_identity_fields():
    malformed = {"fields": ["cik", "symbol"], "data": []}

    with pytest.raises(SECResponseSchemaError, match=r"seriesId.*classId"):
        validate_mf_tickers(malformed)


def test_mf_mapping_rejects_short_or_invalid_rows():
    short = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[891190, "S000002233"]],
    }
    invalid_id = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[891190, "not-a-series", "C000005732", "VUSXX"]],
    }

    with pytest.raises(SECResponseSchemaError, match="row has 2 values"):
        validate_mf_tickers(short)
    with pytest.raises(SECResponseSchemaError, match="invalid SEC fund identifier"):
        validate_mf_tickers(invalid_id)


def test_mf_mapping_allows_valid_class_without_ticker_symbol():
    payload = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[891190, "S000002233", "C000005732", ""]],
    }

    validate_mf_tickers(payload)


def test_ticker_text_rejects_rows_that_would_previously_be_skipped():
    with pytest.raises(SECResponseSchemaError, match=r"line 2"):
        validate_ticker_text("spy\t884394\nmalformed-row\n")


def test_atom_rejects_invalid_xml_and_incomplete_entries():
    with pytest.raises(SECResponseSchemaError, match="invalid XML"):
        parse_atom("<feed>")

    missing_accession = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><filing-date>2026-01-01</filing-date><filing-type>497K</filing-type></entry>
    </feed>
    """
    with pytest.raises(SECResponseSchemaError, match="missing accession-number"):
        parse_atom(missing_accession)


def test_filing_table_rejects_parallel_array_mismatch():
    table = {
        "accessionNumber": ["0001193125-26-000001"],
        "filingDate": [],
        "form": ["497K"],
    }

    with pytest.raises(SECResponseSchemaError, match="array length 0"):
        validate_filing_table(table, "submissions", "$.filings.recent")


def test_filing_table_rejects_bad_dates_accessions_and_document_paths():
    base = {
        "accessionNumber": ["0001193125-26-000001"],
        "filingDate": ["2026-01-01"],
        "form": ["497K"],
        "primaryDocument": ["prospectus.htm"],
    }

    bad_date = dict(base, filingDate=["January 1, 2026"])
    bad_accession = dict(base, accessionNumber=["not-an-accession"])
    bad_document = dict(base, primaryDocument=["../prospectus.htm"])

    with pytest.raises(SECResponseSchemaError, match="expected ISO date"):
        validate_filing_table(bad_date, "submissions", "$.recent")
    with pytest.raises(SECResponseSchemaError, match="expected accession"):
        validate_filing_table(bad_accession, "submissions", "$.recent")
    with pytest.raises(SECResponseSchemaError, match="unsafe document filename"):
        validate_filing_table(bad_document, "submissions", "$.recent")


def test_filing_table_allows_safe_archive_relative_primary_document():
    table = {
        "accessionNumber": ["0001193125-26-000001"],
        "filingDate": ["2026-01-01"],
        "form": ["N-MFP3"],
        "primaryDocument": ["xslN-MFP3_X01/primary_doc.xml"],
    }

    validate_filing_table(table, "submissions", "$.recent")


def test_archive_index_normalizes_sizes_and_rejects_bad_items():
    items = validate_archive_index(
        {"directory": {"item": [{"name": "prospectus.htm", "size": "123"}]}},
        "index",
    )
    assert items == [{"name": "prospectus.htm", "size": 123}]

    with pytest.raises(SECResponseSchemaError, match="non-negative integer"):
        validate_archive_index(
            {"directory": {"item": [{"name": "prospectus.htm", "size": "large"}]}},
            "index",
        )
