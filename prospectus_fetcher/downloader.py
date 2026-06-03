"""Download a filing's primary document and write it to local storage.

Files land at ``output/{TICKER}/{date}_{form}_{accession}.html``. The accession
suffix prevents collisions when a fund has multiple same-date/same-form filings,
and the form code is filesystem-sanitised so amended forms like ``497K/A`` don't
introduce a path separator.
"""

from __future__ import annotations

import logging
import os
import re

from . import config
from .models import Filing
from .sec_client import SECClient

logger = logging.getLogger(__name__)

_UNSAFE_FORM_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def safe_form(form: str) -> str:
    """'497K/A' -> '497K-A' (and any other unsafe char -> '-')."""
    return _UNSAFE_FORM_CHARS.sub("-", form)


class Downloader:
    def __init__(self, client: SECClient, output_dir: str = config.DEFAULT_OUTPUT_DIR) -> None:
        self.client = client
        self.output_dir = output_dir

    def save(self, filing: Filing, ticker: str) -> str:
        if not filing.doc_url:
            raise ValueError(f"Filing for {ticker} has no document URL")

        content = self.client.get_bytes(filing.doc_url)
        filename = f"{filing.date}_{safe_form(filing.form)}_{filing.accession}{self._extension(filing.doc_url)}"
        ticker_dir = os.path.join(self.output_dir, ticker.upper())
        os.makedirs(ticker_dir, exist_ok=True)
        path = os.path.join(ticker_dir, filename)
        with open(path, "wb") as handle:
            handle.write(content)
        logger.info("Saved %s (%d bytes) -> %s", ticker.upper(), len(content), path)
        return path

    @staticmethod
    def _extension(url: str) -> str:
        base = url.rsplit("/", 1)[-1]
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        return f".{ext}" if ext and ext not in ("htm", "html") else ".html"
