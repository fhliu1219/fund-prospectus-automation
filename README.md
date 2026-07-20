# Fund Prospectus Retrieval

A command-line tool that takes a fund **ticker** (e.g. `VUSXX`, `SPY`, `QQQ`),
finds its **latest prospectus material** on the SEC's public **EDGAR** system,
validates the downloaded content, and saves an evidence-bearing document package
to local storage.

It handles mutual funds, money-market funds, and ETFs across multiple providers,
narrows filing lookup to the fund class when SEC identifiers allow it, and
explains **why** each filing was chosen.

The V2 branch is evolving this CLI into a production-minded retrieval and
verification tool for fund-operations workflows. Its strict
[correctness model](CORRECTNESS_MODEL.md) separates registrant, series,
and class identity from verification of the downloaded document itself. The
[caveats register](CAVEATS.md) tracks residual risks, assumptions, and active
design decisions as the project evolves. Future work is ordered in the
[accuracy-first roadmap](ROADMAP.md), and representative validation cases are
defined in the [curated contract matrix](TEST_MATRIX.md).

---

## Setup

Requires Python 3.9+.

```bash
# from the project root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Service persistence dependencies are isolated in `requirements-service.txt`.
Tests install both CLI and service dependencies through
`pip install -r requirements-dev.txt`. The optional PDF feature still requires
WeasyPrint and its system libraries.

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

# Staged V7 evidence policy; omit the option or use "legacy" to roll back
python main.py VUSXX --validation-policy v7
```

You can also run it as a module: `python -m prospectus_fetcher VUSXX`.

`legacy` remains the default validation policy. The same setting can be
supplied through `PROSPECTUS_VALIDATION_POLICY=v7` for controlled deployments.

Documents are saved to `output/{TICKER}/{date}_{form}_{accession}.html`. Every
successful ticker also receives `output/{TICKER}/manifest.json`, which records
structured SEC identity, source URLs, content classification, verification,
warnings, document roles, byte sizes, SHA-256 checksums, and any recovery
candidate decisions. The manifest also records the controlling validation
policy and version. A run summary is written to `logs/summary.log`.

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
ticker → resolve to (CIK [, seriesId, classId]) → find preferred filing
       → resolve primary document → classify and verify HTML
       → save document package + manifest  (→ optional PDFs)
