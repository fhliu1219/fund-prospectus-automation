"""Static configuration: SEC endpoints, request policy, output locations.

No logic lives here — just constants — so the values are easy to find and
change in one place.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# SEC request policy
# ---------------------------------------------------------------------------
# SEC requires a descriptive User-Agent that includes a contact email, and
# enforces a hard limit of 10 requests/second. See:
# https://www.sec.gov/os/webmaster-faq#developers
USER_AGENT = "fund-prospectus-automation/0.1 (vliu3106@gmail.com)"

# Stay safely under SEC's 10 req/s ceiling.
MAX_REQUESTS_PER_SECOND = 8.0

REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# SEC endpoints
# ---------------------------------------------------------------------------
# Ticker -> CIK mappings (two files; a ticker may be in either).
MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
TICKER_TXT_URL = "https://www.sec.gov/include/ticker.txt"

# Filing history for an entity. CIK MUST be zero-padded to 10 digits here.
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Older filing batches referenced by filings.files[].name in the submissions JSON.
SUBMISSIONS_BATCH_URL = "https://data.sec.gov/submissions/{name}"

# browse-edgar: filings filtered by a fund's series id, as an Atom feed.
BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

# Document archive base. NOTE: this uses the *unpadded* registrant CIK — the
# leading segment of an accession number can be a filing agent's CIK, not the
# fund's, so never derive this from the accession.
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = "output"
LOG_DIR = "logs"
SUMMARY_LOG = "summary.log"
