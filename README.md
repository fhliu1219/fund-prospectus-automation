# Fund Prospectus Retrieval

A command-line tool that takes a fund **ticker** (e.g. `VUSXX`, `SPY`, `QQQ`),
finds its **latest prospectus** on the SEC's public **EDGAR** system, and saves
the document to local storage.

It handles mutual funds, money-market funds, and ETFs across multiple providers,
narrows filing lookup to the fund series when SEC identifiers allow it, and
explains **why** each filing was chosen.

The V2 branch is evolving this CLI into a production-minded retrieval and
verification tool for fund-operations workflows. Its strict
[correctness model](CORRECTNESS_MODEL.md) separates registrant, series,
and class identity from verification of the downloaded document itself.

---

## Setup

Requires Python 3.9+.

```bash
# from the project root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(Tests and the optional PDF feature: `pip install -r requirements-dev.txt`.)

## Usage

```bash
# Single fund
python main.py VUSXX

# Multiple funds (comma- or space-separated)
python main.py VTSAX,VMFXX
python main.py SWPPX TRBCX SPY QQQ VOO SWVXX FDRXX

# From a file (one ticker per line or comma-separated; '#' comments allowed)
python main.py --batch tickers.txt

# Options
python main.py VUSXX --output ./downloads   # change output directory
python main.py VUSXX --pdf                   # also write a PDF (best-effort)
python main.py VUSXX --verbose               # DEBUG logging to the console
```

You can also run it as a module: `python -m prospectus_fetcher VUSXX`.

Documents are saved to `output/{TICKER}/{date}_{form}_{accession}.html` and a
run summary is written to `logs/summary.log`.

### Example run

```
$ python main.py SWPPX,SPY,QQQ,ZZZZ

TICKER  STATUS  FORM     DATE        FILE
------  ------  ----     ----        ----
SWPPX   ok      497K     2026-02-26  output/SWPPX/2026-02-26_497K_0000884546-26-000052.html
SPY     ok      485BPOS  2026-01-26  output/SPY/2026-01-26_485BPOS_0001193125-26-022316.html
QQQ     ok      497K     2026-04-30  output/QQQ/2026-04-30_497K_0001104659-26-052973.html
ZZZZ    error   -        -           Could not resolve ticker ZZZZ to an SEC EDGAR entity
```

The `FORM`/`DATE` columns make the selection policy visible at a glance (SWPPX
got a 497K summary prospectus; SPY a full 485BPOS). The per-fund reasoning is in
`logs/summary.log`, e.g.:

```
VUSXX: selected 497K dated 2025-12-19: highest-priority available form —
  investor-facing 497K prospectus filing (summary or supplement) (note: a
  newer 497 dated 2026-04-16 was skipped to honour the priority policy)
```

---

## How it works

```
ticker → resolve to (CIK [, seriesId]) → find latest prospectus filing
       → resolve primary document → download HTML  (→ optional PDF)
