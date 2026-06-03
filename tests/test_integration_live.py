"""Optional live integration test against real SEC EDGAR.

Skipped by default so the suite stays offline and deterministic. Run with:

    RUN_LIVE_TESTS=1 pytest tests/test_integration_live.py
"""

import os

import pytest

from prospectus_fetcher.edgar import EdgarClient, base_form
from prospectus_fetcher.resolver import Resolver
from prospectus_fetcher.sec_client import SECClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to run live EDGAR tests",
)


def test_vusxx_resolves_to_treasury_money_market_prospectus():
    client = SECClient()
    resolver = Resolver(client)
    edgar = EdgarClient(client, resolver)

    fund = resolver.resolve("VUSXX")
    assert fund is not None and fund.series_id  # mutual fund with a series id

    filing = edgar.find_prospectus(fund)
    assert filing is not None
    assert base_form(filing.form) in {"497K", "485BPOS", "485APOS", "N-1A", "497"}
    # archive path uses the registrant CIK (891190), never the accession prefix
    assert filing.doc_url.startswith("https://www.sec.gov/Archives/edgar/data/891190/")
