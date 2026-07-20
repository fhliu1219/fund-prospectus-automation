# Milestone Caveats, Assumptions, and Decisions

This register is organized by milestone so each stage preserves the reasoning
that existed when it was completed. Every milestone contains:

- caveats that remained at its exit;
- assumptions introduced without an explicit product requirement;
- design decisions made or left open;
- planned work handed to a later milestone.

## Definitions

A **caveat** is recorded only when at least one of these is true:

1. Important behavior has not been tested against realistic external systems.
2. A practical downside remains after the current implementation.
3. The limitation cannot be solved by the current approach.
4. No future milestone currently owns the work.

A gap already assigned to a future milestone is listed as planned work rather
than repeatedly reported as a residual caveat.

Caveat statuses:

- `open`: the risk still exists.
- `mitigated`: controls reduce the risk but do not remove it.
- `retired`: later work removed the risk or made it irrelevant.

## Current residual caveats

| ID | Status | Summary | Next review |
|---|---|---|---|
| M1-C001 | open | SEC ticker mappings may not cover the intended fund universe. | Roadmap Milestone 9, only if product scope expands. |
| M1-C002 | mitigated | SEC availability and future schema changes remain outside project control. | Scheduled monitoring in Roadmap Milestone 7. |
| M1-C003 | open | CIK-only resolution cannot establish a class or series relationship. | Identity enrichment in Roadmap Milestone 8. |
| M2-C001 | mitigated | Live provider coverage exists, but class-to-series fallback remains mocked only. | Curated contract matrix maintenance. |
| M3-C001 | mitigated | V7 materially improves measured coverage, but deterministic rules can still misclassify unfamiliar SEC documents. | Expand the corpus from production review outcomes. |
| M3-C003 | mitigated | V7 is feature-flagged and measured, but direct identifier evidence remains position- and structure-dependent. | Monitor staged V7 disagreements before any default switch. |
| M5-C001 | mitigated | Real SEC archive parsing is live-tested, but no stable live fixture currently triggers sibling replacement. | Curated contract matrix maintenance. |
| M6-C001 | mitigated | Thirty curated cases do not establish production-wide accuracy. | Expand by provider and observed failures. |
| M6-C003 | retired | Filing-detail fallback is cross-checked on real pages and exercised synthetically. | Monitor real fallback frequency. |
| M6.1-C001 | retired | Both `v4` and `v5` were evaluated on separately frozen, disjoint 30-case sets. | Use new data for any later policy version. |
| M6.3-C001 | preserved | V7 activation corpora are consumed evidence and cannot be tuning data for the next policy. | Freeze new disjoint cases before another policy change. |
| M6.3-C002 | mitigated | Complete-document structures still vary beyond measured filing generations. | Route unknown structures to review and learn only from new labeled cases. |
| M6.3-C003 | enforced | Class-cover omission is contradictory only for a demonstrably closed SEC-series roster. | Keep the boundary narrow; do not generalize absence from arbitrary text. |
| M6.3-C004 | open | The five-form policy excludes some valid CIK-only instrument form families. | Milestone 8 supported instrument/form matrix. |
| M6.3-C005 | mitigated | V7 identity metadata is persisted after a completed package, but transient stage failures are not durably retried. | Milestone 7.3 stage-aware Temporal retries. |
| M7.1-C001 | open | The SQLite repository proves local durability but not PostgreSQL concurrency behavior. | Milestone 7.2 PostgreSQL adapter and contention tests. |
| M7.1-C002 | open | Filesystem artifact paths are not durable service-level object references. | Milestone 7.2 artifact-store interface. |
| M7.1-C003 | open | Review tasks can be queued before reviewer authorization and decision semantics are defined. | Product decision before Milestone 7.4. |
| M7.1-C004 | open | SQLite lease recovery relies on comparable UTC clocks. | Replace local lease authority with production workflow/database time in Milestone 7.3. |
| M7.1-C005 | open | A completed ticker failure is terminal in the local runner; retry ownership is not stage-aware. | Define typed activity retries in Milestone 7.3. |

---

## Milestone 1: Correctness Model

**Status:** complete

**Goal:** Define what the system is allowed to claim before changing retrieval
behavior.

### Caveats register

#### M1-C001: SEC ticker mapping completeness

- **Status:** open
- **Risk:** `company_tickers_mf.json` and `ticker.txt` may omit, delay, or
  misrepresent new, foreign, renamed, or unusual funds. Without a trustworthy
  identity mapping, fund-level lookup cannot begin.
- **Evidence:** Tests verify known mappings and clean unresolved errors, but do
  not establish that SEC covers a required ticker universe.
- **Current mitigation:** Unresolved tickers fail explicitly instead of
  triggering a guessed filing.
- **Current scope decision:** The supported universe is currently defined by the
  two official SEC mappings. Unresolved symbols fail closed; broader coverage is
  not required now.
- **Exit condition:** Roadmap Milestone 9, only if product requirements expand
  the supported universe.

#### M1-C002: SEC endpoint and schema stability

- **Status:** mitigated
- **Risk:** Mapping files, Atom feeds, submissions JSON, and archive indexes
  have no project-controlled schema version or availability guarantee.
- **Evidence:** The pipeline depends on required fields, identifier/date formats,
  and aligned submissions arrays.
- **Current mitigation:** Runtime contracts now validate every consumed SEC
  response shape, preserve valid empty responses, reject unsafe filenames, and
  produce endpoint/path-specific errors. HTTP retries, deterministic malformed
  fixtures, and the opt-in live contract remain in place.
- **Remaining limitation:** Validation detects incompatibility but cannot repair
  an SEC outage or automatically understand a genuinely new schema.
- **Exit condition:** Add scheduled compatibility monitoring and alerts in
  Roadmap Milestone 7.

#### M1-C003: CIK-only identity ceiling

- **Status:** open
- **Risk:** A registrant CIK alone cannot prove which series or share class a
  filing covers.
- **Evidence:** SPY currently resolves through `ticker.txt` with only a CIK.
- **Current mitigation:** Results preserve `registrant` confidence and a
  warning instead of claiming class-level correctness.
- **Exit condition:** Implement accuracy-first identity enrichment in Roadmap
  Milestone 8. Until then, preserve registrant identity and direct document
  verification as separate claims.

### Assumptions register

#### M1-A001: EDGAR is the automated source of record

SEC EDGAR remains the automated source of record. Official issuer or exchange
sources remain reference-only unless a later product decision approves automated
corroboration; they may never override conflicting SEC evidence.

#### M1-A002: Existing form priority remains in force

The order `497K`, `485BPOS`, `485APOS`, `N-1A`, `497` remains the
selection policy until content validation provides evidence for a better rule.

#### M1-A003: Confidence must reflect evidence actually used

Resolving a class ID is not enough to report class confidence if filing
selection did not use or independently verify that ID.

#### M1-A004: Architecture changes remain incremental

The project remains a Python CLI until persistence, concurrency, scheduling, or
service requirements justify additional infrastructure.

### Decisions register

#### M1-D001: Separate identity from document verification

`IdentityLevel` records the SEC relationship used for candidate selection.
`DocumentVerification` independently records whether document contents were
checked.

#### M1-D002: Do not change retrieval behavior in Milestone 1

Milestone 1 introduced vocabulary, evidence fields, warnings, and tests without
changing which filing was selected.

#### M1-D003: Keep private study material out of the public repository

The ignored `docs/` directory remains private. Public project contracts are
stored as tracked root-level Markdown files.

### Planned work handed forward

- Class-level filing lookup -> Milestone 2.
- Document content verification -> Milestone 3.
- Persistence and operational scaling -> later milestones.

---

## Milestone 2: Class-Level Filing Lookup

**Status:** complete

**Goal:** Prefer SEC filings associated with the requested class while
preserving honest fallback provenance.