```

1. **Resolve** the ticker against SEC's two mapping files — `company_tickers_mf.json`
   (mutual funds & fund-structured ETFs; includes `seriesId` and `classId`) and `ticker.txt`
   (stocks & standalone ETF trusts).
2. **Find the filing.** Prefer the class-level Atom feed when a `classId` is
   available. If it has no qualifying prospectus, fall back to the `seriesId`
   feed with an explicit warning. Use registrant submissions only when neither
   fund identifier is available.
3. **Resolve the document** (normally the filing-designated `primaryDocument`)
   and download its bytes for validation. If it fails verification, inventory
   the accession and inspect eligible SEC-labeled prospectus siblings.
4. **Validate the content.** The default legacy policy uses direct
   ticker/class and structural evidence. The feature-flagged V7 policy also
   combines filing-specific SEC identity metadata, bounded cover evidence,
   document structure, contradictions, and document scope. If its required
   identity metadata is unavailable, it fails closed to manual review.
5. **Build the package.** A complete prospectus produces one document plus a
   manifest. If the latest filing is a supplement, exhaust the older filing
   metadata exposed for the same identity scope, evaluate likely dates first,
   and include a verified complete base prospectus. An ambiguous or incomplete
   relationship is saved as `manual_review_required`.

SEC requires a descriptive `User-Agent` and limits clients to 10 requests/second;
both are handled centrally in `sec_client.py`. (EDGAR-internal mechanics are kept
brief here on purpose — see the code/comments for detail.)

Every consumed SEC response is validated before selection. Structurally valid
empty feeds remain empty; missing required fields, malformed identifiers/dates,
misaligned submissions arrays, and unsafe archive filenames produce explicit
endpoint/path-specific errors instead of being treated as absent data.

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
| `497K` | Summary prospectus filing; occasionally a supplement | Usually the most current and readable investor-facing document. **Example: VUSXX, SWPPX, FDRXX.** |
| `485BPOS` | Post-effective amendment, Rule 485(**b**) | Becomes effective immediately → legally in force. **Example: SPY (full 1.2 MB registration).** |
| `485APOS` | Post-effective amendment, Rule 485(**a**) | Delayed-effective (pending SEC review). Ranked below 485BPOS because it is *not yet effective*, but above the original registration. |
| `N-1A` | Original registration statement | Usually years old after launch; a fallback. |
| `497` | Prospectus / supplement filing | **Last resort** — the most recent 497 is often a short *supplement*, not a full prospectus, so we log a warning when it's chosen. |

Within the strongest available identity level, we select the **highest-priority
form that exists**, then the **newest filing of that form**. Amended variants
(e.g. `497K/A`, `N-1A/A`) are ranked by their base form but the actual form is
recorded.

> The tool intentionally prefers the highest-priority form over a *newer*
> lower-priority filing — e.g. a newer 497 supplement is skipped when a recent
> 497K summary prospectus exists. The `selection_reason` in the log states this
> for each fund.

Identity specificity is evaluated first: any qualifying class-associated form
wins before the tool considers series-associated candidates. This favors
share-class relevance over form preference across identity levels. Document
validation remains necessary because a class-associated filing can still be a
supplement.

**Known nuance:** a `497K` is *usually* the summary prospectus but can
occasionally itself be a short supplement. **Example: QQQ** — its most recent
`497K` is a supplement to the December 2025 prospectus. It is the
highest-priority filing returned for QQQ's resolved identity under the confirmed
policy, but its completeness is a separate question. (Cross-check against
the issuer's page: <https://www.invesco.com/qqq-etf/en/about.html>.)

### 2. Narrowing the lookup to a fund class

One SEC registrant (CIK) holds **many funds**. For example, "Vanguard Admiral
Funds" filed **12 different funds' 497Ks on the same day**, so naively taking the
registrant's "most recent 497K" can silently return the **wrong series'**
document. Series filtering reduces that risk, but one series can still contain
multiple ticker-bearing share classes. V2 therefore queries the requested
`classId` first.

**Proof it matters:** VUSXX resolves to class `C000005732` inside series
`S000002233`. A candidate selected from that class feed receives `class` identity
instead of the weaker `series` identity. If the class feed contains no qualifying
prospectus, the tool falls back to the series feed and records the downgrade.

Class-level SEC association still does not prove that the selected primary
document explicitly covers the ticker or is complete. The content validator
therefore checks document type and direct identity evidence independently.

### 3. Tickers in `ticker.txt` only (no series id)

Standalone ETF trusts may resolve via `ticker.txt` to a registrant CIK with no
series or class identifier. The tool uses registrant submissions but preserves
`registrant` identity confidence. It can independently verify the downloaded
content when the document directly covers the ticker. **Example: SPY** remains
registrant-level while its 485BPOS prospectus is `document_verified` from its
content. This does not manufacture missing class-level identity.

### 4. Other assumptions

- **Combined filings are saved whole.** Some families file one document covering
  several funds; we save it as-is rather than trying to extract one fund's pages.
- **EDGAR is the single source of truth.** Issuer websites (e.g. the Invesco link
  above) are used only as a manual cross-reference, never scraped as a fallback.
- **Unresolvable tickers produce a clean error**, not a guess. **Example: `ZZZZ`.**
- **Archive URLs use the registrant CIK**, never the accession's leading digits
  (that prefix can be a filing agent's CIK, not the fund's).
- **Content verification is conservative and deterministic.** A complete
  prospectus requires a recognized title plus at least two expected sections.
  Direct ticker/class evidence must appear within the first 20,000 normalized
  visible characters, limiting incidental late-document matches.
- **Supplement packages preserve identity scope.** A class-level supplement
  searches only older class-level candidates; it never silently broadens to a
  series or registrant to find a base.
- **Sibling recovery never guesses.** The accession's `index.json` supplies the
  file inventory and the SEC filing document table supplies document types.
  Exactly one sibling must pass direct-identity and content rules; zero or
  multiple matches require manual review.

### Known limitations

- Foreign-domiciled or brand-new funds may not appear in EDGAR's mapping files.
- SEC refreshes the mapping files periodically and does not guarantee their scope
  or accuracy; all tickers in the original validation set resolved as of June 2026.
- Deterministic phrase rules can route unusual but valid documents to manual
  review, and an incidental early ticker reference can still be a false positive.
- Automatic sibling recovery depends on compatible SEC archive metadata and the
  deterministic content rules. Missing metadata, evaluation failures, or
  multiple qualifying files require manual review.
- A best-effort full-text-search fallback for unmapped tickers is left as an
  extension point — a clean "unresolved" message is preferred over a brittle
  scraper. (Not needed for the current validation set.)

---

## Output & logging

```
output/
  VUSXX/
    2026-06-30_497K_0000891190-26-000223.html
    manifest.json
  QQQ/
    2026-06-10_497K_0001193125-26-265096.html   # latest supplement
    2025-12-19_497K_0001104659-25-123275.html   # verified base
    manifest.json
