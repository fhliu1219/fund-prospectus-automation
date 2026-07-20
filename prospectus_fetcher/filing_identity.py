"""Resolve filing-specific fund identity from SEC header and detail metadata."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import parse_qs, urlsplit


_TAG_TEMPLATE = r"<{tag}>\s*([^\r\n<]*)"
_SERIES_BLOCK_RE = re.compile(r"<SERIES>(.*?)</SERIES>", re.IGNORECASE | re.DOTALL)
_CLASS_BLOCK_RE = re.compile(
    r"<CLASS-CONTRACT>(.*?)</CLASS-CONTRACT>", re.IGNORECASE | re.DOTALL
)


class FilingIdentityParseError(ValueError):
    """The SEC submission header did not satisfy the expected identity shape."""


class IdentityMetadataSource(str, Enum):
    SUBMISSION_HEADER = "submission_header"
    FILING_DETAIL_FALLBACK = "filing_detail_fallback"


@dataclass(frozen=True)
class ClassIdentity:
    class_id: str
    name: str
    ticker: Optional[str] = None


@dataclass(frozen=True)
class SeriesIdentity:
    series_id: str
    name: str
    owner_cik: int
    classes: List[ClassIdentity] = field(default_factory=list)


@dataclass(frozen=True)
class FilingIdentityMetadata:
    accession: str
    registrant_name: str
    series: List[SeriesIdentity] = field(default_factory=list)
    source: IdentityMetadataSource = IdentityMetadataSource.SUBMISSION_HEADER
    registrant_cik: Optional[int] = None
    registration_file_numbers: List[str] = field(default_factory=list)

    def find_class(self, class_id: str) -> Optional[tuple[SeriesIdentity, ClassIdentity]]:
        expected = class_id.upper()
        for series in self.series:
            for item in series.classes:
                if item.class_id.upper() == expected:
                    return series, item
        return None


def _tag(block: str, name: str, required: bool = True) -> Optional[str]:
    match = re.search(
        _TAG_TEMPLATE.format(tag=re.escape(name)),
        block,
        re.IGNORECASE,
    )
    value = match.group(1).strip() if match else ""
    if required and not value:
        raise FilingIdentityParseError(f"missing <{name}> value")
    return value or None


def _sgml_source(page: str) -> str:
    unescaped = html.unescape(page)
    comment = re.search(r"<!--(.*?)-->", unescaped, re.DOTALL)
    return comment.group(1) if comment else unescaped


def parse_filing_identity_header(page: str) -> FilingIdentityMetadata:
    """Parse registrant, series, class names, and tickers from an SEC header page."""
    source = _sgml_source(page)
    accession = _tag(source, "ACCESSION-NUMBER")
    registrant_name = _tag(source, "CONFORMED-NAME")
    assert accession is not None and registrant_name is not None
    registrant_cik_text = _tag(source, "CENTRAL-INDEX-KEY", required=False)
    registrant_cik = (
        int(registrant_cik_text)
        if registrant_cik_text and registrant_cik_text.isdigit()
        else None
    )

    parsed_series: List[SeriesIdentity] = []
    seen_series = set()
    seen_classes = set()
    for block_match in _SERIES_BLOCK_RE.finditer(source):
        block = block_match.group(1)
        series_id = _tag(block, "SERIES-ID")
        series_name = _tag(block, "SERIES-NAME")
        owner_cik_text = _tag(block, "OWNER-CIK")
        assert series_id is not None and series_name is not None and owner_cik_text is not None
        if not re.fullmatch(r"S\d{9}", series_id):
            raise FilingIdentityParseError(f"invalid series ID {series_id!r}")
        if not owner_cik_text.isdigit() or int(owner_cik_text) <= 0:
            raise FilingIdentityParseError(f"invalid owner CIK {owner_cik_text!r}")
        if series_id in seen_series:
            raise FilingIdentityParseError(f"duplicate series ID {series_id}")
        seen_series.add(series_id)

        classes: List[ClassIdentity] = []
        for class_match in _CLASS_BLOCK_RE.finditer(block):
            class_block = class_match.group(1)
            class_id = _tag(class_block, "CLASS-CONTRACT-ID")
            class_name = _tag(class_block, "CLASS-CONTRACT-NAME")
            ticker = _tag(
                class_block,
                "CLASS-CONTRACT-TICKER-SYMBOL",
                required=False,
            )
            assert class_id is not None and class_name is not None
            if not re.fullmatch(r"C\d{9}", class_id):
                raise FilingIdentityParseError(f"invalid class ID {class_id!r}")
            if class_id in seen_classes:
                raise FilingIdentityParseError(f"duplicate class ID {class_id}")
            seen_classes.add(class_id)
            classes.append(
                ClassIdentity(
                    class_id=class_id,
                    name=class_name,
                    ticker=ticker.upper() if ticker else None,
                )
            )

        parsed_series.append(
            SeriesIdentity(
                series_id=series_id,
                name=series_name,
                owner_cik=int(owner_cik_text),
                classes=classes,
            )
        )

    return FilingIdentityMetadata(
        accession=accession,
        registrant_name=registrant_name,
        series=parsed_series,
        source=IdentityMetadataSource.SUBMISSION_HEADER,
        registrant_cik=registrant_cik,
    )


@dataclass
class _DetailCell:
    css_class: str
    text: str
    hrefs: List[str]


class _FilingDetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_depth = 0
        self._title_parts: List[str] = []
        self.title = ""
        self._series_table_depth = 0
        self.seen_series_table = False
        self._row: Optional[List[_DetailCell]] = None
        self.rows: List[List[_DetailCell]] = []
        self._cell_class = ""
        self._cell_parts: Optional[List[str]] = None
        self._cell_hrefs: List[str] = []
        self._company_depth = 0
        self._company_parts: List[str] = []
        self.company_names: List[str] = []
        self.company_ciks: List[int] = []
        self.registration_file_numbers: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        low = tag.lower()
        attributes = {name.lower(): value or "" for name, value in attrs}
        css_classes = set(attributes.get("class", "").split())
        if low == "title":
            self._title_depth += 1
            if self._title_depth == 1:
                self._title_parts = []
        if low == "table" and (
            attributes.get("summary") == "Series and Classes/Contracts Table"
            or "tableSeries" in css_classes
        ):
            self._series_table_depth += 1
            self.seen_series_table = True
        elif low == "table" and self._series_table_depth:
            self._series_table_depth += 1
        if low == "tr" and self._series_table_depth:
            self._finish_row()
            self._row = []
        if low == "td" and self._series_table_depth:
            if self._row is None:
                self._row = []
            self._cell_class = attributes.get("class", "")
            self._cell_parts = []
            self._cell_hrefs = []
        if low == "span" and "companyName" in css_classes:
            self._company_depth += 1
            if self._company_depth == 1:
                self._company_parts = []
        if low == "a":
            href = attributes.get("href", "")
            if self._cell_parts is not None and href:
                self._cell_hrefs.append(href)
            if self._company_depth:
                for value in parse_qs(urlsplit(href).query).get("CIK", []):
                    if value.isdigit() and int(value) > 0:
                        cik = int(value)
                        if cik not in self.company_ciks:
                            self.company_ciks.append(cik)
            file_numbers = parse_qs(urlsplit(href).query).get("filenum", [])
            for file_number in file_numbers:
                normalized = file_number.strip()
                if normalized and normalized not in self.registration_file_numbers:
                    self.registration_file_numbers.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low == "title" and self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0:
                self.title = _normalize(" ".join(self._title_parts))
        if low == "td" and self._cell_parts is not None and self._row is not None:
            self._row.append(
                _DetailCell(
                    css_class=self._cell_class,
                    text=_normalize(" ".join(self._cell_parts)),
                    hrefs=list(self._cell_hrefs),
                )
            )
            self._cell_parts = None
            self._cell_hrefs = []
        if low == "tr" and self._series_table_depth:
            self._finish_row()
        if low == "table" and self._series_table_depth:
            self._finish_row()
            self._series_table_depth -= 1
        if low == "span" and self._company_depth:
            self._company_depth -= 1
            if self._company_depth == 0:
                name = _normalize(" ".join(self._company_parts))
                name = re.sub(r"\s*\(Filer\).*$", "", name, flags=re.IGNORECASE)
                if name and name not in self.company_names:
                    self.company_names.append(name)

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._company_depth:
            self._company_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_row()

    def _finish_row(self) -> None:
        if self._row:
            self.rows.append(self._row)
        self._row = None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _identifier_from_cell(cell: _DetailCell, prefix: str) -> Optional[str]:
    expected = re.compile(rf"\b{prefix}\d{{9}}\b", re.IGNORECASE)
    for href in cell.hrefs:
        values = parse_qs(urlsplit(href).query).get("CIK", [])
        for value in values:
            if expected.fullmatch(value):
                return value.upper()
    match = expected.search(cell.text)
    return match.group(0).upper() if match else None


def _cik_from_cell(cell: _DetailCell) -> Optional[int]:
    for href in cell.hrefs:
        values = parse_qs(urlsplit(href).query).get("CIK", [])
        for value in values:
            if value.isdigit() and int(value) > 0:
                return int(value)
    match = re.search(r"\b\d{1,10}\b", cell.text)
    return int(match.group(0)) if match and int(match.group(0)) > 0 else None


def parse_filing_identity_detail(page: str) -> FilingIdentityMetadata:
    """Parse filing identity from an SEC filing-detail HTML page."""
    parser = _FilingDetailParser()
    parser.feed(page)
    parser.close()

    accession_match = re.search(
        r"\b(\d{10}-\d{2}-\d{6})\b",
        parser.title or page[:5_000],
    )
    if not accession_match:
        raise FilingIdentityParseError("filing detail missing accession number")
    accession = accession_match.group(1)
    if not parser.company_names:
        raise FilingIdentityParseError("filing detail missing registrant name")
    registrant_name = parser.company_names[0]

    registrant_cik: Optional[int] = (
        parser.company_ciks[0] if parser.company_ciks else None
    )
    series: List[SeriesIdentity] = []
    current_series_id: Optional[str] = None
    current_series_name: Optional[str] = None
    current_classes: List[ClassIdentity] = []
    seen_series = set()
    seen_classes = set()

    def finish_series() -> None:
        nonlocal current_series_id, current_series_name, current_classes
        if current_series_id is None:
            return
        if registrant_cik is None:
            raise FilingIdentityParseError(
                f"filing detail series {current_series_id} has no owner CIK"
            )
        series.append(
            SeriesIdentity(
                series_id=current_series_id,
                name=current_series_name or "",
                owner_cik=registrant_cik,
                classes=current_classes,
            )
        )
        current_series_id = None
        current_series_name = None
        current_classes = []

    for row in parser.rows:
        if not row:
            continue
        first = row[0]
        first_classes = set(first.css_class.split())
        if "CIKname" in first_classes:
            parsed_cik = _cik_from_cell(first)
            if parsed_cik:
                registrant_cik = parsed_cik
            continue
        if "seriesName" in first_classes:
            finish_series()
            series_id = _identifier_from_cell(first, "S")
            if not series_id:
                raise FilingIdentityParseError("filing detail series row missing series ID")
            if series_id in seen_series:
                raise FilingIdentityParseError(
                    f"filing detail duplicate series ID {series_id}"
                )
            seen_series.add(series_id)
            current_series_id = series_id
            current_series_name = row[2].text if len(row) > 2 else ""
            continue
        if "classContract" in first_classes:
            if current_series_id is None:
                raise FilingIdentityParseError(
                    "filing detail class row appears before a series row"
                )
            class_id = _identifier_from_cell(first, "C")
            if not class_id:
                raise FilingIdentityParseError("filing detail class row missing class ID")
            if class_id in seen_classes:
                raise FilingIdentityParseError(
                    f"filing detail duplicate class ID {class_id}"
                )
            seen_classes.add(class_id)
            current_classes.append(
                ClassIdentity(
                    class_id=class_id,
                    name=row[2].text if len(row) > 2 else "",
                    ticker=(row[3].text.upper() if len(row) > 3 and row[3].text else None),
                )
            )
    finish_series()

    return FilingIdentityMetadata(
        accession=accession,
        registrant_name=registrant_name,
        series=series,
        source=IdentityMetadataSource.FILING_DETAIL_FALLBACK,
        registrant_cik=registrant_cik,
        registration_file_numbers=parser.registration_file_numbers,
    )


def resolve_filing_identity(
    header_page: Optional[str],
    detail_page: Optional[str] = None,
) -> FilingIdentityMetadata:
    """Use the submission header first; fall back only on technical parse failure."""
    header_error: Optional[FilingIdentityParseError] = None
    if header_page is not None:
        try:
            header = parse_filing_identity_header(header_page)
        except FilingIdentityParseError as exc:
            header_error = exc
        else:
            if detail_page is not None:
                detail = parse_filing_identity_detail(detail_page)
                _validate_identity_agreement(header, detail)
            return header

    if detail_page is not None:
        return parse_filing_identity_detail(detail_page)
    if header_error is not None:
        raise header_error
    raise FilingIdentityParseError("no filing identity source was available")


def _validate_identity_agreement(
    header: FilingIdentityMetadata,
    detail: FilingIdentityMetadata,
) -> None:
    if header.accession != detail.accession:
        raise FilingIdentityParseError(
            f"identity sources disagree on accession: {header.accession} != "
            f"{detail.accession}"
        )
    if _normalize(header.registrant_name).casefold() != _normalize(
        detail.registrant_name
    ).casefold():
        raise FilingIdentityParseError("identity sources disagree on registrant name")

    def identity_rows(metadata: FilingIdentityMetadata) -> set[tuple]:
        return {
            (
                item.series_id,
                _normalize(item.name).casefold(),
                item.owner_cik,
                class_identity.class_id,
                _normalize(class_identity.name).casefold(),
                class_identity.ticker,
            )
            for item in metadata.series
            for class_identity in item.classes
        }

    header_rows = identity_rows(header)
    detail_rows = identity_rows(detail)
    if header_rows != detail_rows:
        raise FilingIdentityParseError(
            "identity sources disagree on series/class relationships"
        )