### Caveats register

#### M2-C001: Limited live validation breadth

- **Status:** mitigated
- **Risk:** Live class-to-series fallback behavior has not been observed with a
  stable real ticker; SEC feed behavior may still differ from mocked fixtures.
- **Evidence:** One opt-in live contract now covers VUSXX (Vanguard class-level
  summary), QQQ (Invesco class-level supplement package), and SPY (SPDR
  registrant-level statutory prospectus). Empty-feed and fallback branches are
  covered deterministically with mocked HTTP.
- **Current mitigation:** Offline tests exercise each branch independently of
  SEC availability; the live matrix checks three providers and two identity
  levels.
- **Current test policy:** Maintain the representative categories in
  `TEST_MATRIX.md`; use deterministic fixtures for rare transitions and a small
  live provider matrix for external compatibility.
- **Exit condition:** Add a stable live class-to-series fallback case or capture
  a versioned SEC response fixture from an observed case.

Carried forward unchanged:

- M1-C001: SEC ticker mapping completeness.
- M1-C002: SEC endpoint and schema stability.
- M1-C003: CIK-only identity ceiling.

### Assumptions register

#### M2-A001: Identity specificity precedes form priority

Any qualifying class-associated form is considered before series-associated
forms. Form priority and recency are applied within one identity level.

#### M2-A002: Fallback requires a successful empty result

Class-to-series fallback occurs only after class queries successfully return no
qualifying form. Network and parsing failures remain errors.

#### M2-A003: Known identities must not broaden to CIK

If class and series identifiers are known but both feeds are empty, the tool
returns no filing instead of selecting a potentially unrelated registrant
filing.

#### M2-A004: Confidence warnings are nonfatal

A result may succeed with warnings when it is usable but its identity evidence
is weaker than the strongest supported case.

### Decisions register

#### M2-D001: Query class ID before series ID

A successful class-feed selection produces `IdentityLevel.CLASS`. Series
lookup is a controlled fallback rather than the default mutual-fund path.

#### M2-D002: Class relevance wins across identity levels

A qualifying class-associated `497` is selected before considering a
series-associated `497K`. This favors class relevance over form quality
across identity levels.

#### M2-D003: Transport failures do not lower confidence

A failed class request does not silently trigger series fallback. The ticker
fails so infrastructure problems cannot masquerade as missing class data.

#### M2-D004: All warnings reach operator logs

Identity downgrades and document-selection heuristics are preserved in result
metadata and emitted by the CLI.

### Planned work handed forward

- Determine whether selected documents cover the requested ticker -> Milestone 3.
- Classify complete prospectuses versus supplements -> Milestone 3.
- Reduce per-form request amplification -> scale hardening.
- Expand live provider coverage -> before production-scale batch work.

---

## Milestone 3: Document Evidence Validation

**Status:** complete

**Goal:** Determine whether downloaded material covers the requested ticker or
share class and whether it is a complete standalone prospectus.

### Caveats register

#### M3-C001: Deterministic classifier coverage

- **Status:** mitigated in Milestone 6
- **Risk:** Unusual titles, section names, encodings, combined filings, or new
  SEC document layouts can produce false `unknown`, summary, statutory, or
  supplement classifications.
- **Evidence:** Deterministic fixtures cover each branch; live validation covers
  VUSXX, QQQ, and SPY; and Milestone 6 measures 30 checksum-pinned documents.
  The corpus exposed remaining inline-XBRL and registrant-only misses.
- **Current mitigation:** Unknown or incomplete evidence routes to
  `manual_review_required`; no language model is allowed to silently upgrade
  confidence.
- **Next review:** Expand the corpus using measured misses and production review
  outcomes before changing control thresholds.

#### M3-C002: Bounded supplement-base discovery

- **Status:** retired in Milestone 5
- **Risk:** EDGAR Atom feeds are bounded and the package builder examines at
  most 20 older prospectus candidates. A long supplement history or incomplete
  feed can hide the applicable base.
- **Evidence:** QQQ resolves to its December 2025 base in the live test, but no
  test proves all bases fall within the scan horizon.
- **Current mitigation:** The scan merges the general and per-form feeds, never
  broadens identity scope, and routes a missing base to manual review.
- **Resolution:** Milestone 5 removed the 20-candidate limit, follows every
  validated form-specific Atom `next` link, consumes every submissions history
  batch, orders candidates by the supplement's prospectus dates, and downloads
  lazily until a verified date-linked base is found.

#### M3-C003: Identifier evidence threshold

- **Status:** mitigated
- **Risk:** An exact ticker in the first 20,000 visible characters can still be
  incidental, while a legitimate ticker appearing later can be missed.
- **Evidence:** A regression test rejects an incidental late-document ticker;
  live SPY requires a sufficiently large window because its filing cover is
  long.
- **Current mitigation:** Verification also requires a recognized complete
  document shape. Missing evidence routes to review rather than rejection.
- **Milestone 6.3 result:** V7 adds filing-header identity, legal names,
  structured class tables, cover/front-matter locality, negative examples, and
  explicit missing/contradictory evidence. It passed the frozen activation
  gates and is available behind an explicit feature flag.
- **Next review:** Monitor staged V7 disagreements and expand only from newly
  labeled review outcomes before considering a default-policy switch.

#### M3-C004: No sibling-document recovery

- **Status:** retired in Milestone 5
- **Risk:** If the filing-designated `primaryDocument` is an exhibit, combined
  cover, or otherwise fails content verification, another HTML file in the same
  accession may be the correct ticker-specific prospectus. Milestone 3 does not
  scan sibling files after that failure.
- **Evidence:** Primary-document and largest-file fallback paths are tested, but
  neither test proves the selected file is the only applicable document in its
  accession.
- **Current mitigation:** The file is not overclaimed; it is saved with
  `manual_review_required`, and heuristic primary-document selection remains a
  visible warning.
- **Resolution:** Milestone 5 inventories `index.json`, validates the SEC filing
  document table, excludes generated files/XBRL/exhibits/unsupported forms, and
  records every evaluated sibling. Exactly one qualifying sibling is selected;
  zero or multiple matches require review.

Carried forward unchanged:

- M1-C003: CIK-only identity ceiling. Direct content can verify a CIK-only
  document, but cannot establish missing class or series identity.
- M2-C001: live class-to-series fallback remains unobserved.

### Assumptions register

#### M3-A001: Verification requires direct content evidence

Class-feed association alone does not produce `document_verified`. The
document must contain defensible ticker, class, or applicability evidence.

#### M3-A002: Issuer websites remain reference-only

Automated verification uses SEC material as the source of record. Official
issuer or exchange records remain optional manual corroboration unless their
automation is explicitly approved, and cannot override conflicting SEC evidence.

#### M3-A003: Ambiguity routes to review

Missing, conflicting, or incomplete evidence produces
`manual_review_required` rather than a guessed verified result.

#### M3-A004: Complete-document classification uses title plus sections

A summary or statutory prospectus requires a recognized title signal and at
least two expected sections. A supplement phrase takes precedence over complete
prospectus phrases quoted inside the supplement.

#### M3-A005: Direct identity evidence is position-limited

An exact ticker or class ID must occur within the first 20,000 normalized
visible characters. This favors cover-page and early fund-table evidence over
incidental references later in a combined filing.

#### M3-A006: Supplement linkage requires identity and date evidence

A package is verified only when the supplement directly covers the requested
ticker/class, the base is a verified complete prospectus, and the two documents
share a referenced prospectus date.

#### M3-A007: Retrieval and process readiness are separate

A downloaded package with `manual_review_required` keeps result status `ok`
because retrieval succeeded, but the CLI returns exit code `3` so automation
cannot mistake it for verified, ready-to-use output.

