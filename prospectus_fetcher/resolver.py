"""Resolve a fund ticker to an SEC EDGAR entity.

Lookup order (a ticker may appear in either mapping file):
  1. ``company_tickers_mf.json`` — mutual funds & fund-structured ETFs; carries
     ``seriesId``/``classId``, which lets us narrow the filing candidates.
  2. ``ticker.txt`` — stocks and standalone ETF trusts (e.g. SPY); CIK only.
  3. (optional) a best-effort EDGAR search hook, currently left unimplemented;
     if it finds nothing we return ``None`` so the caller emits a clean error.

Both mapping files are fetched once and cached in memory, so a batch run only
downloads them a single time. Building the file also yields a ``cik -> {seriesId}``
reverse index that :mod:`edgar` reuses to detect multi-series registrants.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Set

from . import config
from .models import ResolvedFund
from .sec_client import SECClient

logger = logging.getLogger(__name__)


class Resolver:
    def __init__(self, client: SECClient) -> None:
        self.client = client
        self._mf_by_symbol: Optional[Dict[str, dict]] = None
        self._series_by_cik: Optional[Dict[int, Set[str]]] = None
        self._ticker_txt: Optional[Dict[str, int]] = None

    # -- mapping-file loaders (cached) -------------------------------------
    def _load_mf(self) -> None:
        if self._mf_by_symbol is not None:
            return
        data = self.client.get_json(config.MF_TICKERS_URL)
        fields = data["fields"]
        i_cik, i_series, i_class, i_sym = (
            fields.index("cik"),
            fields.index("seriesId"),
            fields.index("classId"),
            fields.index("symbol"),
        )
        by_symbol: Dict[str, dict] = {}
        series_by_cik: Dict[int, Set[str]] = {}
        for row in data["data"]:
            symbol = str(row[i_sym]).upper()
            cik = int(row[i_cik])
            series_id = row[i_series] or None
            class_id = row[i_class] or None
            by_symbol[symbol] = {"cik": cik, "series_id": series_id, "class_id": class_id}
            if series_id:
                series_by_cik.setdefault(cik, set()).add(series_id)
        self._mf_by_symbol = by_symbol
        self._series_by_cik = series_by_cik
        logger.debug("Loaded %d mutual-fund ticker rows.", len(by_symbol))

    def _load_ticker_txt(self) -> None:
        if self._ticker_txt is not None:
            return
        text = self.client.get_text(config.TICKER_TXT_URL)
        mapping: Dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].strip().isdigit():
                mapping[parts[0].strip().upper()] = int(parts[1])
        self._ticker_txt = mapping
        logger.debug("Loaded %d ticker.txt rows.", len(mapping))

    # -- public API --------------------------------------------------------
    def series_for_cik(self, cik: int) -> Set[str]:
        """All mutual-fund series ids EDGAR associates with a registrant CIK."""
        self._load_mf()
        return set(self._series_by_cik.get(cik, set()))

    def resolve(self, ticker: str) -> Optional[ResolvedFund]:
        symbol = ticker.strip().upper()
        if not symbol:
            return None

        self._load_mf()
        hit = self._mf_by_symbol.get(symbol)
        if hit is not None:
            return ResolvedFund(
                ticker=symbol,
                cik=hit["cik"],
                series_id=hit["series_id"],
                class_id=hit["class_id"],
                source="mf",
            )

        self._load_ticker_txt()
        cik = self._ticker_txt.get(symbol)
        if cik is not None:
            return ResolvedFund(ticker=symbol, cik=cik, source="ticker_txt")

        found = self._search_fallback(symbol)
        if found is not None:
            return found

        logger.info("Could not resolve ticker %s to an SEC EDGAR entity.", symbol)
        return None

    def _search_fallback(self, symbol: str) -> Optional[ResolvedFund]:
        """Best-effort extension point for tickers absent from both files.

        Intentionally a no-op: the current validation set resolves via the two
        mapping files, and a clean "unresolved" error is preferable to a brittle
        search scraper. Implement here (EDGAR full-text/company search) if a
        broader ticker universe is ever needed.
        """
        return None
