"""Versioned validation-corpus contracts and checksum-verified local caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit

from .models import DocumentKind, DocumentScope
from .sec_client import SECClient


_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FUND_ID_RE = re.compile(r"^[SC]\d{9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CorpusSchemaError(ValueError):
    """A corpus manifest or cached resource violated its declared contract."""


class RelevanceLabel(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    AMBIGUOUS = "ambiguous"


class AutomaticUseLabel(str, Enum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    REVIEW = "review"


@dataclass(frozen=True)
class CorpusLabels:
    relevance: RelevanceLabel
    document_kind: DocumentKind
    automatic_use: AutomaticUseLabel
    document_scope: Optional[DocumentScope] = None
    content_profile: Optional["CorpusContentProfile"] = None


@dataclass(frozen=True)
class CorpusContentProfile:
    contains_summary_prospectus: bool
    contains_statutory_prospectus: bool
    contains_sai: bool
    is_supplement: bool


@dataclass(frozen=True)
class LabelReason:
    summary: str
    category: str
    observed_evidence: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    contradictory_evidence: List[str] = field(default_factory=list)
    resolution_needed: Optional[str] = None


@dataclass(frozen=True)
class CorpusCase:
    case_id: str
    provider: str
    ticker: str
    registrant_cik: int
    requested_cik: int
    accession: str
    form: str
    filing_date: str
    document_url: str
    document_sha256: str
    filing_header_url: str
    filing_header_sha256: str
    labels: CorpusLabels
    reason: LabelReason
    series_id: Optional[str] = None
    class_id: Optional[str] = None
    known_series_count: Optional[int] = None
    filing_detail_url: Optional[str] = None
    filing_detail_sha256: Optional[str] = None


@dataclass(frozen=True)
class CorpusManifest:
    schema_version: int
    corpus_version: str
    target_case_count: int
    cases: List[CorpusCase]


def _required(mapping: Dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise CorpusSchemaError(f"{path}.{key}: missing required field")
    return mapping[key]


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusSchemaError(f"{path}: expected non-empty string")
    return value.strip()


def _text_list(value: Any, path: str) -> List[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise CorpusSchemaError(f"{path}: expected array of non-empty strings")
    return [item.strip() for item in value]


def _enum(enum_type, value: Any, path: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        options = ", ".join(item.value for item in enum_type)
        raise CorpusSchemaError(f"{path}: expected one of {options}") from exc


def _sec_archive_url(value: Any, path: str, cik: int, accession: str) -> str:
    url = _text(value, path)
    parsed = urlsplit(url)
    archive_prefix = f"/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.sec.gov"
        or not parsed.path.startswith(archive_prefix)
    ):
        raise CorpusSchemaError(
            f"{path}: expected SEC archive URL under {archive_prefix}"
        )
    return url


def parse_manifest(payload: Any) -> CorpusManifest:
    if not isinstance(payload, dict):
        raise CorpusSchemaError("$: expected object")
    schema_version = _required(payload, "schema_version", "$")
    if schema_version not in {1, 2}:
        raise CorpusSchemaError("$.schema_version: expected 1 or 2")
    corpus_version = _text(_required(payload, "corpus_version", "$"), "$.corpus_version")
    target = _required(payload, "target_case_count", "$")
    if not isinstance(target, int) or target <= 0:
        raise CorpusSchemaError("$.target_case_count: expected positive integer")
    values = _required(payload, "cases", "$")
    if not isinstance(values, list):
        raise CorpusSchemaError("$.cases: expected array")
    if len(values) != target:
        raise CorpusSchemaError(
            f"$.cases: expected {target} cases, received {len(values)}"
        )

    cases: List[CorpusCase] = []
    seen_ids = set()
    for index, value in enumerate(values):
        path = f"$.cases[{index}]"
        if not isinstance(value, dict):
            raise CorpusSchemaError(f"{path}: expected object")
        case_id = _text(_required(value, "case_id", path), f"{path}.case_id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", case_id):
            raise CorpusSchemaError(f"{path}.case_id: expected stable lowercase slug")
        if case_id in seen_ids:
            raise CorpusSchemaError(f"{path}.case_id: duplicate {case_id!r}")
        seen_ids.add(case_id)

        ticker = _text(_required(value, "ticker", path), f"{path}.ticker").upper()
        registrant_cik = _required(value, "registrant_cik", path)
        requested_cik = _required(value, "requested_cik", path)
        if not isinstance(registrant_cik, int) or registrant_cik <= 0:
            raise CorpusSchemaError(
                f"{path}.registrant_cik: expected positive integer"
            )
        if not isinstance(requested_cik, int) or requested_cik <= 0:
            raise CorpusSchemaError(f"{path}.requested_cik: expected positive integer")
        accession = _text(
            _required(value, "accession", path), f"{path}.accession"
        )
        if not _ACCESSION_RE.fullmatch(accession):
            raise CorpusSchemaError(f"{path}.accession: invalid accession")

        series_id = value.get("series_id") or None
        class_id = value.get("class_id") or None
        known_series_count = value.get("known_series_count")
        for name, identifier, prefix in (
            ("series_id", series_id, "S"),
            ("class_id", class_id, "C"),
        ):
            if identifier is not None and (
                not isinstance(identifier, str)
                or not _FUND_ID_RE.fullmatch(identifier)
                or not identifier.startswith(prefix)
            ):
                raise CorpusSchemaError(f"{path}.{name}: invalid SEC fund identifier")
        if class_id and not series_id:
            raise CorpusSchemaError(
                f"{path}.series_id: required when class_id is present"
            )
        if known_series_count is not None and (
            not isinstance(known_series_count, int) or known_series_count < 0
        ):
            raise CorpusSchemaError(
                f"{path}.known_series_count: expected non-negative integer or null"
            )

        filing_date = _text(
            _required(value, "filing_date", path), f"{path}.filing_date"
        )
        if not _DATE_RE.fullmatch(filing_date):
            raise CorpusSchemaError(f"{path}.filing_date: expected YYYY-MM-DD")
        try:
            date.fromisoformat(filing_date)
        except ValueError as exc:
            raise CorpusSchemaError(f"{path}.filing_date: invalid calendar date") from exc

        document_sha = _text(
            _required(value, "document_sha256", path),
            f"{path}.document_sha256",
        )
        header_sha = _text(
            _required(value, "filing_header_sha256", path),
            f"{path}.filing_header_sha256",
        )
        if not _SHA256_RE.fullmatch(document_sha):
            raise CorpusSchemaError(f"{path}.document_sha256: invalid SHA-256")
        if not _SHA256_RE.fullmatch(header_sha):
            raise CorpusSchemaError(f"{path}.filing_header_sha256: invalid SHA-256")
        detail_url_value = value.get("filing_detail_url")
        detail_sha_value = value.get("filing_detail_sha256")
        if (detail_url_value is None) != (detail_sha_value is None):
            raise CorpusSchemaError(
                f"{path}: filing_detail_url and filing_detail_sha256 must appear together"
            )
        detail_url: Optional[str] = None
        detail_sha: Optional[str] = None
        if detail_url_value is not None:
            detail_url = _sec_archive_url(
                detail_url_value,
                f"{path}.filing_detail_url",
                registrant_cik,
                accession,
            )
            detail_sha = _text(
                detail_sha_value,
                f"{path}.filing_detail_sha256",
            )
            if not _SHA256_RE.fullmatch(detail_sha):
                raise CorpusSchemaError(
                    f"{path}.filing_detail_sha256: invalid SHA-256"
                )

        labels_value = _required(value, "labels", path)
        reason_value = _required(value, "reason", path)
        if not isinstance(labels_value, dict) or not isinstance(reason_value, dict):
            raise CorpusSchemaError(f"{path}: labels and reason must be objects")
        content_profile_value = labels_value.get("content_profile")
        document_scope_value = labels_value.get("document_scope")
        if schema_version == 2:
            if not isinstance(content_profile_value, dict):
                raise CorpusSchemaError(
                    f"{path}.labels.content_profile: expected object"
                )
            if document_scope_value is None:
                raise CorpusSchemaError(
                    f"{path}.labels.document_scope: missing required field"
                )
        elif content_profile_value is not None or document_scope_value is not None:
            raise CorpusSchemaError(
                f"{path}.labels: content_profile and document_scope require "
                "schema_version 2"
            )

        content_profile = None
        if content_profile_value is not None:
            profile_fields = (
                "contains_summary_prospectus",
                "contains_statutory_prospectus",
                "contains_sai",
                "is_supplement",
            )
            values = {}
            for name in profile_fields:
                profile_value = _required(
                    content_profile_value,
                    name,
                    f"{path}.labels.content_profile",
                )
                if not isinstance(profile_value, bool):
                    raise CorpusSchemaError(
                        f"{path}.labels.content_profile.{name}: expected boolean"
                    )
                values[name] = profile_value
            unexpected = set(content_profile_value).difference(profile_fields)
            if unexpected:
                raise CorpusSchemaError(
                    f"{path}.labels.content_profile: unexpected fields "
                    + ", ".join(sorted(unexpected))
                )
            content_profile = CorpusContentProfile(**values)

        labels = CorpusLabels(
            relevance=_enum(
                RelevanceLabel,
                _required(labels_value, "relevance", f"{path}.labels"),
                f"{path}.labels.relevance",
            ),
            document_kind=_enum(
                DocumentKind,
                _required(labels_value, "document_kind", f"{path}.labels"),
                f"{path}.labels.document_kind",
            ),
            automatic_use=_enum(
                AutomaticUseLabel,
                _required(labels_value, "automatic_use", f"{path}.labels"),
                f"{path}.labels.automatic_use",
            ),
            document_scope=(
                _enum(
                    DocumentScope,
                    document_scope_value,
                    f"{path}.labels.document_scope",
                )
                if document_scope_value is not None
                else None
            ),
            content_profile=content_profile,
        )
        reason = LabelReason(
            summary=_text(
                _required(reason_value, "summary", f"{path}.reason"),
                f"{path}.reason.summary",
            ),
            category=_text(
                _required(reason_value, "category", f"{path}.reason"),
                f"{path}.reason.category",
            ),
            observed_evidence=_text_list(
                _required(reason_value, "observed_evidence", f"{path}.reason"),
                f"{path}.reason.observed_evidence",
            ),
            missing_evidence=_text_list(
                reason_value.get("missing_evidence", []),
                f"{path}.reason.missing_evidence",
            ),
            contradictory_evidence=_text_list(
                reason_value.get("contradictory_evidence", []),
                f"{path}.reason.contradictory_evidence",
            ),
            resolution_needed=(
                _text(reason_value["resolution_needed"], f"{path}.reason.resolution_needed")
                if reason_value.get("resolution_needed")
                else None
            ),
        )
        if not reason.observed_evidence:
            raise CorpusSchemaError(
                f"{path}.reason: observed_evidence must contain at least one observation"
            )
        if labels.relevance is RelevanceLabel.AMBIGUOUS and (
            not reason.missing_evidence or not reason.resolution_needed
        ):
            raise CorpusSchemaError(
                f"{path}.reason: ambiguous case requires missing_evidence and "
                "resolution_needed"
            )
        if labels.document_kind in {
            DocumentKind.SUPPLEMENT,
            DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION,
        } and labels.automatic_use is AutomaticUseLabel.ALLOWED:
            raise CorpusSchemaError(
                f"{path}.labels: {labels.document_kind.value} cannot be allowed "
                "as a standalone prospectus"
            )
        if (
            labels.relevance is RelevanceLabel.AMBIGUOUS
            and labels.automatic_use is not AutomaticUseLabel.REVIEW
        ):
            raise CorpusSchemaError(
                f"{path}.labels: ambiguous relevance requires review"
            )
        if labels.content_profile is not None:
            profile = labels.content_profile
            complete_profile = (
                profile.contains_summary_prospectus
                or profile.contains_statutory_prospectus
            )
            profile_kind_requirements = {
                DocumentKind.SUMMARY_PROSPECTUS: profile.contains_summary_prospectus,
                DocumentKind.STATUTORY_PROSPECTUS: (
                    profile.contains_statutory_prospectus
                ),
                DocumentKind.SUPPLEMENT: profile.is_supplement,
                DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION: (
                    profile.contains_sai and not complete_profile
                ),
                DocumentKind.UNKNOWN: not (
                    complete_profile or profile.contains_sai or profile.is_supplement
                ),
            }
            if (
                labels.document_kind is DocumentKind.COMBINED_PROSPECTUS_PACKAGE
                and (
                    not complete_profile
                    or sum(
                        (
                            profile.contains_summary_prospectus,
                            profile.contains_statutory_prospectus,
                            profile.contains_sai,
                        )
                    )
                    < 2
                    or profile.is_supplement
                )
            ):
                raise CorpusSchemaError(
                    f"{path}.labels: combined package requires at least two "
                    "non-supplement content characteristics including a prospectus"
                )
            if (
                labels.document_kind in profile_kind_requirements
                and not profile_kind_requirements[labels.document_kind]
            ):
                raise CorpusSchemaError(
                    f"{path}.labels: document_kind conflicts with content_profile"
                )
            if labels.automatic_use is AutomaticUseLabel.ALLOWED and (
                not complete_profile or profile.is_supplement
            ):
                raise CorpusSchemaError(
                    f"{path}.labels: allowed use requires complete non-supplement "
                    "prospectus content"
                )

        cases.append(
            CorpusCase(
                case_id=case_id,
                provider=_text(_required(value, "provider", path), f"{path}.provider"),
                ticker=ticker,
                registrant_cik=registrant_cik,
                requested_cik=requested_cik,
                accession=accession,
                form=_text(_required(value, "form", path), f"{path}.form").upper(),
                filing_date=filing_date,
                series_id=series_id,
                class_id=class_id,
                known_series_count=known_series_count,
                document_url=_sec_archive_url(
                    _required(value, "document_url", path),
                    f"{path}.document_url",
                    registrant_cik,
                    accession,
                ),
                document_sha256=document_sha,
                filing_header_url=_sec_archive_url(
                    _required(value, "filing_header_url", path),
                    f"{path}.filing_header_url",
                    registrant_cik,
                    accession,
                ),
                filing_header_sha256=header_sha,
                filing_detail_url=detail_url,
                filing_detail_sha256=detail_sha,
                labels=labels,
                reason=reason,
            )
        )

    return CorpusManifest(schema_version, corpus_version, target, cases)


def load_manifest(path: Union[str, Path]) -> CorpusManifest:
    with open(path, encoding="utf-8") as handle:
        return parse_manifest(json.load(handle))


class CorpusCache:
    """Fetch immutable SEC corpus resources once and verify them on every use."""

    def __init__(self, client: SECClient, cache_dir: Union[str, Path]) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)

    def document_path(self, case: CorpusCase) -> Path:
        return self.cache_dir / "documents" / f"{case.document_sha256}.html"

    def header_path(self, case: CorpusCase) -> Path:
        return self.cache_dir / "headers" / f"{case.filing_header_sha256}.html"

    def detail_path(self, case: CorpusCase) -> Path:
        if case.filing_detail_sha256 is None:
            raise ValueError(f"{case.case_id}: filing detail resource is not configured")
        return self.cache_dir / "filing-details" / (
            f"{case.filing_detail_sha256}.html"
        )

    def ensure_case(self, case: CorpusCase) -> tuple[Path, Path, Optional[Path]]:
        document_path = self._ensure(
            case.document_url,
            case.document_sha256,
            self.document_path(case),
        )
        header_path = self._ensure(
            case.filing_header_url,
            case.filing_header_sha256,
            self.header_path(case),
        )
        detail_path = None
        if case.filing_detail_url and case.filing_detail_sha256:
            detail_path = self._ensure(
                case.filing_detail_url,
                case.filing_detail_sha256,
                self.detail_path(case),
            )
        return document_path, header_path, detail_path

    def ensure_manifest(self, manifest: CorpusManifest) -> None:
        for case in manifest.cases:
            self.ensure_case(case)

    def read_case(self, case: CorpusCase) -> tuple[bytes, str, Optional[str]]:
        document_path, header_path, detail_path = self.ensure_case(case)
        return (
            document_path.read_bytes(),
            header_path.read_text(encoding="utf-8", errors="replace"),
            (
                detail_path.read_text(encoding="utf-8", errors="replace")
                if detail_path
                else None
            ),
        )

    def _ensure(self, url: str, expected_sha: str, path: Path) -> Path:
        if path.exists():
            self._verify(path.read_bytes(), expected_sha, str(path))
            return path
        content = self.client.get_bytes(url)
        self._verify(content, expected_sha, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
        return path

    @staticmethod
    def _verify(content: bytes, expected_sha: str, source: str) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha:
            raise CorpusSchemaError(
                f"checksum mismatch for {source}: expected {expected_sha}, received {actual}"
            )