#### M3-A008: Every successful ticker receives a manifest

Standalone prospectuses use a one-document package. Supplements can use a
two-document package. `FetchResult.path` continues to point to the latest
selected document for CLI backward compatibility.

#### M3-A009: Base search is bounded and scope-preserving

**Superseded by Milestone 5.** Milestone 3 searched at most 20 older prospectus
candidates. Milestone 5 exhausts SEC-exposed history while preserving the same
rule that discovery may not broaden class to series or registrant.

### Decisions register

#### M3-D001: Output contract for supplements

- **Status:** accepted
- **Question:** When the latest class-associated filing is a supplement, what
  should the usable result contain?
- **Decision:** Return a package containing the latest applicable
  supplement, its verified base prospectus, and a manifest describing their
  relationship and evidence.
- **Alternative considered:** Return only the latest verified complete prospectus and
  record that a newer supplement exists.
- **Alternative considered:** Return the supplement alone with
  `manual_review_required`.
- **Architectural impact:** This decision changes the model from
  one filing and one output path to multiple related documents.

#### M3-D002: Use deterministic HTML parsing

Visible text is extracted with Python's standard `HTMLParser`. Classification
and evidence rules are explicit, testable, and reproducible; no model-generated
judgment is used in the correctness path.

#### M3-D003: Validate before saving candidate bases

The selected latest document is always preserved. Older candidates are
downloaded in memory and saved only after they qualify as the chosen complete
base, avoiding rejected-candidate output clutter.

#### M3-D004: Preserve identity and document confidence independently

CIK-only SPY can be `document_verified` while remaining `registrant` identity.
Content evidence never upgrades `IdentityLevel` to a class or series that was
not resolved and used.

#### M3-D005: Use an atomic JSON evidence manifest

Each successful package writes a schema-versioned `manifest.json` through a
temporary file and atomic replace. The manifest records structured SEC identity,
source URLs, package verification, component roles, accessions, evidence,
warnings, referenced dates, byte sizes, and SHA-256 checksums.

#### M3-D006: Preserve an archive recovery path

Every document artifact records the accession's `index.json` URL. This does not
yet identify or validate sibling files, but it preserves the exact discovery
path without adding a sibling-content download to every successful request.

### Planned work handed forward

- Route exit-code-3 results into a persisted review workflow in Roadmap
  Milestone 7.
- Persist package records and run state for resumability and idempotency.
- Design sibling-document recovery for unverified primary documents.
- Expand the labeled validation corpus before increasing automation confidence.
- Add approved secondary identity evidence if class-level confidence is required
  for CIK-only tickers.

---

## Milestone 4: External Contract Hardening

**Status:** complete

**Goal:** Detect incompatible SEC responses before selection and make package
readiness unambiguous to automation.

### Caveats register

No new correctness caveat was introduced. M1-C002 remains mitigated because the
project can detect, but cannot prevent, SEC outages or future contract changes.

### Assumptions register

#### M4-A001: Required structure is strict; additive metadata is allowed

Validators require fields and relationships used by the pipeline while ignoring
unknown extra fields. Empty but structurally valid feeds remain valid.

#### M4-A002: Review is not retrieval failure

A review-required package remains saved and has result status `ok`, while process
exit code `3` communicates that it is not ready for unattended downstream use.

#### M4-A003: Archive paths precede automatic sibling scanning

The manifest records `archive_index_url` now. Metadata inventory and sibling
content evaluation remain Roadmap Milestone 5 work.

### Decisions register

#### M4-D001: Validate all consumed SEC response shapes

`company_tickers_mf.json`, `ticker.txt`, Atom entries, submissions tables,
historical batches, and archive indexes receive explicit runtime validation.

#### M4-D002: Reserve exit code 3 for review

Exit codes are `0` for all verified, `1` for retrieval/processing failure, `2`
for invalid CLI usage, and `3` for successful retrieval requiring review.

#### M4-D003: Use a curated contract matrix

Representative live and deterministic cases are tracked in `TEST_MATRIX.md`.
Random sampling may supplement the matrix but does not replace it.

### Planned work handed forward

- Filing-history and sibling recovery -> Roadmap Milestone 5.
- Stronger identity evidence and labeled corpus -> Roadmap Milestone 6.
- Persistent review operations and monitoring -> Roadmap Milestone 7.
- CIK-only identity enrichment -> Roadmap Milestone 8.
- Mapping coverage expansion -> Roadmap Milestone 9, only if scope changes.

---

## Milestone 5: Filing Discovery and Recovery

**Status:** complete

**Goal:** Remove arbitrary filing-search limits and recover the correct document
from an accession without weakening identity scope or silently choosing among
ambiguous siblings.

### Caveats register

#### M5-C001: No stable live sibling-replacement fixture

- **Status:** mitigated
- **Risk:** The live contract validates a real SEC `index.json` and filing
  document table, while deterministic tests prove zero/one/multiple qualifying
  sibling behavior. No stable current accession in the curated ticker set has
  an unverified designated primary plus one verified sibling, so the complete
  replacement path is not exercised against live SEC content.
- **Current mitigation:** External archive shapes are validated live; filtering,
  downloads, checksums, candidate classification, and strict tie behavior are
  covered offline without depending on mutable SEC filing contents.
- **Exit condition:** Add a stable live case when one is observed, or preserve a
  versioned real SEC response/document fixture with permission to retain it.

M3-C002 and M3-C004 are retired by the implementation and tests completed in
this milestone.

### Assumptions register

#### M5-A001: Recovery preserves the selected filing's identity scope

Sibling recovery stays within the selected accession. Supplement-base discovery
stays within the selected class, series, or registrant scope. Neither path may
broaden class to series or registrant to obtain a more convenient match.

#### M5-A002: Candidate metadata is retained instead of every rejected file

Only selected package documents are saved as output artifacts. For every
evaluated sibling, the manifest preserves its SEC URL, checksum,
classification, evidence, warnings, and rejection or selection reason. This
keeps the decision reproducible without treating rejected files as package
documents.

#### M5-A003: Ambiguity is a review state

No qualifying sibling and multiple qualifying siblings are both
`manual_review_required`. Candidate size, filename, or ordering may help decide
evaluation order but may not break a correctness tie.

#### M5-A004: Exhaustive means every history record exposed by SEC

Class and series discovery follows every validated form-specific Atom next-page
link. Registrant discovery consumes the recent submissions table and every
historical batch listed by that response. The tool cannot prove the existence of
records that SEC does not expose; schema/availability failures remain M1-C002.

#### M5-A005: Supplement base dates require prospectus-linked date language

The validator treats dates as candidate base dates only when they occur in a
bounded phrase beginning with `prospectus` or `prospectuses` and ending with
`dated`. Other dates remain general document dates. Unfamiliar wording can still
route to review under M3-C001 rather than creating an inferred relationship.

### Decisions register

#### M5-D001: Strict single-sibling automatic recovery

- **Status:** accepted
- **Question:** When the SEC-designated `primaryDocument` fails verification,
  may another HTML document in the same accession replace it automatically?
- **Decision:** Yes, but only when exactly one sibling satisfies every required
  condition:
  1. it belongs to the same accession and identity-scoped filing;
  2. it contains direct evidence for the requested ticker or class;
  3. it passes complete summary/statutory prospectus validation, or is a valid
     supplement that can continue through supplement-package construction; and
  4. it contains no contradictory identity evidence.
- **Ambiguity rule:** Zero or multiple qualifying siblings produce
  `manual_review_required`. An eligible sibling that cannot be evaluated also
  requires review because uniqueness is unproven. The tool does not use file
  size or archive order as proof of correctness.
- **Provenance rule:** Record every evaluated sibling and its outcome in the
  manifest. Save only the selected document package as primary output.
