# Correctness Contract

This document defines what the system may claim about a retrieval result. It is
not a roadmap or implementation history. The README explains operation, while
`CAVEATS.md` records residual risks, assumptions, and design decisions.

Four questions must remain separate:

1. Did the ticker resolve to an SEC entity?
2. At what SEC identity level was the filing selected?
3. Does the document contain evidence that it covers the requested instrument?
4. Does the saved package contain a complete prospectus suitable for automatic use?

A successful HTTP download answers none of these questions by itself.

## Identity confidence

`IdentityLevel` records the strongest SEC relationship actually used to select
the filing. Merely discovering an identifier in a mapping does not establish
that level.

| Level | Established claim | Not established |
|---|---|---|
| `class` | SEC associated the candidate filing with the requested class/contract ID. | That the selected file is complete or explicitly covers the ticker. |
| `series` | SEC associated the candidate filing with the requested fund series. | Which share class within that series the file covers. |
| `registrant` | The filing belongs to the ticker's registrant CIK. | Which series or class it covers when the registrant has multiple products. |
| `unknown` | No supported identity relationship was established. | Any fund-level relevance. |

The lookup prefers a class feed when a class ID is available. It may fall back
to the series feed only after valid responses establish that no qualifying
class filing exists, and the downgrade must be recorded. Registrant submissions
are used only when class and series identifiers are unavailable.

Content evidence can verify the document independently, but it does not upgrade
the filing's identity level. For example, a registrant-selected document may be
`document_verified` while identity remains `registrant`.

## Content and completeness

`DocumentKind` describes the content found in the file. A file may contain
overlapping characteristics, so one label is not a complete description of
every section. The supported labels include summary prospectus, statutory
prospectus, combined prospectus package, supplement, statement of additional
information, and unknown.

A form code is a filing-selection signal, not a content guarantee. In
particular, `497K` may contain a complete summary prospectus or only a
supplement.

A package is complete in one of two ways:

- one verified document contains complete prospectus material; or
- a relevant supplement is paired with a verified complete base prospectus and
  the supplement's referenced prospectus date matches the base.

A supplement alone is never treated as a complete package.

## Document verification

`DocumentVerification` is independent of identity level and document kind:

| Status | Meaning |
|---|---|
| `not_checked` | The EDGAR selection layer has not inspected the file's content. |
| `document_verified` | The controlling policy found sufficient relevance and completeness evidence, with no disqualifying contradiction. |
| `manual_review_required` | Evidence is absent, incomplete, conflicting, ambiguous, or could not be evaluated reliably. |
| `rejected` | The controlling policy found affirmative contradictory evidence or determined that the file cannot serve as the requested prospectus. |

The manifest records both `validation_policy` and
`validation_policy_version`; a verification claim is meaningful only together
with those fields.

- `legacy` uses deterministic content structure plus direct ticker/class
  evidence and remains the rollback policy.
- `v7` adds filing-specific SEC identity metadata, location-aware evidence,
  contradictions, and document scope. If required identity metadata cannot be
  evaluated, it fails closed to `manual_review_required`.

Neither policy is a legal determination. A new policy version must be evaluated
on accession- and checksum-disjoint labeled data before its results are treated
as equivalent to an existing version.

## Document scope

`DocumentScope` describes how broadly the file applies. It is separate from
both filing identity and document verification.

| Scope | Meaning |
|---|---|
| `ticker_specific` | The narrowest supported bucket: one fund or series containing the requested class. The file may still cover sibling share classes and tickers. |
| `multi_fund` | The file contains material for multiple identified funds or series. |
| `registrant_wide` | The file states or demonstrates registrant-wide applicability. |
| `unknown` | The policy cannot establish scope confidently. |

Filing-level class association alone cannot make a document
`ticker_specific`. V7 computes scope when making its automatic-use decision,
and corpus reports expose it explicitly. The current package manifest records
the resulting evidence and verification decision but does not yet expose scope
as a top-level package field.

## Selection and recovery

Candidate selection first uses the strongest available identity scope, then the
configured form priority, then filing recency within the winning form. Recency
does not override a higher-priority form.

`primaryDocument` means SEC designated the file as the filing's main document.
It does not prove ticker relevance or completeness. If that file fails
automatic-use rules, recovery inventories the same accession through
`index.json`, excludes indexes, XBRL renderings, exhibits, unsafe names, and
unsupported document types, and evaluates every eligible sibling.

Automatic sibling replacement requires exactly one qualifying candidate and no
candidate evaluation errors. Zero candidates, multiple candidates, or an
incomplete evaluation require manual review. File size and archive order cannot
break the tie. The older largest-HTML resolution heuristic is permitted only as
a visible fallback before content validation; it does not establish
correctness.

Temporary request failures and malformed SEC responses are errors, not evidence
that a higher-confidence feed is empty. Recovery must never silently broaden a
class-selected filing to series or registrant scope.

## Result and publication boundary

`status="ok"` means a package was retrieved and saved. It does not by itself
mean the package is verified. Callers must inspect `document_verification`:

- all packages verified -> CLI exit code `0`;
- retrieval or processing failure -> exit code `1`;
- invalid CLI usage -> exit code `2`;
- at least one saved package requires review or is rejected -> exit code `3`.

Every saved package has a manifest containing selection provenance, identity
evidence, policy and verification fields, source URLs, artifact sizes and
SHA-256 checksums, warnings, and recovery decisions.

The durable operations path additionally refuses a successful result without a
consistent manifest, preserves `review_required` as a distinct terminal state,
and verifies immutable artifact metadata before recording completion. These
storage checks preserve evidence integrity; they do not create stronger fund or
document claims.

## Strict invariants

- Identity confidence reports the selection path actually used.
- Series identity is not exact share-class identity.
- Registrant identity is not fund-specific identity.
- Download success and `primaryDocument` do not imply verification.
- Form type, file size, and archive order do not prove document content.
- Missing evidence routes to review; affirmative contradictions may reject.
- A supplement requires a verified, date-linked complete base.
- Recovery preserves identity scope and requires a unique qualifying sibling.
- External schema failures cannot trigger a silent confidence downgrade.
- SEC evidence is authoritative; issuer or exchange evidence may corroborate
  but cannot override conflicting SEC evidence or manufacture SEC identity.
- Review-required output remains retrievable but cannot be reported as
  all-verified success.

## Explicit non-guarantees

- SEC ticker mappings do not cover every possible instrument.
- `ticker_specific` does not mean that no sibling share class appears.
- A CIK-only result does not establish class or series identity.
- Deterministic HTML rules cannot recognize every valid historical layout.
- The five accepted prospectus forms do not cover every exchange-traded product.
- SEC availability, schema stability, and source-data accuracy are external
  dependencies.

## Reference examples

- **VUSXX:** class `C000005732` establishes class-level filing identity; the
  complete document contains sufficient direct evidence for verification.
- **SPY:** only registrant identity is available, but its complete 485BPOS can
  be independently verified without upgrading identity.
- **QQQ:** the preferred `497K` is a supplement, so automatic verification
  requires the related complete base prospectus. This is the concrete reason
  form priority cannot substitute for content validation.