logs/
  summary.log     # timestamped run log: selection reasons, warnings, summary table
```

The accession is part of the filename to avoid collisions when a fund has
multiple same-date/same-form filings; the form code is filesystem-sanitised
(`497K/A` → `497K-A`).

Each artifact in `manifest.json` also records its SEC source URL and the
accession's `archive_index_url`. Manifest schema version 2 additionally records
the coverage and stopping reason for recovery searches plus every evaluated
candidate's URL, checksum, classification, evidence, and disposition. Rejected
candidate files are not saved as package documents.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Every requested package was retrieved and verified. |
| `1` | At least one ticker failed retrieval or processing. |
| `2` | Invalid CLI usage, such as no ticker input. |
| `3` | Retrieval succeeded, but at least one package requires manual review. |

Exit code `3` preserves the downloaded evidence while preventing unattended
automation from treating review-required output as ready for use.

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

# Optional PostgreSQL 16 migration and concurrency contract
docker compose -f docker-compose.postgres.yml up -d --wait
RUN_POSTGRES_TESTS=1 pytest tests/postgres_contract.py -q
docker compose -f docker-compose.postgres.yml down

# Optional: download and evaluate the checksum-verified 30-case corpus
python -m prospectus_fetcher.corpus_cli all
```

The live EDGAR integration test is skipped by default so the normal test suite
stays fast, deterministic, and independent of network/SEC availability.

Coverage includes mapping-file parsing, form-priority selection (incl. amended
forms), class-first selection, explicit class-to-series fallback, Atom parsing,
validated Atom pagination, historical submissions batches, primary-document
resolution, accession inventory filtering, strict sibling recovery, content
classification, exact-identifier evidence, date-first supplement/base linkage,
manifest serialization, archive-URL construction, and the graceful-error path.
The operations tests additionally cover schema compatibility, idempotency-key
conflicts, transactional work claims, lease recovery and ownership, aggregate
job states, manifest-policy integrity, artifact checksums, terminal resume
behavior, and persistent review-task creation.
S3 tests use botocore request stubs to verify content-addressed keys, SHA-256,
conditional no-overwrite writes, collision handling, and explicit SSE-S3/KMS
parameters without requiring AWS credentials. The PostgreSQL contract file is
not discovered by the default suite; it runs explicitly against the disposable
Docker database and covers Alembic upgrade/downgrade/drift, schema revision
refusal, server-time leases, and eight-worker claim contention.
The single opt-in live contract test exercises VUSXX, QQQ, and SPY across
class-level and registrant-level paths, including a real SEC filing inventory.

