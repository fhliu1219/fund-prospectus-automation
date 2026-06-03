"""Resolver tests: parsing the two mapping files + the reverse index."""

import pytest
import responses

from prospectus_fetcher import config
from prospectus_fetcher.resolver import Resolver
from prospectus_fetcher.sec_client import SECClient

MF_FIXTURE = {
    "fields": ["cik", "seriesId", "classId", "symbol"],
    "data": [
        [891190, "S000002233", "C000005732", "VUSXX"],
        [36405, "S000002848", "C000007806", "VTSAX"],
        [36405, "S000002839", "C000092055", "VOO"],  # same CIK, different series
    ],
}
TICKER_TXT_FIXTURE = "spy\t884394\naapl\t320193\n"


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    return Resolver(SECClient())


@responses.activate
def test_resolves_mutual_fund_with_series(resolver):
    responses.add(responses.GET, config.MF_TICKERS_URL, json=MF_FIXTURE, status=200)

    fund = resolver.resolve("vusxx")  # lower-case input is normalised

    assert (fund.cik, fund.series_id, fund.class_id, fund.source) == (
        891190,
        "S000002233",
        "C000005732",
        "mf",
    )


@responses.activate
def test_resolves_etf_via_ticker_txt(resolver):
    responses.add(responses.GET, config.MF_TICKERS_URL, json=MF_FIXTURE, status=200)
    responses.add(responses.GET, config.TICKER_TXT_URL, body=TICKER_TXT_FIXTURE, status=200)

    fund = resolver.resolve("SPY")

    assert fund.cik == 884394
    assert fund.series_id is None
    assert fund.source == "ticker_txt"


@responses.activate
def test_unresolvable_ticker_returns_graceful_error(resolver):
    responses.add(responses.GET, config.MF_TICKERS_URL, json=MF_FIXTURE, status=200)
    responses.add(responses.GET, config.TICKER_TXT_URL, body=TICKER_TXT_FIXTURE, status=200)

    assert resolver.resolve("ZZZZ") is None


@responses.activate
def test_reverse_index_detects_multi_series_registrant(resolver):
    responses.add(responses.GET, config.MF_TICKERS_URL, json=MF_FIXTURE, status=200)

    assert len(resolver.series_for_cik(36405)) == 2  # VTSAX + VOO
    assert resolver.series_for_cik(884394) == set()  # standalone trust
