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
| M3-C001 | open | Deterministic content rules can misclassify unfamiliar SEC documents. | Expand the versioned validation corpus. |
| M3-C003 | mitigated | Position-limited identifier evidence reduces but cannot eliminate false positives/negatives. | Evidence policy in Roadmap Milestone 6. |
| M5-C001 | mitigated | Real SEC archive parsing is live-tested, but no stable live fixture currently triggers sibling replacement. | Curated contract matrix maintenance. |

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

- **Status:** open
- **Risk:** Unusual titles, section names, encodings, combined filings, or new
  SEC document layouts can produce false `unknown`, summary, statutory, or
  supplement classifications.
- **Evidence:** Deterministic fixtures cover each branch and live validation
  covers VUSXX, QQQ, and SPY, but this is not a representative fund universe.
- **Current mitigation:** Unknown or incomplete evidence routes to
  `manual_review_required`; no language model is allowed to silently upgrade
  confidence.
- **Exit condition:** Build a versioned, provider-diverse labeled corpus and
  measure false-positive and false-negative rates before changing thresholds.

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
- **Exit condition:** Add exact fund/share-class names, structured class tables,
  cover-page locality, and negative examples to a measured evidence policy.

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

## Future milestone template

For each new milestone, add:

1. Status and goal.
2. Caveats register with risk, evidence, mitigation, and exit condition.
3. Assumptions introduced without an explicit requirement.
4. Decisions made or still open.
5. Planned work handed to later milestones.