- **Rationale:** A sibling remains inside the filing already selected at the
  strongest available identity scope, while direct content validation proves
  more than the filing-designated primary filename alone. Requiring exactly one
  qualifying candidate prevents automated selection from hiding ambiguity.

#### M5-D002: Exhaust metadata, evaluate content lazily

- **Status:** accepted and implemented
- **Decision:** Discover all identity-scoped filing metadata before content
  selection. Order references nearest the supplement's explicit prospectus
  dates first, resolve/download one at a time, and stop only after a verified
  date-linked base is found. If no date match exists, exhaust the candidate
  content before selecting a verified fallback for manual review.
- **Rationale:** Removing a numeric candidate cap closes the correctness gap,
  while lazy downloads avoid transferring documents that cannot change a
  conclusive result.

#### M5-D003: Use SEC document types for sibling eligibility

- **Status:** accepted and implemented
- **Decision:** Use `index.json` as the complete accession inventory and the SEC
  filing detail table as the source of document type/description. Index pages,
  XBRL renderings, exhibits, non-HTML files, and unsupported form types are
  excluded before content download. If the detail table cannot be validated,
  automatic sibling recovery is disabled and the package remains reviewable.
- **Rationale:** Archive size and filename ordering are not semantic evidence.
  SEC document types provide a stronger conservative filter, while content
  validation remains necessary for final ticker relevance and completeness.

### Planned work handed forward

- Expand prospectus-date and identity evidence against the Milestone 6 labeled
  corpus before relaxing any automatic-use rule.
- Add a stable real sibling-replacement case to the curated matrix if one becomes
  available; do not manufacture a mutable live dependency merely to remove
  M5-C001.

---

## Milestone 6: Evidence Policy and Validation Corpus

**Status:** complete; shadow policy not activated

**Goal:** Replace position-limited identifier matching with a measured,
location-aware evidence policy while keeping the existing validator in control
until shadow results have been reviewed.

This milestone owns M3-C001 and M3-C003.

### Caveats register

#### M6-C001: Limited corpus representativeness

- **Status:** mitigated
- **Risk:** Thirty deliberately difficult cases across 9 registrant CIKs and 11
  filing-header name labels do not represent every provider, filing layout,
  historical format, or future form.
- **Evidence:** The corpus is balanced for learning rather than statistically
  sampled: 20 positive, 5 negative, and 5 ambiguous relevance labels.
- **Current mitigation:** Every label is checksum-pinned and reasoned; ratios are
  documented as corpus measurements, not production accuracy estimates.
- **Next review:** Add cases from new providers, observed review outcomes, and
  classifier disagreements rather than inflating the corpus randomly.

#### M6-C002: Measured shadow-policy misses

- **Status:** retired as an activation blocker in Milestone 6.3
- **Risk:** `m6-shadow-v1` can still withhold valid automatic output or
  misclassify unusual document structure.
- **Evidence:** It produced zero false automatic approvals but missed three
  expected automatic approvals and had a 30% review rate. Two registrant-only
  SPY filings lacked strong location-aware ticker evidence; one inline-XBRL Fidelity filing
  was not classified as statutory. A relevant SAI safely routed to review rather
  than the expected disallowed state.
- **Current mitigation:** The policy is shadow-only, reports every disagreement,
  and cannot alter packages or exit codes.
- **Next review:** Inspect these cases and add general rules only when they do not
  reduce automatic-verification precision.
- **Resolution:** V5 and V6 iterations addressed the measured miss classes, and
  V7 passed all approved safety, recall, and representative-review gates on 80
  fresh disjoint cases. The general unfamiliar-layout risk remains tracked as
  M6.3-C002.

#### M6-C003: Filing-detail identity fallback not exercised

- **Status:** resolved in Milestone 6.1
- **Risk:** A filing with a missing or incompatible submission header would stop
  corpus evaluation because the filing-detail series/class table is not yet an
  exercised fallback path.
- **Evidence:** All 30 SEC submission headers fetched, checksum-verified, and
  parsed successfully, including providers with and without fund class blocks.
- **Resolution:** Two real filing-detail pages are checksum-pinned and
  cross-checked against valid headers. A malformed-header integration test
  exercises fallback selection. Source disagreements fail closed.
- **Residual boundary:** None of the 30 real corpus headers currently requires
  fallback, so the observed live fallback rate remains 0/30.

### Assumptions register

#### M6-A001: Corpus checksums identify labeled bytes

The corpus label applies to the exact document bytes identified by its SHA-256
checksum. A checksum mismatch is corpus drift or retrieval incompatibility, not
a classifier result.

#### M6-A002: Filing identity metadata and file evidence remain separate

SEC submission-header series/class declarations are authoritative filing-level
identity metadata. They do not prove that every sibling file in the accession
covers every declared class, so direct document evidence remains required.

#### M6-A003: Legal names are corroborating evidence

Legal series and class names can bind a ticker occurrence to the document's
subject, but they are not mandatory when stronger exact class/ticker evidence
exists. A missing name is missing evidence rather than a contradiction.

#### M6-A004: Label axes are independent

Relevance, document kind, and automatic-use eligibility are labeled separately.
A relevant supplement can be `positive` for ticker coverage while remaining
ineligible for standalone automatic use.

#### M6-A005: Ambiguity must be explained

An ambiguous corpus label records observed evidence, missing evidence,
contradictions, and the fact or review action needed to resolve the case.
Ambiguity without a reason is not accepted ground truth.

### Decisions register

#### M6-D001: Start with a curated 30-case corpus

- **Status:** accepted and implemented
- **Decision:** Select 30 provider- and form-diverse SEC documents including
  complete prospectuses, supplements, combined filings, incidental ticker
  mentions, negative cases, and ambiguous cases.
- **Rationale:** Difficult and representative cases provide more useful initial
  evidence than a random sample dominated by straightforward documents.

#### M6-D002: Use manifest plus checksum-verified local cache

- **Status:** accepted and implemented
- **Decision:** Commit URLs, accessions, checksums, labels, and reasons in a
  versioned manifest. Download through an opt-in tool and cache raw documents in
  an ignored local directory. Keep compact synthetic fixtures in Git.
- **Rationale:** This preserves exact labeled bytes and repeatable evaluation
  without committing large SEC documents or requiring network access on every
  run.

#### M6-D003: Use SEC filing-specific legal names

- **Status:** primary path implemented; fallback deferred under M6-C003
- **Decision:** Use the accession's `-index-headers.html` submission header as
  the primary source of series/class IDs, names, and ticker relationships. Use
  the SEC filing-detail table as fallback/cross-check. Issuer and exchange data
  remain corroborating only.
- **Rationale:** Filing-specific metadata reflects the identity declared for
  that accession and avoids applying a later name to a historical filing.

#### M6-D004: Preserve missing and contradictory evidence separately

- **Status:** accepted and implemented
- **Decision:** Missing evidence means relevance cannot be proved;
  contradictory evidence requires an explicit incompatible relationship. Other
  tickers in a combined filing are not contradictions by themselves. Both
  unresolved states route to review, but reports preserve the distinction.

#### M6-D005: Optimize automatic-verification precision first

- **Status:** accepted and implemented
- **Decision:** Initial acceptance prioritizes no known false-positive
  verification, no supplement treated as a complete standalone prospectus, and
  no regression in existing verified cases. Report precision, recall,
  false-positive/false-negative counts, review rate, and confusion matrices.
- **Boundary:** Thirty cases support iteration but cannot establish a universal
  production error rate.

#### M6-D006: Shadow the new policy before activation

- **Status:** accepted and implemented
- **Decision:** The current validator continues to control packages, recovery,
  exit codes, and manifests. The new policy initially emits an evaluation report
  with structured evidence and disagreements only.
- **Activation gate:** Review the corpus baseline and shadow report before
  selecting thresholds or changing control behavior. AI may assist review but
  may not silently upgrade deterministic verification.

