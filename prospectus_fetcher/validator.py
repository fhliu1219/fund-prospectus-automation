"""Deterministic content classification and ticker-evidence validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import List, Optional, Set

from .models import DocumentKind, DocumentVerification


_COMPLETE_KINDS = {
    DocumentKind.SUMMARY_PROSPECTUS,
    DocumentKind.STATUTORY_PROSPECTUS,
}

_EARLY_CONTENT_WINDOW = 20_000

_SUPPLEMENT_SIGNALS = (
    "supplement dated",
    "this supplement amends",
    "this supplement updates",
    "this supplement supplements",
    "read this supplement in conjunction",
)

_SECTION_SIGNALS = {
    "investment objective": ("investment objective",),
    "fees and expenses": ("fees and expenses",),
    "investment strategies": (
        "principal investment strategies",
        "principal investment strategy",
    ),
    "principal risks": (
        "principal risks",
        "principal investment risks",
    ),
    "performance": (
        "annual total returns",
        "average annual total returns",
        "performance information",
    ),
    "management": (
        "investment adviser",
        "portfolio manager",
        "management of the fund",
    ),
}

_DATE_PATTERN = (
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2}),\s+(\d{4})\b"
)
_DATED_RE = re.compile(r"\bdated\s+" + _DATE_PATTERN, re.IGNORECASE)
_BASE_PROSPECTUS_DATED_RE = re.compile(
    r"\b(?:summary\s+|statutory\s+)?prospectus(?:es)?\b"
    r"[^.;:]{0,120}?\bdated\s+"
    + _DATE_PATTERN,
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data)


@dataclass
class ValidationResult:
    kind: DocumentKind
    verification: DocumentVerification
    evidence: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    referenced_dates: List[str] = field(default_factory=list)
    base_prospectus_dates: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    ticker_found: bool = False
    class_id_found: bool = False

    @property
    def complete(self) -> bool:
        return self.kind in _COMPLETE_KINDS


def extract_text(content: bytes) -> str:
    """Extract normalized visible text from SEC HTML bytes."""
    decoded: Optional[str] = None
    for encoding in ("utf-8", "windows-1252", "latin-1"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:  # pragma: no cover - latin-1 decodes every byte
        decoded = content.decode("utf-8", errors="replace")

    parser = _TextExtractor()
    parser.feed(decoded)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _contains_identifier(text: str, identifier: Optional[str]) -> bool:
    if not identifier:
        return False
    pattern = rf"(?<![A-Z0-9]){re.escape(identifier.upper())}(?![A-Z0-9])"
    return re.search(pattern, text.upper()) is not None


def _section_matches(lower_text: str) -> Set[str]:
    return {
        section
        for section, signals in _SECTION_SIGNALS.items()
        if any(signal in lower_text for signal in signals)
    }


def _extract_dates(text: str, pattern: re.Pattern = _DATED_RE) -> List[str]:
    dates: List[str] = []
    for month, day, year in pattern.findall(text):
        try:
            parsed = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
        except ValueError:
            continue
        value = parsed.strftime("%Y-%m-%d")
        if value not in dates:
            dates.append(value)
    return dates


class DocumentValidator:
    """Classify document content and require direct requested-class evidence."""

    def validate(
        self,
        content: bytes,
        ticker: str,
        class_id: Optional[str] = None,
    ) -> ValidationResult:
        text = extract_text(content)
        lower_text = text.lower()
        early_content = lower_text[:_EARLY_CONTENT_WINDOW]
        sections = _section_matches(lower_text)
        identity_text = text[:_EARLY_CONTENT_WINDOW]
        ticker_found = _contains_identifier(identity_text, ticker)
        class_id_found = _contains_identifier(identity_text, class_id)
        direct_identity_found = ticker_found or class_id_found
        dates = _extract_dates(text)

        supplement_signal = next(
            (signal for signal in _SUPPLEMENT_SIGNALS if signal in early_content),
            None,
        )
        if supplement_signal:
            kind = DocumentKind.SUPPLEMENT
        elif "summary prospectus" in early_content and len(sections) >= 2:
            kind = DocumentKind.SUMMARY_PROSPECTUS
        elif (
            "statutory prospectus" in early_content
            or "prospectus dated" in early_content
        ) and len(sections) >= 2:
            kind = DocumentKind.STATUTORY_PROSPECTUS
        else:
            kind = DocumentKind.UNKNOWN

        base_prospectus_dates = (
            _extract_dates(text, _BASE_PROSPECTUS_DATED_RE)
            if kind is DocumentKind.SUPPLEMENT
            else []
        )

        evidence: List[str] = []
        warnings: List[str] = []
        if ticker_found:
            evidence.append(f"document contains exact ticker {ticker.upper()}")
        if class_id_found and class_id:
            evidence.append(f"document contains class identifier {class_id.upper()}")

        if kind is DocumentKind.SUPPLEMENT:
            evidence.append(f"classified as supplement from phrase '{supplement_signal}'")
            if base_prospectus_dates:
                evidence.append(
                    "supplement references prospectus date(s): "
                    + ", ".join(base_prospectus_dates)
                )
            verification = DocumentVerification.MANUAL_REVIEW_REQUIRED
            warnings.append("supplement requires a verified base prospectus")
        elif kind in _COMPLETE_KINDS:
            evidence.append(
                f"classified as {kind.value} from title and sections: "
                + ", ".join(sorted(sections))
            )
            if direct_identity_found:
                verification = DocumentVerification.VERIFIED
            else:
                verification = DocumentVerification.MANUAL_REVIEW_REQUIRED
                warnings.append(
                    f"complete prospectus lacks direct evidence for ticker {ticker.upper()}"
                )
        else:
            verification = DocumentVerification.MANUAL_REVIEW_REQUIRED
            warnings.append("document type could not be established from content")
            if sections:
                evidence.append("prospectus-like sections found: " + ", ".join(sorted(sections)))

        return ValidationResult(
            kind=kind,
            verification=verification,
            evidence=evidence,
            warnings=warnings,
            referenced_dates=dates,
            base_prospectus_dates=base_prospectus_dates,
            ticker_found=ticker_found,
            class_id_found=class_id_found,
        )