```

1. **Resolve** the ticker against SEC's two mapping files — `company_tickers_mf.json`
   (mutual funds & fund-structured ETFs; includes a `seriesId`) and `ticker.txt`
   (stocks & standalone ETF trusts).
2. **Find the filing.** When a `seriesId` is known we fetch that fund's filings
   from EDGAR's browse-edgar Atom feed and pick the best prospectus form (see
   policy below). Otherwise we use the registrant's submissions history.
3. **Resolve the document** (the filing-designated `primaryDocument`) and download it.

SEC requires a descriptive `User-Agent` and limits clients to 10 requests/second;
both are handled centrally in `sec_client.py`. (EDGAR-internal mechanics are kept
brief here on purpose — see the code/comments for detail.)

---

## Design decisions & assumptions

Every assumption below is illustrated with a concrete example fund.

### 1. Which filing counts as the "latest prospectus" (form priority)

On EDGAR, "prospectus" spans several form types. We rank them with a single,
swappable constant at the top of `prospectus_fetcher/edgar.py`:

```python
PROSPECTUS_FORM_PRIORITY = ["497K", "485BPOS", "485APOS", "N-1A", "497"]
```

This order was retained from the original project's stakeholder-confirmed policy
("497K as primary is the right call ... there is no single correct answer;
document the reasoning").
Per SEC's [EDGAR Filer Manual](https://www.sec.gov/files/edgar/filermanual/efmvol2-c3.pdf):

| Form | What it is | Why this rank |
|---|---|---|
| `497K` | Summary prospectus | Investor-facing summary; most current & readable. **Example: VUSXX, SWPPX, FDRXX.** |
| `485BPOS` | Post-effective amendment, Rule 485(**b**) | Becomes effective immediately → legally in force. **Example: SPY (full 1.2 MB registration).** |
| `485APOS` | Post-effective amendment, Rule 485(**a**) | Delayed-effective (pending SEC review). Ranked below 485BPOS because it is *not yet effective*, but above the original registration. |
| `N-1A` | Original registration statement | Usually years old after launch; a fallback. |
| `497` | Prospectus / supplement filing | **Last resort** — the most recent 497 is often a short *supplement*, not a full prospectus, so we log a warning when it's chosen. |

We select the **highest-priority form that exists**, then the **newest filing of
that form**. Amended variants (e.g. `497K/A`, `N-1A/A`) are ranked by their base
form but the actual form is recorded.

> The tool intentionally prefers the highest-priority form over a *newer*
> lower-priority filing — e.g. a newer 497 supplement is skipped when a recent
> 497K summary prospectus exists. The `selection_reason` in the log states this
> for each fund.

**Known nuance:** a `497K` is *usually* the summary prospectus but can
occasionally itself be a short supplement. **Example: QQQ** — its most recent
`497K` is a supplement to the December 2025 prospectus. It is the
highest-priority filing returned for QQQ's resolved identity under the confirmed
policy, but its completeness is a separate question. (Cross-check against
the issuer's page: <https://www.invesco.com/qqq-etf/en/about.html>.)

### 2. Narrowing the lookup to a fund series

One SEC registrant (CIK) holds **many funds**. For example, "Vanguard Admiral
Funds" filed **12 different funds' 497Ks on the same day**, so naively taking the
registrant's "most recent 497K" can silently return the **wrong series'**
document. We reduce this risk by filtering filings by the fund's `seriesId`.

**Proof it matters:** `VTSAX` and `VOO` are both under CIK `36405`, but resolve
to distinct series-level candidates for *Vanguard Total Stock Market Index Fund*
and *Vanguard 500 Index Fund*. Likewise `VUSXX` (Treasury MMF) vs `VMFXX`
(Federal MMF).

This is series-level, not exact share-class, evidence. A series can contain
multiple ticker-bearing classes, so the current lookup does not prove that the
downloaded document covers the requested ticker. V2 records that distinction
explicitly; class-level lookup and document-content validation are the next
correctness milestones.

### 3. Tickers in `ticker.txt` only (no series id)

Standalone ETF trusts resolve via `ticker.txt` to a registrant CIK with no
series. We assume such a registrant is effectively single-fund and use its
submissions history directly. **Example: SPY** (SPDR S&P 500 ETF Trust). This
assumption is **verified at runtime**: if the CIK actually maps to multiple fund
series, the tool logs a notice rather than silently guessing.

### 4. Other assumptions

- **Combined filings are saved whole.** Some families file one document covering
  several funds; we save it as-is rather than trying to extract one fund's pages.
- **EDGAR is the single source of truth.** Issuer websites (e.g. the Invesco link
  above) are used only as a manual cross-reference, never scraped as a fallback.
- **Unresolvable tickers produce a clean error**, not a guess. **Example: `ZZZZ`.**
- **Archive URLs use the registrant CIK**, never the accession's leading digits
  (that prefix can be a filing agent's CIK, not the fund's).

### Known limitations

- Foreign-domiciled or brand-new funds may not appear in EDGAR's mapping files.
- SEC refreshes the mapping files periodically and does not guarantee their scope
  or accuracy; all tickers in the original validation set resolved as of June 2026.
- A best-effort full-text-search fallback for unmapped tickers is left as an
  extension point — a clean "unresolved" message is preferred over a brittle
  scraper. (Not needed for the current validation set.)

---

## Output & logging

```
output/
  VUSXX/2025-12-19_497K_0001193125-25-325229.html
  SPY/2026-01-26_485BPOS_0001193125-26-022316.html
logs/
  summary.log     # timestamped run log: selection reasons, warnings, summary table
```

The accession is part of the filename to avoid collisions when a fund has
multiple same-date/same-form filings; the form code is filesystem-sanitised
(`497K/A` → `497K-A`).

---

## Docker

The image installs WeasyPrint's system libraries so `--pdf` works out of the box.

```bash
docker build -t prospectus .
docker run --rm -v "$PWD/output:/app/output" prospectus VUSXX
docker run --rm -v "$PWD/output:/app/output" prospectus VTSAX,VMFXX --pdf
```

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest                         # offline, deterministic (HTTP mocked)
RUN_LIVE_TESTS=1 pytest tests/test_integration_live.py   # optional live EDGAR check
```

The live EDGAR integration test is skipped by default so the normal test suite
stays fast, deterministic, and independent of network/SEC availability.

Coverage includes mapping-file parsing, form-priority selection (incl. amended
forms), series filtering, Atom parsing, primary-document resolution (and its
fallback heuristic), archive-URL construction, and the graceful-error path.

---

## Project layout

```
main.py                     # CLI entry point
prospectus_fetcher/
  config.py                 # SEC endpoints, User-Agent, rate limit
  sec_client.py             # rate-limited HTTP client (User-Agent, retries)
  resolver.py               # ticker -> CIK/series; cik->series reverse index
  edgar.py                  # PROSPECTUS_FORM_PRIORITY; filing selection; doc resolution
  downloader.py             # save the document to disk
  converter.py              # optional, best-effort HTML -> PDF
  models.py                 # ResolvedFund, Filing, FetchResult
  cli.py                    # orchestration, summary table, logging
CORRECTNESS_MODEL.md         # V2 identity and document-verification rules
tests/                      # pytest suite (HTTP mocked) + optional live test
Dockerfile
```