### Evidence at completion

- The manifest validates 30 cases with 30 unique document checksums and 30
  unique submission-header checksums.
- Live corpus fetch verified every SEC resource against its committed checksum.
- The corpus contains 9 registrant CIKs, 11 filing-header name labels, and
  summary, statutory, supplement, and unknown document-kind labels.
- The current validator measured 94.1% positive precision, 80.0% positive
  recall, zero false automatic approvals, six missed automatic approvals, and a
  40% review rate on this corpus.
- `m6-shadow-v1` measured 100% positive precision, 90.0% positive recall, zero
  false automatic approvals, three missed automatic approvals, and a 30% review
  rate on this corpus.
- Offline suite: 103 passed with the one opt-in live test skipped normally; the
  live SEC integration test passed separately. Coverage includes manifest
  contracts, checksums, submission-header parsing, location-aware evidence,
  ambiguity, contradictions, and report metrics.

### Planned work handed forward

- Keep the current validator authoritative until a separate activation review.
- Investigate the specific M6-C002 misses before changing thresholds.
- Expand the corpus from new provider layouts and real review outcomes.
- Exercise filing-detail identity fallback before production activation.

---

## Milestone 6.1: Evidence Policy Hardening

**Status:** complete; `m6.1-shadow-v5` retained in shadow mode

### Decisions register

#### M6.1-D001: Represent SAI as an explicit document kind

- **Status:** accepted and implemented
- **Decision:** Add `statement_of_additional_information` instead of treating a
  recognized SAI as `unknown`.
- **Reason:** Relevance and suitability are separate. The TRBCX SAI has direct
  class and ticker evidence, but it is not the prospectus requested by the
  product.
- **Policy:** An SAI may be relevance-positive but is always disallowed as a
  standalone prospectus.
- **Measured result:** The TRBCX shadow disagreement is resolved. Shadow review
  rate decreased from 30.0% to 26.7%, while false automatic approvals remained
  zero and positive precision/recall remained 100%/90%.

#### M6.1-D002: Test a guarded registrant-instrument signal

- **Status:** accepted and implemented in shadow mode
- **Decision:** For CIK-only requests, treat the combined relationship as strong
  only when requested and filing CIKs agree, no mutual-fund series are known,
  the exact SEC registrant name is prominent, the ticker occurs in the document,
  the document is a complete prospectus, and no contradiction exists.
- **Boundary:** This remains registrant-level confidence and does not establish a
  class, series, or permanent instrument identity. The effective-dated
  instrument registry remains the long-term solution.
- **Measured result:** Both SPY shadow misses are resolved. Shadow relevance
  precision and recall are now 100%, false automatic approvals remain zero, one
  expected automatic approval remains missed, and review rate is 20.0%.

#### M6.1-D003: Separate visible HTML from inline-XBRL metadata

- **Status:** accepted and implemented in shadow mode
- **Decision:** Use browser-visible body text for semantic location; parse the
  HTML title, hidden inline-XBRL facts, and declared filing form as separately
  sourced supporting evidence.
- **Safety rules:** Hidden tickers cannot establish visible identity, and form
  type alone cannot establish document kind.
- **Measured result:** FDRXX and both combined Vanguard document-kind
  disagreements are resolved. The shadow policy now matches every label on the
  30-case development corpus, with zero false or missed automatic approvals and
  a 16.7% review rate.

#### M6.1-D004: Use filing detail only as a technical identity fallback

- **Status:** accepted and implemented in shadow evaluation
- **Decision:** Parse the accession's SEC filing-detail page only when the
  submission header is absent or technically unparseable. A valid header remains
  primary; a valid header that omits the requested identity is evidence, not a
  reason to fall back.
- **Safety rules:** Checksum-pin optional detail pages, record
  `identity_metadata_source`, cross-check both sources when present, and fail
  closed on accession, registrant-name, or series/class disagreement.
- **Measured result:** Real VUSXX and SPY detail pages agree with their valid
  headers. A forced malformed-header evaluation resolves through
  `filing_detail_fallback` without changing the 30-case shadow metrics.

#### M6.1-D005: Report metrics by explicit scenario dimensions

- **Status:** accepted and implemented
- **Decision:** Derive scenario metadata from immutable labels, request
  identity, parsed SEC metadata, and document bytes. Report current and shadow
  metrics by provider, form, requested identity scope, filing metadata breadth,
  HTML/inline-XBRL encoding, reason category, and each expected label.
- **Reason:** Aggregate precision can conceal a failure concentrated in one
  provider, registrant-only path, combined filing, or document encoding.
- **Measured result:** The development corpus includes 27 class and 3
  registrant-only requests, 7 inline-XBRL documents, and 12 filings whose
  metadata covers multiple series. All five shadow review outcomes are the
  intentionally ambiguous supplement cases.

#### M6.1-D006: Freeze an accession-disjoint holdout before evaluation

- **Status:** accepted and implemented
- **Decision:** Label 30 new cases before running the shadow policy and reject
  every accession or document checksum that overlaps the development corpus.
- **Composition:** 25 relevance-positive and 5 same-registrant class-mismatch
  negatives; 11 summaries, 10 statutory prospectuses, 7 supplements, and 2
  SAIs; 16 allowed and 14 disallowed.
- **Measured result:** `m6.1-shadow-v4` has zero false automatic approvals and
  zero missed allowed documents. It matches all complete prospectuses and all
  class-mismatch controls, but routes six positive supplements to review.
- **Boundary:** The holdout is now consumed for model selection. Any `v5`
  changes derived from its disagreements require a new, separately frozen
  follow-up set.

#### M6.1-D007: Add guarded supplement-scope evidence in `v5`

- **Status:** accepted and implemented in shadow mode
- **Decision:** Treat an exact SEC series name in a supplement title,
  front-matter region, heading, or structured table row as strong relevance
  evidence. Also recognize universal all-funds/all-series language only when
  filing metadata contains the requested class and the exact SEC registrant
  name is prominent.
- **Safety rules:** Supplement and SAI document kinds remain ineligible for
  standalone automatic use. Contradictions still override positive signals.
  Generic provider language and body-only identity mentions do not become
  strong merely because the filing is a supplement.
- **Measured result:** `v5` resolves all six conservative supplement misses on
  the consumed `v4` holdout without introducing a false automatic approval.
  The development corpus now has three relevance-label disagreements because
  `v5` treats explicit exact-series fund lists as positive while the historical
  labels called them ambiguous; all three documents remain disallowed
  supplements.

#### M6.1-D008: Validate `v5` on a second disjoint follow-up set

- **Status:** accepted and implemented
- **Decision:** Freeze 30 additional cases with no accession or document
  checksum overlap with either prior corpus, label them before evaluation, and
  leave `v5` unchanged during adjudication.
- **Composition:** 23 relevance-positive, 4 negative, and 3 ambiguous cases
  after one human-label correction; 12 summaries, 10 statutory prospectuses, 7
  supplements, and 1 SAI; 20 allowed, 7 disallowed, and 3 review.
- **Label adjudication:** The initial FOCPX SAI label was corrected from
  negative to positive after the document's later authoritative table was
  found to pair Fidelity OTC Portfolio with FOCPX. The pre-adjudication report
  is preserved locally; no policy rule changed.
- **Measured result:** `v5` produced 100% positive precision, 95.7% positive
  recall, zero false automatic approvals, one missed allowed document, and a
  20% review rate. Nineteen of 20 expected allowed documents were allowed.
- **Activation decision:** Do not activate `v5` yet. Retain it as the next
  shadow baseline because the combined PIMCO `485BPOS` case is incorrectly
  classified as SAI and disallowed.

### Caveats register

#### M6.1-C001: Development-set overfitting

