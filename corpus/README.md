# Validation Corpus

Milestone 6 adds a versioned, human-labeled SEC corpus for measuring document
identity and completeness policies before they control retrieval output.

`manifest.json` contains 30 immutable development cases. Each case records the filing and
requested identities separately, SEC document and submission-header URLs,
optional SEC filing-detail fallback URLs, SHA-256 checksums, three independent
labels, and the evidence behind the human label. Raw SEC files and generated
reports are intentionally ignored by Git.

`holdout_manifest.json` contains 30 separately labeled cases with no accession
or document-checksum overlap with the development corpus. It was labeled before
the frozen `m6.1-shadow-v4` policy was evaluated.

`v5_followup_manifest.json` contains a second 30-case set with no accession or
document-checksum overlap against either earlier corpus. It was labeled before
`m6.1-shadow-v5` was evaluated and is the independent activation check for that
policy version.

## Labels

- `relevance`: `positive`, `negative`, or `ambiguous` ticker/class coverage.
- `document_kind`: `summary_prospectus`, `statutory_prospectus`, `supplement`,
  `statement_of_additional_information`, or `unknown`.
- `automatic_use`: `allowed`, `disallowed`, or `review` as a standalone result.

An applicable supplement can therefore be relevant while still being
disallowed for standalone use. Every ambiguous case identifies what evidence is
missing and what review would resolve it.

## Run

```bash
# Download SEC bytes and verify every committed checksum
python -m prospectus_fetcher.corpus_cli fetch

# Evaluate from the verified local cache
python -m prospectus_fetcher.corpus_cli evaluate

# Perform both operations
python -m prospectus_fetcher.corpus_cli all

# Evaluate the independently labeled holdout
python -m prospectus_fetcher.corpus_cli all \
  --manifest corpus/holdout_manifest.json \
  --report corpus/reports/holdout-evaluation.json

# Evaluate the independent v5 follow-up
python -m prospectus_fetcher.corpus_cli all \
  --manifest corpus/v5_followup_manifest.json \
  --report corpus/reports/v5-followup-evaluation.json
```

The cache is written under `corpus/cache/`. The detailed report is written to
`corpus/reports/evaluation.json`; it includes per-case evidence, disagreements,
confusion matrices, precision, recall, false-verification counts, and review
rate. Report schema v2 also includes per-scenario metrics for provider, form,
requested identity scope, filing metadata breadth, document encoding, reason
category, and each expected label.

## Initial Baseline

The 2026-07-16 corpus contains 20 positive, 5 negative, and 5 ambiguous cases,
with 30 unique document checksums and 30 unique filing-header checksums.

| Policy | Positive precision | Positive recall | False automatic approvals | Missed automatic approvals | Review rate |
|---|---:|---:|---:|---:|---:|
| Current validator | 94.1% | 80.0% | 0 | 6 | 40.0% |
| `m6-shadow-v1` | 100.0% | 90.0% | 0 | 3 | 30.0% |

These figures describe only this deliberately difficult 30-case corpus. They
are evidence for iteration, not estimates of universal production accuracy.
The shadow policy does not affect normal CLI packages, manifests, or exit codes.

## Milestone 6.1 Progress

The corpus version was advanced when SAI became an explicit document kind.
`m6.1-shadow-v1` correctly classified the TRBCX control as
relevant SAI material that is disallowed as a standalone prospectus. This
reduced the shadow review rate to 26.7%, with positive precision, recall, and
false-approval counts unchanged.

`m6.1-shadow-v2` adds a shadow-only registrant-instrument signal for CIK-only
requests. It requires matching requested/filing CIKs, no known mutual-fund
series, a prominent exact SEC registrant name, the exact ticker, complete
prospectus structure, and no contradiction. Both previously missed SPY cases
now match their labels. On the current corpus, shadow positive precision and
recall are 100%, false automatic approvals remain zero, one expected automatic
approval is still missed, and review rate is 20.0%.

`m6.1-shadow-v3` separates browser-visible body text, the HTML title, hidden
inline-XBRL facts, and the filing's declared form. It resolves the Fidelity and
combined Vanguard document-kind disagreements without allowing hidden tickers
or form type alone to establish correctness. The policy now matches every label
on the 30-case development corpus: 100% relevance precision/recall, zero false
or missed automatic approvals, and a 16.7% review rate representing the five
intentionally ambiguous cases.

`m6.1-shadow-v4` keeps the `v3` scoring rules and adds a technical identity
fallback. Submission headers remain primary. When a header is absent or
unparseable, a checksum-pinned SEC filing-detail page may provide registrant,
series, and class metadata. When both sources are available they must agree, and
the report records `identity_metadata_source`. Real VUSXX and SPY detail pages
cross-check successfully; a malformed-header integration test exercises the
fallback branch. No real case currently needs fallback, so its observed corpus
rate is 0/30.

The same report slices the development measurements instead of relying only on
aggregate scores. The current corpus contains 27 class requests, 3
registrant-only requests, 7 inline-XBRL documents, and 12 filings whose metadata
covers multiple series. Every one of the five shadow review outcomes is an
intentionally ambiguous supplement case.

The development result is not independent validation because those cases
exposed the defects used to design the scoring rules.

## Frozen Holdout Result

The 30-case holdout contains 25 relevance-positive cases, 5 same-registrant
class-mismatch negatives, 11 summaries, 10 statutory prospectuses, 7
supplements, and 2 SAIs. Sixteen cases are allowed and 14 are disallowed.

`m6.1-shadow-v4` allowed all 16 valid complete prospectuses, rejected every
class-mismatch control, produced zero false automatic approvals, and missed no
allowed document. Positive relevance precision was 100% and recall was 76%.
Six relevance-positive supplements were conservatively routed to review, for a
20% review rate.

This holdout is now consumed: it may guide a future `v5`, but a new follow-up set
is required to evaluate resulting rule changes independently.

## V5 Follow-Up Result

`m6.1-shadow-v5` adds guarded supplement-scope evidence. An exact SEC series
name can establish relevance when it appears in a supplement title,
front-matter region, heading, or structured table row. Universal
all-funds/all-series scope requires matching filing metadata and the exact SEC
registrant name. Supplements and SAIs remain disallowed as standalone
prospectuses.

The 30-case follow-up contains 23 relevance-positive, 4 negative, and 3
ambiguous cases after adjudication, with 12 summaries, 10 statutory
prospectuses, 7 supplements, and 1 SAI. Twenty cases are allowed, 7 are
disallowed, and 3 require review.

One initial human label was corrected without changing the policy: the FOCPX
SAI begins with Class K material but later contains an authoritative
fund/ticker table that explicitly pairs Fidelity OTC Portfolio with FOCPX. The
final label is relevance-positive and standalone-use-disallowed.

Final independent measurements:

| Policy | Positive precision | Positive recall | False automatic approvals | Missed automatic approvals | Review rate |
|---|---:|---:|---:|---:|---:|
| `m6.1-shadow-v5` | 100.0% | 95.7% | 0 | 1 | 20.0% |

The remaining four disagreements are actionable rather than aggregate noise:

- a TLT supplement lists the fund only in a later Appendix A and routes to
  review;
- a combined PIMCO `485BPOS` is misclassified as SAI and disallowed;
- two limited-scope supplements that exclude the requested fund/class route to
  review rather than explicit rejection.

`v5` remains shadow-only. The combined PIMCO miss is an activation blocker even
though no false automatic approval occurred.