The separate [validation corpus](corpus/README.md) measures the legacy
validator against versioned location-aware evidence policies before they
control CLI behavior. Raw SEC bytes and detailed reports remain local; the
committed manifests preserve exact URLs, checksums, labels, and human reasons.
Submission headers are the primary source of filing identity; the evaluator
can use a checksum-pinned SEC filing-detail page only when the header is
unavailable or unparseable, and records which source was used.
An accession- and checksum-disjoint 30-case holdout is committed separately so
development tuning and independent evaluation are not conflated. A second
disjoint 30-case follow-up evaluates the resulting `m6.1-shadow-v5` policy; its
measured activation blockers and the decision to keep `v5` shadow-only are
documented in the corpus guide. Milestone 6.2 adds a disjoint 30-case challenge,
a separately sampled 50-case representative set, mixed-content profiles, and
document scope. `m6.2-shadow-v6` passed both safety gates but missed the approved
recall and representative-review gates, so it remains report-only. Milestone
6.3 then froze 80 new disjoint V7 cases. V7 passed all four approved gates:
zero false approvals, no valid complete document automatically disallowed,
100%/97.5% complete-document recall on challenge/representative data, and 4%
representative review. The exact evaluated policy is now available only through
the explicit `--validation-policy v7` staged-control flag; `legacy` is the
default rollback.

### Durable operations foundation

Milestones 7.1 and 7.2 add a storage-neutral job runner plus SQLite and
PostgreSQL repositories. They persist scoped idempotency keys, leased batch
items, terminal states, package manifests, identity and policy fields,
content-addressed artifacts, and pending review tasks. PostgreSQL claims work
with `FOR UPDATE SKIP LOCKED` and uses database time for lease recovery.

Artifact identity is `(storage_provider, storage_namespace, object_key,
sha256)`, never a worker-local path. The local adapter publishes an immutable
content-addressed object; the S3 adapter sends and verifies SHA-256, uses
conditional no-overwrite writes, and requires explicit SSE-S3 or SSE-KMS
configuration.

This operations path is not wired into the submitted CLI yet. Temporal
stage-aware retries, reviewer decisions, an internal API, deployment-account
AWS validation, and monitoring remain later Milestone 7 work.

---

## Project layout

```
main.py                     # CLI entry point
prospectus_fetcher/
  config.py                 # SEC endpoints, User-Agent, rate limit
  sec_client.py             # rate-limited HTTP client (User-Agent, retries)
  sec_schema.py             # runtime contracts for external SEC responses
  resolver.py               # ticker -> CIK/series/class; cik->series reverse index
  edgar.py                  # PROSPECTUS_FORM_PRIORITY; filing selection; doc resolution
  downloader.py             # save the document to disk
  validator.py              # classify content and collect verification evidence
  filing_identity.py        # parse header identity with filing-detail fallback
  evidence_policy.py        # location-aware Milestone 6/V7 evidence policy
  corpus.py                 # versioned corpus schema and checksum cache
  corpus_evaluator.py       # current-vs-shadow metrics and disagreements
  corpus_cli.py             # opt-in corpus fetch/evaluate commands
  package.py                # assemble documents and write the evidence manifest
  operations.py             # durable job contracts and persistent runner
  sqlite_operations.py      # local/test persistence reference adapter
  operations_schema.py      # SQLAlchemy Core PostgreSQL tables
  postgres_operations.py    # PostgreSQL repository and queue claims
  artifact_store.py         # artifact protocol and local content-addressed store
  s3_artifact_store.py      # checksummed, encrypted, conditional S3 writes
  converter.py              # optional, best-effort HTML -> PDF
  models.py                 # ResolvedFund, Filing, FetchResult
  cli.py                    # orchestration, summary table, logging
CORRECTNESS_MODEL.md         # V2 identity and document-verification rules
CAVEATS.md                   # residual risks, assumptions, and decision log
ROADMAP.md                   # prioritized accuracy-first future milestones
TEST_MATRIX.md               # curated live and deterministic contract cases
corpus/manifest.json         # 30 labeled SEC cases; raw bytes stay ignored
corpus/v6_challenge_manifest.json       # independent mixed-content challenge
corpus/v6_representative_manifest.json  # separately sampled operating profile
corpus/v7_challenge_manifest.json       # fresh V7 activation challenge
corpus/v7_representative_manifest.json  # fresh V7 operating profile
tests/                       # pytest suite (HTTP mocked) + optional live test
migrations/                  # explicit Alembic PostgreSQL migrations
alembic.ini
docker-compose.postgres.yml  # disposable PostgreSQL 16 contract service
requirements-service.txt
Dockerfile
```
