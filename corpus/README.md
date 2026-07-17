# Validation Corpus

Milestone 6 adds a versioned, human-labeled SEC corpus for measuring document
identity and completeness policies before they control retrieval output.

`manifest.json` contains 30 immutable cases. Each case records the filing and
requested identities separately, SEC document and submission-header URLs,
SHA-256 checksums, three independent labels, and the evidence behind the human
label. Raw SEC files and generated reports are intentionally ignored by Git.

## Labels

- `relevance`: `positive`, `negative`, or `ambiguous` ticker/class coverage.
- `document_kind`: `summary_prospectus`, `statutory_prospectus`, `supplement`,
  or `unknown`.
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
```

The cache is written under `corpus/cache/`. The detailed report is written to
`corpus/reports/evaluation.json`; it includes per-case evidence, disagreements,
confusion matrices, precision, recall, false-verification counts, and review
rate.

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
