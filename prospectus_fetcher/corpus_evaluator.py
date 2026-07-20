"""Evaluate current and shadow policies against the versioned labeled corpus."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .corpus import (
    AutomaticUseLabel,
    CorpusCache,
    CorpusManifest,
    RelevanceLabel,
)
from .evidence_policy import POLICY_VERSION, ShadowEvidencePolicy
from .filing_identity import FilingIdentityMetadata, resolve_filing_identity
from .models import DocumentKind, DocumentScope, DocumentVerification
from .validator import DocumentValidator


REPORT_SCHEMA_VERSION = 3
SCENARIO_DIMENSIONS = (
    "provider",
    "form",
    "requested_identity_scope",
    "filing_metadata_breadth",
    "document_encoding",
    "reason_category",
    "expected_relevance",
    "expected_document_kind",
    "expected_document_scope",
    "expected_automatic_use",
)


def _confusion(expected: Iterable[str], actual: Iterable[str]) -> Dict[str, Dict[str, int]]:
    pairs = Counter(zip(expected, actual))
    expected_labels = sorted({left for left, _ in pairs})
    actual_labels = sorted({right for _, right in pairs})
    return {
        left: {right: pairs[(left, right)] for right in actual_labels}
        for left in expected_labels
    }


def _metrics(rows: List[dict], policy_key: str) -> dict:
    expected_relevance = [row["expected"]["relevance"] for row in rows]
    actual_relevance = [row[policy_key]["relevance"] for row in rows]
    expected_kind = [row["expected"]["document_kind"] for row in rows]
    actual_kind = [row[policy_key]["document_kind"] for row in rows]
    expected_use = [row["expected"]["automatic_use"] for row in rows]
    actual_use = [row[policy_key]["automatic_use"] for row in rows]

    true_positive = sum(
        left == RelevanceLabel.POSITIVE.value
        and right == RelevanceLabel.POSITIVE.value
        for left, right in zip(expected_relevance, actual_relevance)
    )
    false_positive = sum(
        left != RelevanceLabel.POSITIVE.value
        and right == RelevanceLabel.POSITIVE.value
        for left, right in zip(expected_relevance, actual_relevance)
    )
    false_negative = sum(
        left == RelevanceLabel.POSITIVE.value
        and right != RelevanceLabel.POSITIVE.value
        for left, right in zip(expected_relevance, actual_relevance)
    )
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else None
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else None
    )
    false_positive_verification = sum(
        expected != AutomaticUseLabel.ALLOWED.value
        and actual == AutomaticUseLabel.ALLOWED.value
        for expected, actual in zip(expected_use, actual_use)
    )
    false_negative_verification = sum(
        expected == AutomaticUseLabel.ALLOWED.value
        and actual != AutomaticUseLabel.ALLOWED.value
        for expected, actual in zip(expected_use, actual_use)
    )
    review_count = sum(value == AutomaticUseLabel.REVIEW.value for value in actual_use)
    result = {
        "case_count": len(rows),
        "relevance_confusion_matrix": _confusion(expected_relevance, actual_relevance),
        "document_kind_confusion_matrix": _confusion(expected_kind, actual_kind),
        "automatic_use_confusion_matrix": _confusion(expected_use, actual_use),
        "positive_precision": precision,
        "positive_recall": recall,
        "false_positive_verification_count": false_positive_verification,
        "false_negative_verification_count": false_negative_verification,
        "manual_review_count": review_count,
        "manual_review_rate": review_count / len(rows) if rows else None,
    }
    scope_rows = [
        row for row in rows if "document_scope" in row["expected"]
    ]
    if scope_rows:
        result["document_scope_confusion_matrix"] = _confusion(
            [row["expected"]["document_scope"] for row in scope_rows],
            [row[policy_key]["document_scope"] for row in scope_rows],
        )
    profile_rows = [
        row for row in rows if "content_profile" in row["expected"]
    ]
    if profile_rows:
        profile_fields = (
            "contains_summary_prospectus",
            "contains_statutory_prospectus",
            "contains_sai",
            "is_supplement",
        )
        result["content_profile_confusion_matrices"] = {
            field: _confusion(
                [
                    str(row["expected"]["content_profile"][field]).lower()
                    for row in profile_rows
                ],
                [
                    str(row[policy_key]["content_profile"][field]).lower()
                    for row in profile_rows
                ],
            )
            for field in profile_fields
        }
    return result


def _requested_identity_scope(class_id: Optional[str], series_id: Optional[str]) -> str:
    if class_id:
        return "class"
    if series_id:
        return "series"
    return "registrant"


def _filing_metadata_breadth(metadata: FilingIdentityMetadata) -> str:
    if not metadata.series:
        return "registrant_only"
    if len(metadata.series) > 1:
        return "multiple_series"
    if len(metadata.series[0].classes) > 1:
        return "single_series_multiple_classes"
    return "single_series_single_class"


def _current_content_profile(kind: DocumentKind) -> dict:
    return {
        "contains_summary_prospectus": (
            kind is DocumentKind.SUMMARY_PROSPECTUS
        ),
        "contains_statutory_prospectus": (
            kind is DocumentKind.STATUTORY_PROSPECTUS
        ),
        "contains_sai": (
            kind is DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION
        ),
        "is_supplement": kind is DocumentKind.SUPPLEMENT,
    }


def _scenario_slices(rows: List[dict], policy_key: str) -> dict:
    slices: Dict[str, dict] = {}
    for dimension in SCENARIO_DIMENSIONS:
        values = sorted({row["scenario"][dimension] for row in rows})
        slices[dimension] = {
            value: _metrics(
                [row for row in rows if row["scenario"][dimension] == value],
                policy_key,
            )
            for value in values
        }
    return slices


class CorpusEvaluator:
    def __init__(
        self,
        cache: CorpusCache,
        current: Optional[DocumentValidator] = None,
        shadow: Optional[ShadowEvidencePolicy] = None,
    ) -> None:
        self.cache = cache
        self.current = current or DocumentValidator()
        self.shadow = shadow or ShadowEvidencePolicy()

    def evaluate(self, manifest: CorpusManifest) -> dict:
        rows: List[dict] = []
        for case in manifest.cases:
            content, header_page, detail_page = self.cache.read_case(case)
            metadata = resolve_filing_identity(header_page, detail_page)
            if metadata.accession != case.accession:
                raise ValueError(
                    f"{case.case_id}: header accession {metadata.accession} does not "
                    f"match manifest {case.accession}"
                )

            current = self.current.validate(content, case.ticker, case.class_id)
            current_relevance = (
                RelevanceLabel.POSITIVE
                if current.ticker_found or current.class_id_found
                else RelevanceLabel.AMBIGUOUS
            )
            if (
                current.verification is DocumentVerification.VERIFIED
                and current.complete
            ):
                current_use = AutomaticUseLabel.ALLOWED
            elif current.kind is DocumentKind.SUPPLEMENT:
                current_use = AutomaticUseLabel.DISALLOWED
            else:
                current_use = AutomaticUseLabel.REVIEW

            shadow = self.shadow.evaluate(
                content,
                case.ticker,
                case.class_id,
                case.series_id,
                metadata,
                requested_cik=case.requested_cik,
                registrant_cik=case.registrant_cik,
                known_series_count=case.known_series_count,
                declared_form=case.form,
            )
            expected = {
                "relevance": case.labels.relevance.value,
                "document_kind": case.labels.document_kind.value,
                "automatic_use": case.labels.automatic_use.value,
            }
            if (
                case.labels.document_scope is not None
                and case.labels.content_profile is not None
            ):
                expected["document_scope"] = case.labels.document_scope.value
                expected["content_profile"] = {
                    "contains_summary_prospectus": (
                        case.labels.content_profile.contains_summary_prospectus
                    ),
                    "contains_statutory_prospectus": (
                        case.labels.content_profile.contains_statutory_prospectus
                    ),
                    "contains_sai": case.labels.content_profile.contains_sai,
                    "is_supplement": case.labels.content_profile.is_supplement,
                }
            current_value = {
                "relevance": current_relevance.value,
                "document_kind": current.kind.value,
                "automatic_use": current_use.value,
                "document_scope": DocumentScope.UNKNOWN.value,
                "content_profile": _current_content_profile(current.kind),
                "evidence": current.evidence,
                "warnings": current.warnings,
            }
            shadow_value = shadow.as_dict()
            scenario = {
                "provider": case.provider,
                "form": case.form,
                "requested_identity_scope": _requested_identity_scope(
                    case.class_id,
                    case.series_id,
                ),
                "filing_metadata_breadth": _filing_metadata_breadth(metadata),
                "document_encoding": (
                    "inline_xbrl" if b"<ix:" in content.lower() else "html"
                ),
                "reason_category": case.reason.category,
                "expected_relevance": case.labels.relevance.value,
                "expected_document_kind": case.labels.document_kind.value,
                "expected_document_scope": (
                    case.labels.document_scope.value
                    if case.labels.document_scope is not None
                    else "not_labeled"
                ),
                "expected_automatic_use": case.labels.automatic_use.value,
            }
            rows.append(
                {
                    "case_id": case.case_id,
                    "ticker": case.ticker,
                    "accession": case.accession,
                    "document_url": case.document_url,
                    "identity_metadata_source": metadata.source.value,
                    "scenario": scenario,
                    "expected": expected,
                    "label_reason": {
                        "summary": case.reason.summary,
                        "category": case.reason.category,
                        "observed_evidence": case.reason.observed_evidence,
                        "missing_evidence": case.reason.missing_evidence,
                        "contradictory_evidence": case.reason.contradictory_evidence,
                        "resolution_needed": case.reason.resolution_needed,
                    },
                    "current": current_value,
                    "shadow": shadow_value,
                    "disagreements": {
                        "current": [
                            key
                            for key, value in expected.items()
                            if current_value.get(key) != value
                        ],
                        "shadow": [
                            key
                            for key, value in expected.items()
                            if shadow_value.get(key) != value
                        ],
                    },
                }
            )

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "corpus_version": manifest.corpus_version,
            "shadow_policy_version": POLICY_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "current_metrics": _metrics(rows, "current"),
            "shadow_metrics": _metrics(rows, "shadow"),
            "scenario_dimensions": list(SCENARIO_DIMENSIONS),
            "current_slices": _scenario_slices(rows, "current"),
            "shadow_slices": _scenario_slices(rows, "shadow"),
            "cases": rows,
        }


def save_report(report: dict, path: Union[str, Path]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