- **Status:** retired by the frozen holdout evaluation
- **Risk:** The same 30 cases were used to discover defects and measure the
  corrected policy, so perfect metrics can reflect post-hoc tuning.
- **Current mitigation:** The scoring behavior introduced in
  `m6.1-shadow-v3` is frozen before selecting new cases. `m6.1-shadow-v4` adds
  only identity-source fallback and provenance; it does not relax relevance or
  automatic-use rules. Normal CLI behavior remains unchanged.
- **Resolution:** Thirty accession- and checksum-disjoint cases were labeled
  before `v4` evaluation. After those cases informed `v5`, a second 30-case set
  was frozen without overlap against either prior corpus and evaluated without
  changing `v5`.

#### M6.1-C002: Broad supplement relevance is under-classified

- **Status:** retired as a V5 activation blocker in Milestone 6.3
- **Risk:** `v4` under-classified explicit fund-list and universal-scope
  supplements. `v5` adds guarded evidence, but a requested fund listed only in
  a later Appendix A still routes to review.
- **Evidence:** `v5` resolves all six original holdout misses. On the new
  follow-up set, the TLT supplement is relevance-positive by its Appendix A but
  remains ambiguous because TLT appears only in body text.
- **Current mitigation:** Review is conservative, and supplements remain
  disallowed even when relevance-positive.
- **Next review:** Add a structured fund-list parser or bounded Appendix
  recognition using additional independently labeled cases.
- **Resolution:** V7 replaced this case-specific gap with bounded cover and
  closed-roster evidence evaluated on fresh cases. Unknown or open-ended lists
  still route to review under M6.3-C003.

#### M6.1-C003: Mixed prospectus and SAI package precedence

- **Status:** retired in Milestone 6.3
- **Risk:** A combined filing document can contain both prospectus and SAI
  sections. The current kind classifier uses the earliest recognized complete
  document marker, so an early SAI section can cause a complete `485BPOS`
  package to be disallowed.
- **Evidence:** The follow-up PIMIX `485BPOS` is relevance-positive and contains
  complete prospectus material, but `v5` classifies it as SAI and misses one of
  20 expected automatic approvals.
- **Current mitigation:** The policy remains shadow-only, so the controlling
  validator does not lose this document.
- **Exit condition:** Represent mixed packages explicitly or require stronger
  top-level package/form evidence before an SAI marker can override complete
  prospectus structure.
- **Resolution:** V6 introduced independent content facets and V7 added
  declaration-style SAI evidence plus document-boundary rules. Fresh V7
  evaluation did not automatically disallow any valid complete document.

#### M6.1-C004: Negative supplement scope is not fully structured

- **Status:** retired as a V5 activation blocker in Milestone 6.3
- **Risk:** A supplement may expressly apply only to other classes or funds
  without naming the requested identity. Absence alone is not a contradiction,
  so `v5` can route a known mismatch to review rather than reject it.
- **Evidence:** The PIMIX Class A/Class C supplement and the AGTHX bond-fund
  supplement are labeled negative but remain ambiguous under `v5`.
- **Current mitigation:** Both route to review and neither can be automatically
  returned as a complete prospectus.
- **Exit condition:** Parse explicit class/fund scope lists and emit a
  contradiction only when the list is demonstrably exhaustive.
- **Resolution:** V7 permits an omission contradiction only on a demonstrably
  closed SEC-series roster. The narrower general rule and its boundary are now
  tracked as M6.3-C003.

### Remaining disagreement audit

The original development corpus is diagnostic rather than independent. Under
`v5`, three historical ambiguous labels become positive because exact series
names appear in explicit fund lists; all remain disallowed supplements. The
first holdout is now consumed for `v5` design and has no remaining `v5`
disagreement.

The independent `v5` follow-up has four adjudicated policy disagreements: one
positive Appendix A supplement routes to review, one complete combined
`485BPOS` is misclassified as SAI and disallowed, and two negative limited-scope
supplements route to review. There are no false automatic approvals. `v5`
therefore remains shadow-only.

### Verification at completion

- All three committed manifests pass the runtime schema.
- The `v5` follow-up has 30 unique accessions and document checksums with no
  overlap against either prior corpus.
- Focused corpus/evidence tests: 35 passed.
- Full offline suite: 127 passed, with the one opt-in live test skipped.
- Opt-in VUSXX/QQQ/SPY live SEC contract: 1 passed.
- All project and test Python files parse under Python 3.9 grammar.

---

## Milestone 6.2: Activation Readiness

**Status:** complete; activation gates not met and all behavior remains
shadow-only

**Goal:** Represent mixed SEC document packages and document scope without
losing safety, prefer narrower verified files when available, and measure an
activation candidate against both adversarial and representative data.

### Caveats register

#### M6.2-C001: The v6 activation candidate misses the recall and review gates

- **Status:** retired by the V7 activation evaluation
- **Risk:** Activating `m6.2-shadow-v6` would send valid complete packages to
  manual review more often than the approved operating target.
- **Independent evidence:** On the 30-case challenge set, v6 produced zero false
  approvals and never disallowed a valid complete package, but automatically
  allowed only 15 of 17 expected complete packages and reviewed 10% of cases.
  On the 50-case representative sample it allowed 35 of 41 expected complete
  packages and reviewed 18% of cases. The approved gates require at least 95%
  valid-complete recall and at most 10% representative review.
- **Current mitigation:** v6 remains report-only. The existing validator still
  controls packages, manifests, and exit codes.
- **Exit condition:** A generalized successor must meet every gate on a new
  accession- and checksum-disjoint evaluation set.
- **Resolution:** V7 met every preserved gate on 80 fresh, disjoint cases and
  is available through explicit staged activation.

#### M6.2-C002: Historical and alternate prospectus layouts remain under-detected

- **Status:** mitigated and superseded by M6.3-C002
- **Risk:** Complete `485BPOS`, closed-end offering, prospectus/proxy, appended
  SAI, and heading variants such as `Fund Summary` can be classified as
  incomplete even when their requested identity evidence is sound.
- **Evidence:** The independent reviews include complete LQD and DODFX
  `485BPOS` packages, BTMFX's `Fund Summary`, a multi-class Fidelity summary,
  and four CIK-only 2003-2009 combined packages.
- **Current mitigation:** Every miss routes to review; none is automatically
  rejected or presented as ready.
- **Exit condition:** Add provider-independent structural evidence, then
  evaluate the changed policy on fresh data rather than reusing the consumed
  v6 corpora as holdouts.
- **Resolution:** V7 added provider-independent cover, section-cluster, and
  document-boundary evidence and reached 97.5% complete-document recall on the
  fresh representative set. Universal layout coverage is not claimed.

#### M6.2-C003: CIK-only form coverage is narrower than the public ticker map

- **Status:** open; owned by Milestone 8 where identity enrichment is required
- **Risk:** A ticker can resolve through `ticker.txt` while exposing no filing
  in the project's five accepted prospectus forms.
- **Evidence:** GLD, SLV, USO, UNG, and UUP all resolved to a CIK but produced
  no selected document during representative-sample construction. Supported
  historical CIK-only cases also required a filing-detail identity fallback
  because modern `-index-headers.html` resources were absent.
- **Current mitigation:** Unsupported cases fail closed. No other SEC form is
  treated as an equivalent prospectus without an explicit policy decision.
- **Exit condition:** Define the intended instrument/form universe and add
  effective-dated CIK-only identity and form rules with independent evidence.

#### M6.2-C004: Scope classification is useful but not activation-critical yet

- **Status:** open; reporting-quality issue
- **Risk:** Phrases such as `each Fund` can make a closed multi-fund list appear
  registrant-wide, and one-fund documents can include several share classes
  even though the narrowest enum value is named `ticker_specific`.
