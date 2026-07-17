"""Parse filing-specific fund identity metadata from SEC submission headers."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional


_TAG_TEMPLATE = r"<{tag}>\s*([^\r\n<]*)"
_SERIES_BLOCK_RE = re.compile(r"<SERIES>(.*?)</SERIES>", re.IGNORECASE | re.DOTALL)
_CLASS_BLOCK_RE = re.compile(
    r"<CLASS-CONTRACT>(.*?)</CLASS-CONTRACT>", re.IGNORECASE | re.DOTALL
)


class FilingIdentityParseError(ValueError):
    """The SEC submission header did not satisfy the expected identity shape."""


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
    )
