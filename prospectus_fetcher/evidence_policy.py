"""Location-aware shadow evidence policy for Milestone 6 evaluation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import List, Optional, Set

from .corpus import AutomaticUseLabel, RelevanceLabel
from .filing_identity import FilingIdentityMetadata, IdentityMetadataSource
from .models import DocumentKind, DocumentScope


POLICY_VERSION = "m6.2-shadow-v6"

_SUPPLEMENT_SIGNALS = (
    "prospectus supplement",
    "supplement dated",
    "supplement to",
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
_UNIVERSAL_FUND_SCOPE_RE = re.compile(
    r"\b(?:all|each)\s+(?:of\s+the\s+)?(?:funds?|series)\b|"
    r"\bfor\s+each\s+(?:fund|series)\b",
    re.IGNORECASE,
)
_CLOSED_FUND_SCOPE_START_RE = re.compile(
    r"\bfor\s+the\s+following\s+funds?\b[^:]{0,180}:",
    re.IGNORECASE,
)
_CLOSED_FUND_SCOPE_END_RE = re.compile(
    r"\s(?:effective\s+(?:immediately|on)|accordingly|"
    r"\d{1,2}\.\s+[A-Z])",
    re.IGNORECASE,
)
_SAI_SUBSTANTIVE_SIGNALS = (
    "investment advisory and other services",
    "control persons and principal holders",
    "portfolio transactions and brokerage",
    "description of the trust",
    "additional purchase and redemption information",
    "distribution and service plans",
)
_REGISTRATION_FORMS = {"485BPOS", "485APOS", "N-1A"}


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
class DocumentContentProfile:
    contains_summary_prospectus: bool
    contains_statutory_prospectus: bool
    contains_sai: bool
    is_supplement: bool

    @property
    def contains_complete_prospectus(self) -> bool:
        return (
            self.contains_summary_prospectus
            or self.contains_statutory_prospectus
        )


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
    document_scope: DocumentScope
    content_profile: DocumentContentProfile
    automatic_use: AutomaticUseLabel
    signals: List[EvidenceSignal] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        value = asdict(self)
        value["relevance"] = self.relevance.value
        value["document_kind"] = self.document_kind.value
        value["document_scope"] = self.document_scope.value
        value["automatic_use"] = self.automatic_use.value
        for index, signal in enumerate(self.signals):
            value["signals"][index]["strength"] = signal.strength.value
            value["signals"][index]["location"] = signal.location.value
        return value


class ShadowSelectionStatus(str, Enum):
    SELECTED = "selected"
    REVIEW = "review"


@dataclass(frozen=True)
class ShadowCandidate:
    name: str
    evaluation: Optional[ShadowEvaluation] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ShadowCandidateSelection:
    status: ShadowSelectionStatus
    selected_name: Optional[str]
    reason: str
    qualified_names: List[str] = field(default_factory=list)


_SCOPE_RANK = {
    DocumentScope.TICKER_SPECIFIC: 0,
    DocumentScope.MULTI_FUND: 1,
    DocumentScope.REGISTRANT_WIDE: 2,
    DocumentScope.UNKNOWN: 3,
}


def select_shadow_candidate(
    candidates: List[ShadowCandidate],
) -> ShadowCandidateSelection:
    """Prefer one uniquely qualified candidate at the narrowest proven scope."""
    errors = [candidate for candidate in candidates if candidate.error]
    if errors:
        return ShadowCandidateSelection(
            status=ShadowSelectionStatus.REVIEW,
            selected_name=None,
            reason=(
                f"{len(errors)} eligible candidate(s) could not be evaluated; "
                "narrowest-candidate uniqueness is unproven"
            ),
        )

    qualified = [
        candidate
        for candidate in candidates
        if candidate.evaluation
        and candidate.evaluation.automatic_use is AutomaticUseLabel.ALLOWED
    ]
    if not qualified:
        return ShadowCandidateSelection(
            status=ShadowSelectionStatus.REVIEW,
            selected_name=None,
            reason="no candidate is eligible for automatic use",
        )

    best_rank = min(
        _SCOPE_RANK[candidate.evaluation.document_scope]
        for candidate in qualified
        if candidate.evaluation is not None
    )
    best = [
        candidate
        for candidate in qualified
        if candidate.evaluation
        and _SCOPE_RANK[candidate.evaluation.document_scope] == best_rank
    ]
    qualified_names = [candidate.name for candidate in qualified]
    if len(best) != 1:
        return ShadowCandidateSelection(
            status=ShadowSelectionStatus.REVIEW,
            selected_name=None,
            reason=(
                f"{len(best)} equally narrow candidates qualify; automatic "
                "selection would be ambiguous"
            ),
            qualified_names=qualified_names,
        )

    selected = best[0]
    return ShadowCandidateSelection(
        status=ShadowSelectionStatus.SELECTED,
        selected_name=selected.name,
        reason=(
            f"selected the only qualified {selected.evaluation.document_scope.value} "
            "candidate"
        ),
        qualified_names=qualified_names,
    )


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._element_stack: List[tuple[str, bool]] = []
        self._hidden_depth = 0
        self._head_depth = 0
        self._title_depth = 0
        self._title_parts: List[str] = []
        self.title = ""
        self._visible_parts: List[str] = []
        self._heading_depth = 0
        self._heading_parts: List[str] = []
        self.headings: List[str] = []
        self._table_depth = 0
        self._row_depth = 0
        self._row_parts: List[str] = []
        self.table_rows: List[str] = []
        self._fact_name: Optional[str] = None
        self._fact_parts: List[str] = []
        self.xbrl_facts: dict[str, List[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.lower()
        attributes = {name.lower(): value or "" for name, value in attrs}
        style = attributes.get("style", "").lower()
        own_hidden = (
            low in {"script", "style", "noscript", "ix:header", "ix:hidden"}
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
            or bool(re.search(r"\bdisplay\s*:\s*none\b", style))
            or bool(re.search(r"\bvisibility\s*:\s*hidden\b", style))
        )
        self._element_stack.append((low, own_hidden))
        if own_hidden:
            self._hidden_depth += 1

        if low == "head":
            self._head_depth += 1
        if low == "title":
            self._title_depth += 1
            if self._title_depth == 1:
                self._title_parts = []
        if low == "ix:nonnumeric":
            name = attributes.get("name", "").lower()
            if name:
                self._fact_name = name
                self._fact_parts = []

        if self._hidden_depth:
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
        if not self._hidden_depth:
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

        if low == "ix:nonnumeric" and self._fact_name:
            value = _normalize_space(" ".join(self._fact_parts))
            if value:
                self.xbrl_facts.setdefault(self._fact_name, []).append(value)
            self._fact_name = None
            self._fact_parts = []
        if low == "title" and self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                self.title = _normalize_space(" ".join(self._title_parts))
        if low == "head" and self._head_depth:
            self._head_depth -= 1

        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == low:
                removed = self._element_stack[index:]
                del self._element_stack[index:]
                self._hidden_depth -= sum(hidden for _, hidden in removed)
                break

    def handle_data(self, data: str) -> None:
        if self._fact_name and data.strip():
            self._fact_parts.append(data)
        if not data.strip():
            return
        if self._title_depth:
            self._title_parts.append(data)
            return
        if self._hidden_depth or self._head_depth:
            return
        self._visible_parts.append(data)
        if self._heading_depth:
            self._heading_parts.append(data)
        if self._row_depth:
            self._row_parts.append(data)

    @property
    def visible_text(self) -> str:
        return _normalize_space(" ".join(self._visible_parts))


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


def _closed_fund_scope_region(text: str) -> Optional[str]:
    """Return a document-declared exhaustive fund list near the cover."""
    cover = text[:30_000]
    start = _CLOSED_FUND_SCOPE_START_RE.search(cover)
    if start is None:
        return None
    remainder = cover[start.end() :]
    end = _CLOSED_FUND_SCOPE_END_RE.search(remainder)
    region = remainder[: end.start()] if end else remainder[:10_000]
    normalized = _normalize_space(region)
    if normalized.lower().count("fund") < 2:
        return None
    return normalized


def _appendix_contains_subject(
    text: str,
    ticker: str,
    expected_series_name: Optional[str],
) -> bool:
    lower = text.lower()
    starts = [match.start() for match in re.finditer(r"\bappendix\s+a\b", lower)]
    return any(
        _contains_identifier(text[start : start + 500_000], ticker)
        or _contains_name(text[start : start + 500_000], expected_series_name)
        for start in starts
    )


def _document_content_profile(
    parser: _StructureParser,
    text: str,
    kind_region: str,
    supplement_signal: Optional[str],
    declared_form: Optional[str],
) -> DocumentContentProfile:
    lower = text.lower()
    sections = _section_matches(lower)
    title_and_headings = (parser.title + " " + " ".join(parser.headings)).lower()
    sai_position = kind_region.find("statement of additional information")
    complete_marker_positions = [
        kind_region.find(marker)
        for marker in (
            "summary prospectus",
            "statutory prospectus",
            "prospectus dated",
        )
        if kind_region.find(marker) >= 0
    ]
    if "prospectus" in parser.title.lower():
        complete_marker_positions.append(0)
    top_level_sai = bool(
        sai_position >= 0
        and (
            not complete_marker_positions
            or sai_position < min(complete_marker_positions)
        )
    )
    body_sai_position = lower.find("statement of additional information")
    sai_tail = lower[sai_position : sai_position + 1_000_000] if sai_position >= 0 else ""
    if body_sai_position >= 0:
        sai_tail = lower[body_sai_position : body_sai_position + 1_000_000]
    substantive_sai_count = sum(
        signal in sai_tail for signal in _SAI_SUBSTANTIVE_SIGNALS
    )
    contains_sai = bool(
        body_sai_position >= 0
        and (
            top_level_sai
            or (
                "statement of additional information" in title_and_headings
                and substantive_sai_count >= 1
            )
            or substantive_sai_count >= 2
        )
    )

    normalized_form = (declared_form or "").strip().upper()
    base_form = normalized_form.removesuffix("/A")
    xbrl_types = {
        value.strip().upper().removesuffix("/A")
        for value in parser.xbrl_facts.get("dei:documenttype", [])
    }
    registration_form = (
        base_form in _REGISTRATION_FORMS
        or bool(xbrl_types.intersection(_REGISTRATION_FORMS))
    )
    explicit_statutory_marker = (
        "statutory prospectus" in kind_region
        or "prospectus dated" in kind_region
        or any(signal in lower[:100_000] for signal in _REGISTRATION_SIGNALS)
        or (
            "prospectus" in parser.title.lower()
            and "summary prospectus" not in kind_region
        )
    )

    is_supplement = supplement_signal is not None
    contains_summary = bool(
        not is_supplement
        and "summary prospectus" in kind_region
        and len(sections) >= 2
    )
    contains_statutory = bool(
        not is_supplement
        and not (contains_summary and not registration_form)
        and len(sections) >= 2
        and (
            explicit_statutory_marker
            or (
                registration_form
                and len(sections) >= 4
                and "prospectus" in lower
            )
        )
    )
    return DocumentContentProfile(
        contains_summary_prospectus=contains_summary,
        contains_statutory_prospectus=contains_statutory,
        contains_sai=contains_sai,
        is_supplement=is_supplement,
    )


def _derived_document_kind(profile: DocumentContentProfile) -> DocumentKind:
    if profile.is_supplement:
        return DocumentKind.SUPPLEMENT
    content_type_count = sum(
        (
            profile.contains_summary_prospectus,
            profile.contains_statutory_prospectus,
            profile.contains_sai,
        )
    )
    if profile.contains_complete_prospectus and content_type_count >= 2:
        return DocumentKind.COMBINED_PROSPECTUS_PACKAGE
    if profile.contains_summary_prospectus:
        return DocumentKind.SUMMARY_PROSPECTUS
    if profile.contains_statutory_prospectus:
        return DocumentKind.STATUTORY_PROSPECTUS
    if profile.contains_sai:
        return DocumentKind.STATEMENT_OF_ADDITIONAL_INFORMATION
    return DocumentKind.UNKNOWN


def _matched_series_ids(
    metadata: FilingIdentityMetadata,
    text: str,
) -> Set[str]:
    return {
        series.series_id
        for series in metadata.series
        if series.name and _contains_name(text, series.name)
    }


class ShadowEvidencePolicy:
    """Produce a non-controlling structured evidence recommendation."""

    def evaluate(
        self,
        content: bytes,
        ticker: str,
        class_id: Optional[str],
        series_id: Optional[str],
        metadata: FilingIdentityMetadata,
        requested_cik: Optional[int] = None,
        registrant_cik: Optional[int] = None,
        known_series_count: Optional[int] = None,
        declared_form: Optional[str] = None,
    ) -> ShadowEvaluation:
        ticker = ticker.upper()
        parser = _StructureParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        parser.close()
        text = parser.visible_text
        lower = text.lower()
        front = _front_matter(text)
        heading_text = " ".join(parser.headings)
        title_lower = parser.title.lower()
        kind_region = (parser.title + " " + front + " " + heading_text).lower()
        supplement_signal = next(
            (
                signal
                for signal in _SUPPLEMENT_SIGNALS
                if signal in kind_region
            ),
            None,
        )
        content_profile = _document_content_profile(
            parser,
            text,
            kind_region,
            supplement_signal,
            declared_form,
        )
        kind = _derived_document_kind(content_profile)

        signals: List[EvidenceSignal] = []
        missing: List[str] = []
        contradictions: List[str] = []
        expected_series_name: Optional[str] = None
        expected_class_name: Optional[str] = None
        metadata_label = (
            "SEC filing header"
            if metadata.source is IdentityMetadataSource.SUBMISSION_HEADER
            else "SEC filing-detail page"
        )
        metadata_label_lower = metadata_label.lower()

        metadata_match = None
        if class_id:
            metadata_match = metadata.find_class(class_id)
            if metadata.series and metadata_match is None:
                contradictions.append(
                    f"{metadata_label} does not include requested class ID {class_id}"
                )
            elif metadata_match is None:
                missing.append(f"{metadata_label_lower} contains no class metadata")
            else:
                series, class_identity = metadata_match
                expected_series_name = series.name
                expected_class_name = class_identity.name
                signals.append(
                    EvidenceSignal(
                        "metadata_class_match",
                        EvidenceStrength.STRONG,
                        EvidenceLocation.FILING_METADATA,
                        f"{metadata_label_lower} associates {class_id} with "
                        f"{series.series_id}",
                    )
                )
                if series_id and series.series_id != series_id:
                    contradictions.append(
                        f"requested series {series_id} conflicts with "
                        f"{metadata_label_lower} "
                        f"series {series.series_id} for {class_id}"
                    )
                if class_identity.ticker and class_identity.ticker != ticker:
                    contradictions.append(
                        f"{metadata_label_lower} maps {class_id} to "
                        f"{class_identity.ticker}, not {ticker}"
                    )
                elif class_identity.ticker == ticker:
                    signals.append(
                        EvidenceSignal(
                            "metadata_ticker_match",
                            EvidenceStrength.STRONG,
                            EvidenceLocation.FILING_METADATA,
                            f"{metadata_label_lower} maps {class_id} to ticker {ticker}",
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

        series_name_rows = (
            [
                row
                for row in parser.table_rows
                if _contains_name(row, expected_series_name)
            ]
            if expected_series_name
            else []
        )
        series_name_in_heading = bool(
            expected_series_name
            and _contains_name(heading_text, expected_series_name)
        )
        series_name_in_subject_front = bool(
            expected_series_name
            and _contains_name(front[:5_000], expected_series_name)
        )
        supplement_series_subject = bool(
            supplement_signal
            and expected_series_name
            and (
                series_name_rows
                or series_name_in_heading
                or series_name_in_subject_front
                or _contains_name(parser.title, expected_series_name)
            )
        )
        supplement_appendix_subject = bool(
            supplement_signal
            and _appendix_contains_subject(text, ticker, expected_series_name)
        )
        if expected_series_name and _contains_name(text, expected_series_name):
            if series_name_rows:
                series_location = EvidenceLocation.CLASS_TABLE
            elif series_name_in_heading:
                series_location = EvidenceLocation.HEADING
            elif series_name_in_subject_front:
                series_location = EvidenceLocation.FRONT_MATTER
            else:
                series_location = EvidenceLocation.BODY
            signal_code = (
                "supplement_appendix_subject"
                if supplement_appendix_subject
                else (
                    "supplement_series_subject"
                    if supplement_series_subject
                    else "series_name_match"
                )
            )
            signals.append(
                EvidenceSignal(
                    signal_code,
                    (
                        EvidenceStrength.STRONG
                        if supplement_series_subject or supplement_appendix_subject
                        else EvidenceStrength.SUPPORTING
                    ),
                    (
                        EvidenceLocation.CLASS_TABLE
                        if supplement_appendix_subject
                        else series_location
                    ),
                    f"document contains SEC series name {expected_series_name!r}",
                )
            )
            if supplement_series_subject or supplement_appendix_subject:
                strong_document_identity = True
        elif expected_series_name:
            if supplement_appendix_subject:
                signals.append(
                    EvidenceSignal(
                        "supplement_appendix_subject",
                        EvidenceStrength.STRONG,
                        EvidenceLocation.CLASS_TABLE,
                        f"Appendix A contains requested ticker {ticker}",
                    )
                )
                strong_document_identity = True
            else:
                missing.append(
                    f"document does not contain SEC series name {expected_series_name!r}"
                )
        elif supplement_appendix_subject:
            signals.append(
                EvidenceSignal(
                    "supplement_appendix_subject",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.CLASS_TABLE,
                    f"Appendix A contains requested ticker {ticker}",
                )
            )
            strong_document_identity = True

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

        closed_fund_scope = (
            _closed_fund_scope_region(text) if supplement_signal else None
        )
        if closed_fund_scope:
            signals.append(
                EvidenceSignal(
                    "closed_fund_scope_list",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.FRONT_MATTER,
                    "document declares an exhaustive list of covered funds",
                )
            )
            if not _contains_identifier(
                closed_fund_scope, ticker
            ) and not _contains_name(closed_fund_scope, expected_series_name):
                contradictions.append(
                    "document's exhaustive covered-fund list excludes requested "
                    f"ticker {ticker} and SEC series {expected_series_name!r}"
                )

        registrant_name_in_front = _contains_name(front, metadata.registrant_name)
        registrant_name_in_heading = _contains_name(
            heading_text, metadata.registrant_name
        )
        registrant_name_match = _contains_name(text, metadata.registrant_name)
        if registrant_name_match:
            signals.append(
                EvidenceSignal(
                    "registrant_name_match",
                    EvidenceStrength.SUPPORTING,
                    (
                        EvidenceLocation.FRONT_MATTER
                        if registrant_name_in_front
                        else (
                            EvidenceLocation.HEADING
                            if registrant_name_in_heading
                            else EvidenceLocation.BODY
                        )
                    ),
                    f"document contains SEC registrant name {metadata.registrant_name!r}",
                )
            )

        universal_scope_region = (
            parser.title + " " + front[:5_000] + " " + heading_text
        )
        if (
            supplement_signal
            and metadata_match is not None
            and (registrant_name_in_front or registrant_name_in_heading)
            and _UNIVERSAL_FUND_SCOPE_RE.search(universal_scope_region)
        ):
            signals.append(
                EvidenceSignal(
                    "supplement_universal_registrant_scope",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.FRONT_MATTER,
                    (
                        "supplement states that it applies to all funds or series "
                        "of the SEC-identified registrant"
                    ),
                )
            )
            strong_document_identity = True

        xbrl_document_types = parser.xbrl_facts.get("dei:documenttype", [])
        normalized_form = declared_form.strip().upper() if declared_form else ""
        if normalized_form:
            signals.append(
                EvidenceSignal(
                    "declared_filing_form",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.FILING_METADATA,
                    f"selected filing declares form {normalized_form}",
                )
            )
        for document_type in xbrl_document_types:
            signals.append(
                EvidenceSignal(
                    "xbrl_document_type",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.FILING_METADATA,
                    f"inline XBRL declares document type {document_type}",
                )
            )
        if "prospectus" in title_lower:
            signals.append(
                EvidenceSignal(
                    "prospectus_html_title",
                    EvidenceStrength.SUPPORTING,
                    EvidenceLocation.HEADING,
                    f"HTML title identifies prospectus content: {parser.title!r}",
                )
            )
        profile_signals = (
            (
                content_profile.contains_summary_prospectus,
                "contains_summary_prospectus",
                "document contains complete summary-prospectus structure",
            ),
            (
                content_profile.contains_statutory_prospectus,
                "contains_statutory_prospectus",
                "document contains complete statutory-prospectus structure",
            ),
            (
                content_profile.contains_sai,
                "contains_sai",
                "document contains substantive SAI material",
            ),
            (
                content_profile.is_supplement,
                "is_top_level_supplement",
                "document presents itself as a top-level supplement",
            ),
        )
        for present, code, detail in profile_signals:
            if present:
                signals.append(
                    EvidenceSignal(
                        code,
                        EvidenceStrength.SUPPORTING,
                        EvidenceLocation.HEADING,
                        detail,
                    )
                )

        complete_kind = content_profile.contains_complete_prospectus
        if (
            not class_id
            and not series_id
            and requested_cik is not None
            and requested_cik == registrant_cik
            and known_series_count == 0
            and not metadata.series
            and (registrant_name_in_front or registrant_name_in_heading)
            and _contains_identifier(text, ticker)
            and complete_kind
            and not contradictions
        ):
            signals.append(
                EvidenceSignal(
                    "registrant_instrument_match",
                    EvidenceStrength.STRONG,
                    EvidenceLocation.FRONT_MATTER,
                    (
                        f"SEC ticker mapping and filing both use CIK {requested_cik}; "
                        "the exact registrant name is prominent, the ticker is present, "
                        "and no mutual-fund series are known for the CIK"
                    ),
                )
            )
            strong_document_identity = True

        compatible_identity = any(
            signal.code
            in {
                "metadata_class_match",
                "metadata_ticker_match",
                "series_name_match",
                "supplement_series_subject",
                "supplement_appendix_subject",
                "supplement_universal_registrant_scope",
                "class_name_paired_with_ticker",
                "registrant_name_match",
                "registrant_instrument_match",
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

        signal_codes = {signal.code for signal in signals}
        matched_series = _matched_series_ids(metadata, text)
        appendix_multi_fund_scope = bool(
            supplement_signal
            and re.search(
                r"\bfunds?\s+listed\s+in\s+appendix\s+a\b",
                lower[:30_000],
            )
        )
        if "supplement_universal_registrant_scope" in signal_codes:
            document_scope = DocumentScope.REGISTRANT_WIDE
        elif (
            len(matched_series) > 1
            or closed_fund_scope
            or appendix_multi_fund_scope
        ):
            document_scope = DocumentScope.MULTI_FUND
        elif len(matched_series) == 1:
            document_scope = DocumentScope.TICKER_SPECIFIC
        elif relevance is RelevanceLabel.POSITIVE and strong_document_identity:
            document_scope = DocumentScope.TICKER_SPECIFIC
        else:
            document_scope = DocumentScope.UNKNOWN

        if relevance is RelevanceLabel.NEGATIVE:
            automatic_use = AutomaticUseLabel.DISALLOWED
        elif relevance is RelevanceLabel.AMBIGUOUS:
            automatic_use = AutomaticUseLabel.REVIEW
        elif content_profile.is_supplement or (
            content_profile.contains_sai
            and not content_profile.contains_complete_prospectus
        ):
            automatic_use = AutomaticUseLabel.DISALLOWED
        elif (
            relevance is RelevanceLabel.POSITIVE
            and content_profile.contains_complete_prospectus
            and document_scope is not DocumentScope.UNKNOWN
        ):
            automatic_use = AutomaticUseLabel.ALLOWED
        else:
            automatic_use = AutomaticUseLabel.REVIEW

        return ShadowEvaluation(
            policy_version=POLICY_VERSION,
            relevance=relevance,
            document_kind=kind,
            document_scope=document_scope,
            content_profile=content_profile,
            automatic_use=automatic_use,
            signals=signals,
            missing_evidence=list(dict.fromkeys(missing)),
            contradictions=list(dict.fromkeys(contradictions)),
        )