- **Evidence:** Several challenge and representative supplements were correctly
  disallowed but disagreed only on `multi_fund` versus `registrant_wide`.
- **Current mitigation:** Scope is a separate report field and does not upgrade
  an incomplete document. `ticker_specific` is documented as the narrowest
  one-fund/series bucket, not proof that no sibling ticker appears.
- **Exit condition:** Tighten scope grammar against fresh reviewed examples
  before scope affects control behavior.

#### M6.2-C005: Both v6 evaluation sets are now consumed

- **Status:** permanent evaluation boundary
- **Risk:** Tuning from these disagreements and reporting a rerun on the same
  cases as independent evidence would overstate generalization.
- **Current mitigation:** The first-run reports are retained locally, labels
  and checksums are committed, and all v6 behavior remains shadow-only.
- **Exit condition:** Freeze a new disjoint corpus before evaluating any
  successor policy intended for activation.

### Assumptions register

#### M6.2-A001: A complete multi-fund package can satisfy the request

A combined SEC file is an acceptable fallback when it contains complete
prospectus material, has strong evidence for the requested class or instrument,
and no uniquely verified narrower sibling is available. It must be reported as
`multi_fund`, not ticker-specific.

#### M6.2-A002: Document characteristics are not mutually exclusive

One file may contain summary prospectus, statutory prospectus, SAI, and
supplement material. Completeness and automatic-use decisions must use
independent content characteristics rather than whichever phrase appears first.

#### M6.2-A003: Review-rate denominators have different meanings

The difficult curated corpus measures correctness under stress. It does not
estimate operational review volume. Activation therefore reports valid-complete
document recall on labeled challenge data and review rate on a separately
defined representative sample.

### Decisions register

#### M6.2-D001: Prefer a uniquely verified narrow sibling

- **Status:** approved and implemented in shadow policy
- **Decision:** Rank a verified ticker-specific file above a verified multi-fund
  package. Use the multi-fund package only when no uniquely qualified narrower
  sibling exists.
- **Safety boundary:** Zero or multiple equally narrow qualified siblings remain
  review. Candidate evaluation errors prevent a uniqueness claim.

#### M6.2-D002: Add a backward-compatible content profile

- **Status:** approved and implemented in corpus schema v2 and report schema v3
- **Decision:** Preserve `document_kind` for compatibility while adding explicit
  flags for summary prospectus, statutory prospectus, SAI, and top-level
  supplement content. Add a combined-package kind as a derived reporting label,
  not as a replacement for the underlying flags.

#### M6.2-D003: Report document scope explicitly

- **Status:** approved and implemented
- **Decision:** Classify scope as `ticker_specific`, `multi_fund`,
  `registrant_wide`, or `unknown`. Filing-level class metadata alone cannot make
  a file ticker-specific.

#### M6.2-D004: Use only demonstrably closed lists as contradictions

- **Status:** approved and implemented
- **Decision:** A structured Appendix or fund list can establish positive scope
  when it contains the requested exact ticker or SEC series name. Absence is a
  contradiction only when the document itself clearly defines the list as
  exhaustive. Otherwise the result remains review.

#### M6.2-D005: Keep v6 shadow-only through independent validation

- **Status:** approved and upheld
- **Decision:** The existing validator and package builder remain authoritative.
  Any `v6` policy is evaluated on new accession- and checksum-disjoint data
  before an activation decision.
- **Activation gates:** zero false automatic approvals, zero valid complete
  documents automatically disallowed, at least 95% valid-complete-document
  recall, and at most 10% review on a representative operational sample.

### Verification at completion

- Corpus schema v2 records independent content characteristics and document
  scope while continuing to parse all schema-v1 manifests.
- Focused tests cover mixed packages, pure SAI, Appendix and exhaustive lists,
  scope, candidate preference, ties, errors, and report confusion matrices.
- Historical development, holdout, and v5-follow-up results were used only as
  diagnostics.
- `v6_challenge_manifest.json` freezes 30 new cases with no accession or
  document-checksum overlap against the prior 90 cases.
- `v6_representative_manifest.json` freezes 50 additional cases with no overlap
  against the prior 120. Its committed provenance records seed 6202, the
  45-case mutual-fund sample, the five CIK-only cases, and unsupported attempts.
- Both manifests were labeled before their first v6 evaluation. No v6 rule was
  changed after either first-run report.
- v6 passed both safety gates but failed both coverage gates, so it was not
  activated.
- Full deterministic suite: 145 passed, with the one opt-in live test skipped.
- Opt-in VUSXX/QQQ/SPY SEC contract: 1 passed.
- All 34 project and test Python files parse under Python 3.9 grammar.

---

## Milestone 6.3: Coverage Generalization

**Status:** complete; V7 is available only through explicit staged activation

**Goal:** Generalize the V6 coverage fixes, measure them once on fresh V7 data,
and make an explicit activation decision before beginning persistent review
operations.

### Caveats register

#### M6.3-C001: V6 disagreements are development data

- **Status:** preserved
- **Risk:** Rules designed from V6 misses can appear successful when rerun on
  those same documents without generalizing to new layouts.
- **Current mitigation:** V6 is diagnostic only for V7 development. Activation
  evidence must come from new accession- and checksum-disjoint corpora whose
  labels are frozen before the first V7 evaluation.
- **Completion evidence:** V7 was evaluated once on 80 newly frozen cases with
  no accession, URL, or document-checksum overlap against prior corpora. No V7
  rule was changed after the first reports.

#### M6.3-C002: Complete-document structure varies across filing generations

- **Status:** mitigated, not universally eliminated
- **Risk:** Modern HTML, untagged legacy text, prospectus/proxy statements, and
  files with an appended SAI use different headings and document boundaries.
  Literal title matching can miss complete documents.
- **Current mitigation:** Require a prospectus cover marker plus a cluster of
  substantive sections, and require declaration-style SAI evidence before
  treating a later SAI reference as appended content.
- **Completion evidence:** Complete-document recall reached 100% on the fresh
  challenge and 97.5% on the fresh representative sample without a false
  approval or automatic disallowance of a valid complete document.

#### M6.3-C003: Class-cover omission is safe only for a closed roster

- **Status:** enforced rule boundary
- **Risk:** Absence of a ticker from arbitrary document text is not evidence of
  exclusion. Treating it as one could reject a valid multi-class filing.
- **Current mitigation:** Use omission only when a summary-prospectus cover for
  the exact SEC series lists one or more known sibling class tickers and no
  cover segment names the requested ticker.
- **Completion evidence:** All five fresh same-registrant class-mismatch
  controls were rejected, and both V7 corpora had zero false automatic
  approvals.

#### M6.3-C004: Accepted-form expansion remains unresolved

- **Status:** deferred to Milestone 8
- **Risk:** GLD, SLV, USO, UNG, UUP, and similar CIK-resolved instruments may
  not file any of the five currently accepted prospectus forms.
- **Current mitigation:** Continue failing closed. Milestone 6.3 must not call
  another form a prospectus merely to increase coverage.
- **Exit condition:** Milestone 8 defines the supported instrument/form matrix
  with effective-dated identity evidence.

#### M6.3-C005: V7 adds filing-identity metadata to the live dependency path

- **Status:** open operational dependency; handed to Milestone 7
- **Risk:** V7 needs the SEC filing submission header, or the filing-detail
  fallback for older filings. A transient endpoint failure can prevent an
  otherwise valid document from being automatically verified.
- **Current mitigation:** Metadata is cached by accession for the CLI run.
  Missing or unparseable identity evidence fails closed to
  `manual_review_required`; it never falls back to a legacy automatic approval.
- **Exit condition:** Milestone 7 persists immutable identity artifacts and
  supports resumable retry before human review.

### Assumptions register

#### M6.3-A001: Structural evidence should be provider-independent

