# Correctness Model

This project separates three questions that can look equivalent but are not:

1. Did the ticker resolve to an SEC entity?
2. Is the selected filing associated with the requested registrant, series, or class?
3. Does the downloaded document itself contain evidence that it covers the requested ticker or share class?

A successful HTTP download answers none of these questions by itself. The V2
model records identity provenance separately from document-content verification
so downstream users can see what is known and what still requires review.

## Identity levels

`IdentityLevel` records the strongest SEC relationship actually used to select
the candidate filing. The existence of a stronger identifier in a mapping file
does not count unless the selection path uses or independently confirms it.

| Level | What it proves | What it does not prove |
|---|---|---|
| `class` | SEC associated the candidate filing with the requested class/contract ID. | That the chosen file explicitly covers the ticker or is a complete prospectus. |
| `series` | SEC associated the candidate filing with the requested fund series. | Which share class within that series the document covers. |
| `registrant` | The filing belongs to the ticker's registrant CIK. | Which series or class the filing covers when the registrant contains multiple products. |
| `unknown` | No identity claim has been established yet. | Any fund-level relevance. |

The current implementation prefers the class feed when a class ID exists. If
that feed and its per-form queries contain no qualifying prospectus, it falls
back to the series feed and records the downgrade. Registrant submissions are
used only when neither class nor series identifiers are available.

A request or parsing failure is not treated as an empty class feed. It propagates
as an error so a temporary infrastructure problem cannot silently lower the
identity level.

## Document verification

`DocumentVerification` is independent of identity level:

| Status | Meaning |
|---|---|
| `not_checked` | The file was not inspected for share-class evidence. This remains the EDGAR selection layer's state before package validation. |
| `document_verified` | Content checks found sufficient evidence that the file covers the requested ticker or class. |
| `manual_review_required` | Automated evidence is absent, conflicting, incomplete, or ambiguous. |
| `rejected` | Reserved for affirmative evidence that the file does not cover the requested ticker or class; absence alone routes to review. |

The current validator classifies visible HTML text as a summary prospectus,
statutory prospectus, supplement, or unknown. A complete prospectus needs a
recognized title plus at least two expected sections. Direct ticker or class-ID
evidence must appear in the first 20,000 normalized visible characters; this
reduces, but does not eliminate, incidental matches.

A supplement is not treated as a standalone complete prospectus. The package
builder exhausts the filing metadata exposed for the same identity scope,
evaluates candidates nearest explicitly referenced prospectus dates first, and
requires a complete base with direct identity evidence. The relationship is
verified only when the supplement and base share the requested identity and a
referenced prospectus date. Otherwise the package is
`manual_review_required`.

If the filing-designated primary document fails automatic-use rules, the
package builder inventories the accession through `index.json`, uses the SEC
filing document table to exclude indexes, XBRL renderings, exhibits, and
unsupported forms, and validates every remaining sibling. A recovered sibling
is selected automatically only when exactly one candidate qualifies. Zero or
multiple qualifying siblings require manual review.

The manifest records the resolved CIK/series/class identifiers, mapping source,
SEC document URL, accession, filing date, byte size, and SHA-256 checksum for
each saved artifact. Schema version 2 also records recovery search coverage,
stopping reasons, and every evaluated candidate's evidence and disposition.

SEC evidence is authoritative. Official issuer or exchange information may be
used only as approved corroboration; it cannot override conflicting SEC evidence
or independently establish an SEC class/series relationship.

## Strict invariants

- Resolving a class ID does not produce class-level confidence unless the filing lookup uses or verifies that ID.
- A `primaryDocument` value identifies the filing's designated main file; it is not proof of ticker coverage or completeness.
- A form such as `497K` is a ranking signal, not a reliable content classifier. It may be a summary prospectus or a supplement.
- Series-level relevance must not be described as exact share-class relevance.
- Registrant-level selection must not be described as fund-specific when the registrant can contain multiple series.
- Choosing the largest HTML file is a fallback heuristic and must remain visible as a warning.
- File size and archive order must never break a sibling-recovery tie.
- Recovery must not broaden class identity to series or registrant identity.
- Exactly one sibling must satisfy direct-identity and content rules before automatic replacement.
- Download success must not imply `document_verified`.
- A malformed SEC response must not be interpreted as an empty feed or trigger a lower-confidence fallback.
- A review-required package remains retrievable but must not produce the all-verified process exit code.

## Concrete examples

- **VUSXX:** class `C000005732` selects the filing at `class` identity. Its HTML is classified as a summary prospectus and directly contains `VUSXX`, producing `document_verified`.
- **SPY:** the fallback mapping provides only a registrant CIK, so identity remains `registrant`. Its complete 485BPOS directly contains `SPY`, allowing independent document verification without upgrading identity.
- **QQQ:** the latest preferred `497K` is a supplement. The result package includes that supplement and the date-linked December 2025 summary prospectus, demonstrating why form priority cannot replace content validation.

## Milestone boundaries

Milestone 1 introduced the vocabulary, model fields, and evidence containers.
Milestone 2 implemented class-first lookup, explicit series fallback, identity
provenance, and regression tests. Milestone 3 implements deterministic content
classification, direct identity evidence, supplement/base packages, manifests,
and manual-review routing. Milestone 4 validates every consumed SEC response
shape and separates review-required output from success. Milestone 5 removes
fixed discovery limits, follows SEC history pagination, searches supplement
dates first, and implements strict accession sibling recovery with provenance.
