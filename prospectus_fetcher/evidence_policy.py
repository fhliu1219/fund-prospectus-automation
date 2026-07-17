"""Location-aware shadow evidence policy for Milestone 6 evaluation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import List, Optional, Set

from .corpus import AutomaticUseLabel, RelevanceLabel
from .filing_identity import FilingIdentityMetadata
from .models import DocumentKind
from .validator import extract_text


POLICY_VERSION = "m6-shadow-v1"

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
    "principal risks": ("principal risks", "principal investment risks"),
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
_EXCLUSION_RE = re.compile(
    r"\b(?:not available (?:to|for)|not offered (?:to|for)|"
    r"does not apply to|excluding)\b",
    re.IGNORECASE,
)
_REGISTRATION_SIGNALS = (
    "form n-1a registration statement",
    "registration statement under the securities act",
    "post-effective amendment",
    "post effective amendment",
    "form s-6",
)
_INCIDENTAL_RE = re.compile(
    r"\b(?:benchmark|comparison|portfolio holdings?|underlying (?:index|fund|security))\b",
    re.IGNORECASE,
)
_APPLICABILITY_RE = re.compile(
    r"\b(?:all|each)\s+(?:share\s+)?classes\b|\bapplicable to all classes\b",
    re.IGNORECASE,
)


class EvidenceStrength(str, Enum):
    STRONG = "strong"
    SUPPORTING = "supporting"
    WEAK = "weak"


class EvidenceLocation(str, Enum):
    FILING_METADATA = "filing_metadata"
    FRONT_MATTER = "front_matter"
    HEADING = "heading"
    CLASS_TABLE = "class_table"
    BODY = "body"


@dataclass(frozen=True)
class EvidenceSignal:
    code: str
    strength: EvidenceStrength
    location: EvidenceLocation
    detail: str


@dataclass
class ShadowEvaluation:
    policy_version: str
    relevance: RelevanceLabel
    document_kind: DocumentKind
    automatic_use: AutomaticUseLabel
    signals: List[EvidenceSignal] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        value = asdict(self)
        value["relevance"] = self.relevance.value
        value["document_kind"] = self.document_kind.value
        value["automatic_use"] = self.automatic_use.value
        for index, signal in enumerate(self.signals):
            value["signals"][index]["strength"] = signal.strength.value
            value["signals"][index]["location"] = signal.location.value
        return value


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored = 0
        self._heading_depth = 0
        self._heading_parts: List[str] = []
        self.headings: List[str] = []
        self._table_depth = 0
        self._row_depth = 0
        self._row_parts: List[str] = []
        self.table_rows: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript"}:
            self._ignored += 1
        if self._ignored:
            return
        if low in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_depth += 1
            if self._heading_depth == 1:
                self._heading_parts = []
        if low == "table":
            self._table_depth += 1
        if low == "tr" and self._table_depth:
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_parts = []

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
            return
        if self._ignored:
            return
        if low in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading_depth:
            self._heading_depth -= 1
            if self._heading_depth == 0:
                value = _normalize_space(" ".join(self._heading_parts))
                if value:
                    self.headings.append(value)
        if low == "tr" and self._row_depth:
            self._row_depth -= 1
            if self._row_depth == 0:
                value = _normalize_space(" ".join(self._row_parts))
                if value:
                    self.table_rows.append(value)
        if low == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored or not data.strip():
            return
        if self._heading_depth:
            self._heading_parts.append(data)
        if self._row_depth:
            self._row_parts.append(data)


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _identifier_re(identifier: str) -> re.Pattern:
    return re.compile(
        rf"(?<![A-Z0-9]){re.escape(identifier.upper())}(?![A-Z0-9])",
        re.IGNORECASE,
    )


def _contains_identifier(text: str, identifier: Optional[str]) -> bool:
    return bool(identifier and _identifier_re(identifier).search(text))


def _normalize_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _contains_name(text: str, expected: Optional[str]) -> bool:
    if not expected:
        return False
    normalized = _normalize_name(expected)
    return bool(normalized and normalized in _normalize_name(text))


def _section_matches(lower_text: str) -> Set[str]:
    return {
        section
        for section, signals in _SECTION_SIGNALS.items()
        if any(signal in lower_text for signal in signals)
    }


def _front_matter(text: str) -> str:
    lower = text.lower()
    starts = [
        lower.find(signal)
        for signals in _SECTION_SIGNALS.values()
        for signal in signals
        if lower.find(signal) >= 0
    ]
    return text[: min(starts)] if starts else text


def _contexts(text: str, identifier: str, radius: int = 160) -> List[str]:
    return [
        text[max(0, match.start() - radius) : match.end() + radius]
        for match in _identifier_re(identifier).finditer(text)
    ]


class ShadowEvidencePolicy:
    """Produce a non-controlling structured evidence recommendation."""

    def evaluate(
        self,
        content: bytes,
        ticker: str,
        class_id: Optional[str],
        series_id: Optional[str],
        metadata: FilingIdentityMetadata,
    ) -> ShadowEvaluation:
        ticker = ticker.upper()
        text = extract_text(content)
        lower = text.lower()
        front = _front_matter(text)
        parser = _StructureParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        parser.close()
        heading_text = " ".join(parser.headings)

        signals: List[EvidenceSignal] = []
        missing: List[str] = []
        contradictions: List[str] = []
        expected_series_name: Optional[str] = None
        expected_class_name: Optional[str] = None

        metadata_match = None
        if class_id:
            metadata_match = metadata.find_class(class_id)
            if metadata.series and metadata_match is None:
                contradictions.append(
                    f"SEC filing header does not include requested class ID {class_id}"
                )
            elif metadata_match is None:
                missing.append("filing header contains no class metadata")
            else:
                series, class_identity = metadata_match
                expected_series_name = series.name
                expected_class_name = class_identity.name
                signals.append(
                    EvidenceSignal(
                        "metadata_class_match",
                        EvidenceStrength.STRONG,
                        EvidenceLocation.FILING_METADATA,
                        f"filing header associates {class_id} with {series.series_id}",
                    )
                )
                if series_id and series.series_id != series_id:
                    contradictions.append(
                        f"requested series {series_id} conflicts with filing-header "
                        f"series {series.series_id} for {class_id}"
                    )
                if class_identity.ticker and class_identity.ticker != ticker:
                    contradictions.append(
                        f"filing header maps {class_id} to {class_identity.ticker}, not {ticker}"
                    )
                elif class_identity.ticker == ticker:
                    signals.append(
                        EvidenceSignal(
                            "metadata_ticker_match",
                            EvidenceStrength.STRONG,
                            EvidenceLocation.FILING_METADATA,
                            f"filing header maps {class_id} to ticker {ticker}",
                        )
                    )

        ticker_rows = [row for row in parser.table_rows if _contains_identifier(row, ticker)]
        class_rows = [
            row for row in parser.table_rows if _contains_identifier(row, class_id)
        ] if class_id else []
        strong_document_identity = False

        if class_id and _contains_identifier(text, class_id):
            location = (
                EvidenceLocation.CLASS_TABLE if class_rows else EvidenceLocation.BODY
            )
            signals.append(
                EvidenceSignal(
                    "document_class_id",
                    EvidenceStrength.STRONG,
                    location,
                    f"document contains exact class ID {class_id}",
                )
            )
            strong_document_identity = True

        if ticker_rows:
            signals.append(
                EvidenceSignal(
                    "ticker_in_table",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.CLASS_TABLE,
                    f"ticker {ticker} appears in an HTML table row",
                )
            )
            strong_document_identity = True
        elif _contains_identifier(heading_text, ticker):
            signals.append(
                EvidenceSignal(
                    "ticker_in_heading",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.HEADING,
                    f"ticker {ticker} appears in a document heading",
                )
            )
            strong_document_identity = True
        elif _contains_identifier(front, ticker):
            signals.append(
                EvidenceSignal(
                    "ticker_in_front_matter",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.FRONT_MATTER,
                    f"ticker {ticker} appears before the first substantive section",
                )
            )
            strong_document_identity = True
        elif _contains_identifier(text, ticker):
            contexts = _contexts(text, ticker)
            incidental = any(_INCIDENTAL_RE.search(context) for context in contexts)
            signals.append(
                EvidenceSignal(
                    "ticker_in_body_incidental" if incidental else "ticker_in_body",
                    EvidenceStrength.WEAK,
                    EvidenceLocation.BODY,
                    (
                        f"ticker {ticker} appears only in incidental body context"
                        if incidental
                        else f"ticker {ticker} appears only in body text"
                    ),
                )
            )
        else:
            missing.append(f"document does not contain exact ticker {ticker}")

        if expected_series_name and _contains_name(text, expected_series_name):
            signals.append(
                EvidenceSignal(
                    "series_name_match",
                    EvidenceStrength.SUPPORTING,
                    (
                        EvidenceLocation.FRONT_MATTER
                        if _contains_name(front, expected_series_name)
                        else EvidenceLocation.BODY
                    ),
                    f"document contains SEC series name {expected_series_name!r}",
                )
            )
        elif expected_series_name:
            missing.append(
                f"document does not contain SEC series name {expected_series_name!r}"
            )

        class_name_paired = bool(
            expected_class_name
            and any(_contains_name(row, expected_class_name) for row in ticker_rows)
        )
        if class_name_paired:
            signals.append(
                EvidenceSignal(
                    "class_name_paired_with_ticker",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.CLASS_TABLE,
                    f"ticker row contains SEC class name {expected_class_name!r}",
                )
            )

        if _APPLICABILITY_RE.search(text) and expected_series_name and _contains_name(
            text, expected_series_name
        ):
            signals.append(
                EvidenceSignal(
                    "all_classes_applicability",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.BODY,
                    "document states applicability to all classes of the identified series",
                )
            )

        for context in _contexts(text, ticker):
            if _EXCLUSION_RE.search(context):
                contradictions.append(
                    f"document uses exclusionary language near ticker {ticker}"
                )
                break

        registrant_name_match = _contains_name(text, metadata.registrant_name)
        if registrant_name_match:
            signals.append(
                EvidenceSignal(
                    "registrant_name_match",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.BODY,
                    f"document contains SEC registrant name {metadata.registrant_name!r}",
                )
            )

        sections = _section_matches(lower)
        kind_region = (front + " " + heading_text).lower()
        if any(signal in kind_region for signal in _SUPPLEMENT_SIGNALS):
            kind = DocumentKind.SUPPLEMENT
        elif "summary prospectus" in kind_region and len(sections) >= 2:
            kind = DocumentKind.SUMMARY_PROSPECTUS
        elif (
            "statutory prospectus" in kind_region or "prospectus dated" in kind_region
            or any(signal in front.lower() for signal in _REGISTRATION_SIGNALS)
        ) and len(sections) >= 2:
            kind = DocumentKind.STATUTORY_PROSPECTUS
        else:
            kind = DocumentKind.UNKNOWN

        compatible_identity = any(
            signal.code
            in {
                "metadata_class_match",
                "metadata_ticker_match",
                "series_name_match",
                "class_name_paired_with_ticker",
                "registrant_name_match",
                "all_classes_applicability",
            }
            for signal in signals
        )
        if contradictions:
            relevance = RelevanceLabel.NEGATIVE
        elif strong_document_identity and compatible_identity:
            relevance = RelevanceLabel.POSITIVE
        else:
            relevance = RelevanceLabel.AMBIGUOUS
            if not strong_document_identity:
                missing.append("no strong location-aware document identity signal")
            if not compatible_identity:
                missing.append("no independent compatible identity signal")

        if relevance is RelevanceLabel.NEGATIVE:
            automatic_use = AutomaticUseLabel.DISALLOWED
        elif relevance is RelevanceLabel.AMBIGUOUS:
            automatic_use = AutomaticUseLabel.REVIEW
        elif kind is DocumentKind.SUPPLEMENT:
            automatic_use = AutomaticUseLabel.DISALLOWED
        elif relevance is RelevanceLabel.POSITIVE and kind in {
            DocumentKind.SUMMARY_PROSPECTUS,
            DocumentKind.STATUTORY_PROSPECTUS,
        }:
            automatic_use = AutomaticUseLabel.ALLOWED
        else:
            automatic_use = AutomaticUseLabel.REVIEW

        return ShadowEvaluation(
            policy_version=POLICY_VERSION,
            relevance=relevance,
            document_kind=kind,
            automatic_use=automatic_use,
            signals=signals,
            missing_evidence=list(dict.fromkeys(missing)),
            contradictions=list(dict.fromkeys(contradictions)),
        )