Complete-document detection may use SEC form metadata, cover declarations,
substantive section clusters, and document boundaries. It must not contain
provider names, requested tickers, or accession-specific exceptions.

#### M6.3-A002: Common legal-name abbreviations are equivalent

For cover matching, legal-name tokens such as `Co`/`Company`,
`Corp`/`Corporation`, and `Inc`/`Incorporated` are treated as equivalent.
Identifier contradictions still take priority over name agreement.

### Decisions register

#### M6.3-D001: Preserve the V6 activation gates

- **Status:** approved
- **Decision:** V7 must produce zero false automatic approvals, zero valid
  complete documents automatically disallowed, at least 95% valid-complete
  recall, and at most 10% review on a representative sample.

#### M6.3-D002: Require staged activation

- **Status:** approved
- **Decision:** Passing V7 may enable a feature-flagged control path with
  rollback. It does not justify deleting the current validator or its reports.

#### M6.3-D003: Activate V7 only by explicit policy selection

- **Status:** implemented
- **Decision:** `--validation-policy v7` (or
  `PROSPECTUS_VALIDATION_POLICY=v7`) enables the evaluated policy.
  `--validation-policy legacy` remains the default and rollback. Each output
  manifest records the policy and exact policy version.

#### M6.3-D004: Fail closed when V7 identity evidence is unavailable

- **Status:** implemented
- **Decision:** A technical failure while retrieving or parsing filing identity
  metadata downgrades the document to manual review. It does not silently reuse
  the legacy validator's automatic approval.

### Verification at completion

- Frozen V7 challenge: 30 cases, five negative controls, 14/14 complete
  documents automatically allowed, zero false approvals, 10% review.
- Frozen V7 representative: 50 cases, 39/40 complete documents automatically
  allowed, zero false approvals, 4% review.
- Cross-corpus integrity: 80 unique V7 accessions, URLs, and document checksums;
  zero overlap with all five prior corpora.
- Deterministic suite: 159 passed, with the one opt-in live test skipped.
- Live staged-policy smoke test: VUSXX, QQQ, and SPY all produced verified V7
  packages; QQQ included a review-required supplement and a verified
  date-linked base.

---

## Milestone 7.1: Durable Local Operations Contract

**Status:** complete

**Goal:** Prove persistent, idempotent, resumable job execution and durable
review-task creation before adding PostgreSQL, Temporal, or an API.

### Caveats register

#### M7.1-C001: SQLite does not prove production database concurrency

- **Status:** open; handed to Milestone 7.2
- **Risk:** SQLite transaction and locking behavior differs from PostgreSQL.
  Passing local lease/idempotency tests does not prove correct work claiming
  across multiple pods.
- **Current mitigation:** Keep persistence behind a repository boundary and
  make state transitions and uniqueness rules explicit. Use SQLite only as a
  local/test reference adapter.
- **Exit condition:** Implement the same contract with PostgreSQL migrations,
  row-level contention tests, and database-enforced work claiming.

#### M7.1-C002: Local artifact paths are not durable object references

- **Status:** open; handed to Milestone 7.2
- **Risk:** A database record can outlive or move away from its local HTML,
  manifest, or PDF file.
- **Current mitigation:** Persist source URLs, roles, sizes, and SHA-256
  checksums with every path. Do not represent a local path as an S3 URL or
  service-stable artifact ID.
- **Exit condition:** Add an artifact-store interface and immutable object keys,
  then verify object checksums before a result is published.

#### M7.1-C003: Review resolution semantics require a product decision

- **Status:** open; queue creation only
- **Risk:** “Approve” could mean accepting machine evidence, creating a human
  override, selecting another document, or authorizing downstream use. Treating
  these as one action would weaken the audit model.
- **Current mitigation:** M7.1 may create and list pending review tasks, but it
  must not implement reviewer decisions or upgrade
  `manual_review_required`.
- **Exit condition:** Define allowed decisions, actor identity, authorization,
  downstream effect, and whether a newer machine run supersedes an open task.

#### M7.1-C004: Lease recovery assumes synchronized UTC clocks

- **Status:** open operational assumption
- **Risk:** A worker with a materially incorrect clock could reclaim active
  work early or delay recovery of abandoned work.
- **Current mitigation:** Store all timestamps in normalized UTC, require a
  positive bounded lease duration, and treat lease expiry as an execution
  claim only, never as correctness evidence.
- **Exit condition:** Temporal or the production database becomes the
  authoritative lease/workflow clock, with clock-skew monitoring where
  applicable.

#### M7.1-C005: Local failures do not have stage-aware durable retries

- **Status:** open; handed to Milestone 7.3
- **Risk:** `SECClient` handles bounded HTTP retries, but a ticker-level
  exception recorded by the local runner becomes terminal. It cannot resume
  from an individual resolve, discovery, validation, or storage stage.
- **Current mitigation:** One ticker failure does not stop the rest of the job;
  interrupted `running` items can be reclaimed after lease expiry; and a new
  job can intentionally retry a failed ticker.
- **Exit condition:** Temporal activities own typed retry policies and persist
  completed stage outputs so a retry does not repeat unrelated work.

### Assumptions register

#### M7.1-A001: PostgreSQL remains the production state store

SQLite is introduced only because it is deterministic, dependency-free, and
available in local tests. The repository contract and schema vocabulary should
map directly to PostgreSQL; SQLite is not the target deployment recommendation.

#### M7.1-A002: At-least-once attempts are acceptable when effects are idempotent

An interrupted worker may execute an external read again after its lease
expires. Database uniqueness and immutable artifact checksums must prevent that
retry from creating conflicting logical results.

#### M7.1-A003: Machine evidence and human review are separate records

Review tasks and later decisions are additive audit data. They must not rewrite
the package manifest, validation signals, contradictions, policy version, or
artifact checksum that caused review.

### Decisions register

#### M7.1-D001: Freeze persistence contracts before infrastructure

- **Status:** approved by implementation sequence
- **Decision:** Define job, item, artifact, lease, and review records first.
  PostgreSQL and Temporal adapters must implement these contracts rather than
  introducing a second state model.

#### M7.1-D002: Use explicit caller-owned idempotency keys

- **Status:** approved for M7.1
- **Decision:** A repeated key returns the same job only when normalized
  tickers and validation policy have the same request fingerprint. Reusing a
  key for a different request is an explicit conflict.

#### M7.1-D003: Do not change the submitted CLI yet

- **Status:** approved for M7.1
- **Decision:** The local persistent runner is an application service exercised
  by deterministic tests. CLI flags and asynchronous API behavior are added
  only after the storage contract is stable.

### Verification at completion

- Schema migration is idempotent across reopen and rejects a database whose
  recorded schema version is newer than this code understands.
- An explicit idempotency key returns the existing job only for the same
  normalized ticker sequence and validation policy; conflicting reuse fails.
- Expired work can be reclaimed, the old owner cannot finalize it, and two
  SQLite connections do not claim the same active item.
- Mixed verified, review-required, and failed ticker outcomes are persisted
  independently, with aggregate job status derived from terminal item states.
- Successful results persist the complete manifest, identity level, policy
  version, and verified artifact bytes/checksums. Missing manifests, policy
  mismatches, and checksum drift fail closed.
- A successful non-verified package creates exactly one pending review task;
  rerunning a terminal job does not retrieve its tickers again.
- Focused operations suite: 13 passed. Full deterministic suite: 172 passed
  with the one opt-in live SEC test skipped. All 37 Python source and test files
  parse under Python 3.9 grammar.

---

## Future milestone template

For each new milestone, add:

1. Status and goal.
2. Caveats register with risk, evidence, mitigation, and exit condition.
3. Assumptions introduced without an explicit requirement.
4. Decisions made or still open.
5. Planned work handed to later milestones.
