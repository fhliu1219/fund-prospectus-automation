"""CLI orchestration tests: ticker parsing, summary table, graceful errors."""

import logging
from unittest.mock import Mock

import responses

from prospectus_fetcher import config
from prospectus_fetcher.cli import (
    EXIT_FETCH_ERROR,
    EXIT_REVIEW_REQUIRED,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    ProspectusFetcher,
    format_summary,
    main,
    parse_tickers,
)
from prospectus_fetcher.models import (
    DocumentVerification,
    FetchResult,
    Filing,
    IdentityLevel,
    ResolvedFund,
)


def test_parse_tickers_splits_uppercases_dedupes():
    assert parse_tickers(["VUSXX", "spy,qqq"], None) == ["VUSXX", "SPY", "QQQ"]
    assert parse_tickers(["voo", "VOO"], None) == ["VOO"]


def test_parse_tickers_from_batch_file(tmp_path):
    f = tmp_path / "tickers.txt"
    f.write_text("VTSAX\nVMFXX, SWPPX  # a comment\n\n")
    assert parse_tickers([], str(f)) == ["VTSAX", "VMFXX", "SWPPX"]


def test_format_summary_shows_columns_and_statuses():
    results = [
        FetchResult("VUSXX", "ok", form="497K", date="2025-12-19", path="output/VUSXX/x.html"),
        FetchResult("ZZZZ", "error", error="Could not resolve ticker ZZZZ to an SEC EDGAR entity"),
    ]
    table = format_summary(results)
    for col in ("TICKER", "STATUS", "FORM", "DATE", "FILE"):
        assert col in table
    assert "497K" in table
    assert "ok" in table and "error" in table


def test_fetch_returns_package_result_and_logs_warnings(caplog):
    caplog.set_level(logging.WARNING, logger="prospectus_fetcher")
    fetcher = ProspectusFetcher.__new__(ProspectusFetcher)
    fetcher.resolver = Mock(
        resolve=Mock(return_value=ResolvedFund("VUSXX", 891190, "S1", "C1", "mf"))
    )
    fetcher.edgar = Mock(
        find_prospectus=Mock(
            return_value=Filing(
                registrant_cik=891190,
                accession="0000000000-00-000001",
                form="497K",
                date="2026-01-01",
                identity_level=IdentityLevel.SERIES,
                identity_evidence=["selected from series S1"],
                warnings=["example warning"],
            )
        )
    )
    expected = FetchResult(
        "VUSXX",
        "ok",
        identity_level=IdentityLevel.SERIES,
        document_verification=DocumentVerification.MANUAL_REVIEW_REQUIRED,
        identity_evidence=["selected from series S1"],
        warnings=["example warning"],
    )
    fetcher.package_builder = Mock(build=Mock(return_value=expected))

    result = fetcher.fetch("vusxx")

    assert result.identity_level is IdentityLevel.SERIES
    assert result.document_verification is DocumentVerification.MANUAL_REVIEW_REQUIRED
    assert result.identity_evidence == ["selected from series S1"]
    assert result.warnings == ["example warning"]
    assert "VUSXX: example warning." in caplog.text


@responses.activate
def test_fetch_unresolvable_ticker_yields_error_result(monkeypatch, tmp_path):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    responses.add(
        responses.GET,
        config.MF_TICKERS_URL,
        json={"fields": ["cik", "seriesId", "classId", "symbol"], "data": []},
        status=200,
    )
    responses.add(responses.GET, config.TICKER_TXT_URL, body="", status=200)

    result = ProspectusFetcher(output_dir=str(tmp_path)).fetch("ZZZZ")

    assert result.status == "error"
    assert "ZZZZ" in result.error


def _main_with_results(monkeypatch, results):
    monkeypatch.setattr("prospectus_fetcher.cli._setup_logging", lambda verbose: None)
    monkeypatch.setattr(ProspectusFetcher, "fetch_many", lambda self, tickers, progress: results)
    return main([result.ticker for result in results])


def test_main_exit_code_zero_requires_all_results_verified(monkeypatch):
    results = [
        FetchResult(
            "VUSXX",
            "ok",
            document_verification=DocumentVerification.VERIFIED,
        )
    ]

    assert _main_with_results(monkeypatch, results) == EXIT_SUCCESS


def test_main_exit_code_three_preserves_review_as_distinct_state(monkeypatch):
    results = [
        FetchResult(
            "QQQ",
            "ok",
            document_verification=DocumentVerification.MANUAL_REVIEW_REQUIRED,
        )
    ]

    assert _main_with_results(monkeypatch, results) == EXIT_REVIEW_REQUIRED


def test_main_fetch_error_takes_precedence_over_review(monkeypatch):
    results = [
        FetchResult(
            "QQQ",
            "ok",
            document_verification=DocumentVerification.MANUAL_REVIEW_REQUIRED,
        ),
        FetchResult("ZZZZ", "error", error="unresolved"),
    ]

    assert _main_with_results(monkeypatch, results) == EXIT_FETCH_ERROR


def test_main_without_tickers_returns_usage_error(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.cli._setup_logging", lambda verbose: None)

    assert main([]) == EXIT_USAGE_ERROR
