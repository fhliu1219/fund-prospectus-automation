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
| `not_checked` | The file was not inspected for share-class evidence. This remains the default through Milestone 2. |
| `document_verified` | Content checks found sufficient evidence that the file covers the requested ticker or class. |
| `manual_review_required` | Automated evidence is absent, conflicting, incomplete, or ambiguous. |
| `rejected` | Content evidence shows that the file does not cover the requested ticker or class. |

Possible future evidence signals include the requested ticker, exact share-class
name, class/contract ID, a class table containing that class, or language saying
the document applies to all classes of the verified series. The validation policy
will require a defensible combination rather than trusting one incidental string.
A supplement must also be linked to a base prospectus that covers the class;
form type alone is not enough.

## Strict invariants

- Resolving a class ID does not produce class-level confidence unless the filing lookup uses or verifies that ID.
- A `primaryDocument` value identifies the filing's designated main file; it is not proof of ticker coverage or completeness.
- A form such as `497K` is a ranking signal, not a reliable content classifier. It may be a summary prospectus or a supplement.
- Series-level relevance must not be described as exact share-class relevance.
- Registrant-level selection must not be described as fund-specific when the registrant can contain multiple series.
- Choosing the largest HTML file is a fallback heuristic and must remain visible as a warning.
- Download success must not imply `document_verified`.

## Concrete examples

- **VUSXX:** the resolver knows class `C000005732` and series `S000002233`. A successful class-feed selection now produces `class` identity while document status remains `not_checked`.
- **SPY:** the fallback mapping provides only a registrant CIK. The current selection is `registrant` until filing metadata or document content establishes stronger evidence.
- **QQQ:** its latest preferred `497K` can be a supplement. This demonstrates why form priority cannot replace content validation.

## Milestone boundaries

Milestone 1 introduced the vocabulary, model fields, and evidence containers.
Milestone 2 implements class-first lookup, explicit series fallback, identity
provenance, and regression tests. Milestone 3 will inspect filing metadata and
document content, assign document verification, and route ambiguous cases to
manual review.
