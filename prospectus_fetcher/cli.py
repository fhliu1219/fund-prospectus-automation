"""Command-line orchestration: ticker(s) -> latest prospectus saved locally.

Wires together the resolver, EDGAR lookup, and downloader. Each ticker is
processed independently so one failure never aborts a batch; results are
collected into a summary table (printed and logged) with a per-fund
``selection_reason`` explaining why each filing was chosen.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from . import config
from .converter import to_pdf
from .downloader import Downloader
from .edgar import EdgarClient
from .models import FetchResult
from .resolver import Resolver
from .sec_client import SECClient

try:  # progress bar is a nice-to-have, not required
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None

logger = logging.getLogger("prospectus_fetcher")


class ProspectusFetcher:
    """High-level pipeline: resolve -> find filing -> download (-> optional PDF)."""

    def __init__(self, output_dir: str = config.DEFAULT_OUTPUT_DIR, want_pdf: bool = False) -> None:
        self.client = SECClient()
        self.resolver = Resolver(self.client)
        self.edgar = EdgarClient(self.client, self.resolver)
        self.downloader = Downloader(self.client, output_dir=output_dir)
        self.want_pdf = want_pdf

    def fetch(self, ticker: str) -> FetchResult:
        symbol = ticker.strip().upper()
        try:
            fund = self.resolver.resolve(symbol)
            if fund is None:
                return FetchResult(
                    symbol, "error",
                    error=f"Could not resolve ticker {symbol} to an SEC EDGAR entity",
                )

            filing = self.edgar.find_prospectus(fund)
            if filing is None:
                return FetchResult(symbol, "error", error="No prospectus filing found on EDGAR")

            path = self.downloader.save(filing, symbol)
            logger.info("%s: %s", symbol, filing.selection_reason)
            for warning in filing.warnings:
                logger.warning("%s: %s.", symbol, warning)
            if self.want_pdf:
                to_pdf(path)
            return FetchResult(
                symbol, "ok",
                form=filing.form, date=filing.date, fund_name=filing.fund_name,
                selection_reason=filing.selection_reason, path=path,
                identity_level=filing.identity_level,
                document_verification=filing.document_verification,
                identity_evidence=list(filing.identity_evidence),
                warnings=list(filing.warnings),
            )
        except Exception as exc:  # network/parse errors -> graceful per-ticker error
            logger.debug("Unhandled error fetching %s", symbol, exc_info=True)
            return FetchResult(symbol, "error", error=str(exc))

    def fetch_many(self, tickers: List[str], progress: bool = False) -> List[FetchResult]:
        iterator = tickers
        if progress and tqdm is not None and len(tickers) > 1:
            iterator = tqdm(tickers, desc="Fetching", unit="fund")
        return [self.fetch(t) for t in iterator]


def parse_tickers(raw_args: List[str], batch_file: Optional[str]) -> List[str]:
    """Collect tickers from positional args (space/comma separated) and/or a file."""
    tokens: List[str] = []
    for chunk in raw_args:
        tokens += chunk.replace(",", " ").split()
    if batch_file:
        with open(batch_file) as handle:
            for line in handle:
                line = line.split("#", 1)[0]  # allow comments
                tokens += line.replace(",", " ").split()

    seen, ordered = set(), []
    for token in tokens:
        symbol = token.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


def format_summary(results: List[FetchResult]) -> str:
    """ASCII summary table; FORM/DATE columns make the priority logic visible."""
    header = [("TICKER", "STATUS", "FORM", "DATE", "FILE"),
              ("------", "------", "----", "----", "----")]
    rows = header + [
        (r.ticker, r.status, r.form or "-", r.date or "-", r.path or r.error or "-")
        for r in results
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = [
        "  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4]
        for row in rows
    ]
    return "\n".join(lines)


def _setup_logging(verbose: bool) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)

    file_handler = logging.FileHandler(os.path.join(config.LOG_DIR, config.SUMMARY_LOG))
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prospectus-fetcher",
        description="Fetch the latest fund prospectus from SEC EDGAR and save it locally.",
    )
    parser.add_argument("tickers", nargs="*", help="Fund ticker(s), e.g. VUSXX or 'SPY,QQQ,VOO'")
    parser.add_argument("--batch", metavar="FILE", help="File of tickers (one per line or comma-separated)")
    parser.add_argument("--output", default=config.DEFAULT_OUTPUT_DIR, help="Output directory (default: output/)")
    parser.add_argument("--pdf", action="store_true", help="Also write a PDF copy (best-effort)")
    parser.add_argument("--verbose", action="store_true", help="Verbose (DEBUG) console logging")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    tickers = parse_tickers(args.tickers, args.batch)
    if not tickers:
        logger.error("No tickers provided. Example: python main.py VUSXX")
        return 2

    logger.info("Fetching prospectuses for: %s", ", ".join(tickers))
    fetcher = ProspectusFetcher(output_dir=args.output, want_pdf=args.pdf)
    results = fetcher.fetch_many(tickers, progress=True)

    table = format_summary(results)
    print(table)
    logger.info("Run summary:\n%s", table)

    succeeded = sum(1 for r in results if r.ok)
    logger.info("Done: %d/%d succeeded.", succeeded, len(results))
    return 0 if succeeded == len(results) else 1
