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

from .models import DocumentKind
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
    if schema_version != 1:
        raise CorpusSchemaError("$.schema_version: expected 1")
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

        labels_value = _required(value, "labels", path)
        reason_value = _required(value, "reason", path)
        if not isinstance(labels_value, dict) or not isinstance(reason_value, dict):
            raise CorpusSchemaError(f"{path}: labels and reason must be objects")
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
        if (
            labels.document_kind is DocumentKind.SUPPLEMENT
            and labels.automatic_use is AutomaticUseLabel.ALLOWED
        ):
            raise CorpusSchemaError(
                f"{path}.labels: supplement cannot be allowed as a standalone document"
            )
        if (
            labels.relevance is RelevanceLabel.AMBIGUOUS
            and labels.automatic_use is not AutomaticUseLabel.REVIEW
        ):
            raise CorpusSchemaError(
                f"{path}.labels: ambiguous relevance requires review"
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

    def ensure_case(self, case: CorpusCase) -> tuple[Path, Path]:
        return (
            self._ensure(case.document_url, case.document_sha256, self.document_path(case)),
            self._ensure(
                case.filing_header_url,
                case.filing_header_sha256,
                self.header_path(case),
            ),
        )

    def ensure_manifest(self, manifest: CorpusManifest) -> None:
        for case in manifest.cases:
            self.ensure_case(case)

    def read_case(self, case: CorpusCase) -> tuple[bytes, str]:
        document_path, header_path = self.ensure_case(case)
        return document_path.read_bytes(), header_path.read_text(
            encoding="utf-8", errors="replace"
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
