"""Data structures passed between pipeline stages.

These are plain dataclasses with no behaviour, so every other module can
import them without creating cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ResolvedFund:
    """Result of resolving a ticker symbol to an SEC EDGAR entity.

    ``series_id``/``class_id`` are only present for funds found in the mutual
    fund mapping file. Tickers resolved via ``ticker.txt`` (e.g. standalone ETF
    trusts like SPY) have just a registrant CIK.
    """

    ticker: str
    cik: int
    series_id: Optional[str] = None
    class_id: Optional[str] = None
    source: str = ""  # "mf" | "ticker_txt" | "search"


@dataclass
class Filing:
    """A single prospectus filing chosen for a fund."""

    registrant_cik: int
    accession: str            # e.g. "0001193125-25-325229"
    form: str                 # actual form as filed, e.g. "497K" or "497K/A"
    date: str                 # filing date, "YYYY-MM-DD"
    series_id: Optional[str] = None
    filing_detail_url: Optional[str] = None
    doc_url: Optional[str] = None
    fund_name: Optional[str] = None     # from primaryDocDescription when available
    selection_reason: str = ""          # why this filing/form won
    heuristic_used: bool = False        # True if the primary-doc size heuristic was needed


@dataclass
class FetchResult:
    """Outcome of attempting to fetch one ticker's prospectus."""

    ticker: str
    status: str                         # "ok" | "error"
    form: Optional[str] = None
    date: Optional[str] = None
    fund_name: Optional[str] = None
    selection_reason: str = ""
    path: Optional[str] = None          # saved file path on success
    error: Optional[str] = None         # message on failure

    @property
    def ok(self) -> bool:
        return self.status == "ok"
