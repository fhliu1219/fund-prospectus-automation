"""Locate the latest prospectus filing for a resolved fund and its document URL.

The hard part of this workflow lives here. Two facts drive the design:

1. **One registrant and series can cover multiple ticker-bearing classes.** We
   prefer the requested ``classId`` in EDGAR's Atom feed, fall back to its
   ``seriesId`` only when no qualifying class filing exists, and use registrant
   submissions only when neither fund identifier is available.
2. **"Prospectus" is several form types.** We rank them by an explicit, swappable
   policy and pick the best available, normalising amended (``/A``) variants.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from . import config
from .models import Filing, IdentityLevel, ResolvedFund
from .resolver import Resolver
from .sec_client import SECClient
from .sec_schema import (
    SECResponseSchemaError,
    validate_accession,
    validate_archive_index,
    validate_document_name,
    validate_filing_table,
    validate_iso_date,
    validate_submissions,
)

logger = logging.getLogger(__name__)

# Prospectus form preference, retained from the original stakeholder-confirmed
# policy. This is the single source of truth for filing-type preference.
PROSPECTUS_FORM_PRIORITY = ["497K", "485BPOS", "485APOS", "N-1A", "497"]
_PRIORITY_RANK = {form: i for i, form in enumerate(PROSPECTUS_FORM_PRIORITY)}
_PROSPECTUS_FORMS = set(PROSPECTUS_FORM_PRIORITY)

_FORM_DESCRIPTIONS = {
    "497K": "investor-facing 497K prospectus filing (summary or supplement)",
    "485BPOS": "post-effective prospectus filing (Rule 485(b), in force)",
    "485APOS": "post-effective amendment (Rule 485(a), delayed-effective)",
    "N-1A": "original registration statement",
    "497": "prospectus/supplement filing (may be a partial supplement)",
}

_XBRL_VIEWER = re.compile(r"^r\d+\.html?$", re.IGNORECASE)
_INDEX_DOCUMENT = re.compile(r"(?:^|-)index(?:-headers)?\.html?$", re.IGNORECASE)
_EXHIBIT_FILENAME = re.compile(
    r"^(?:d\d+)?(?:ex|dex|exhibit)[-_.]?\d",
    re.IGNORECASE,
)


@dataclass
class FilingRef:
    """A lightweight reference to one filing, from either Atom or submissions."""

    form: str                              # actual form as filed, e.g. "497K/A"
    date: str                              # "YYYY-MM-DD"
    accession: str                         # "0001193125-25-325229"
    primary_document: Optional[str] = None  # known only from submissions JSON
    description: Optional[str] = None       # primaryDocDescription (fund name)
    filing_href: Optional[str] = None       # filing index page (from Atom)


@dataclass
class ArchiveDocumentRef:
    """One accession file plus the SEC document-table metadata used to filter it."""

    name: str
    url: str
    size_bytes: int
    document_type: str = ""
    description: str = ""
    eligible: bool = False
    exclusion_reason: str = ""


@dataclass
class ArchiveInventory:
    """Complete `index.json` inventory for one accession."""

    index_url: str
    documents: List[ArchiveDocumentRef]
    warnings: List[str]


@dataclass
class _AtomPage:
    refs: List[FilingRef]
    next_url: Optional[str]


@dataclass
class _FilingDocumentMetadata:
    document_type: str
    description: str


class _DocumentTableParser(HTMLParser):
    """Extract rows from EDGAR's `Document Format Files` table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.seen_table = False
        self.in_table = False
        self.table_depth = 0
        self.row: Optional[List[Tuple[str, Optional[str]]]] = None
        self.cell_parts: Optional[List[str]] = None
        self.cell_href: Optional[str] = None
        self.rows: List[List[Tuple[str, Optional[str]]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value for key, value in attrs}
        if tag == "table":
            summary = (attributes.get("summary") or "").strip().lower()
            if not self.in_table and summary == "document format files":
                self.seen_table = True
                self.in_table = True
                self.table_depth = 1
                return
            if self.in_table:
                self.table_depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell_parts = []
            self.cell_href = None
        elif tag == "a" and self.cell_parts is not None:
            self.cell_href = attributes.get("href")

    def handle_data(self, data: str) -> None:
        if self.in_table and self.cell_parts is not None:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.in_table:
            return
        if tag == "td" and self.row is not None and self.cell_parts is not None:
            text = re.sub(r"\s+", " ", " ".join(self.cell_parts)).strip()
            self.row.append((text, self.cell_href))
            self.cell_parts = None
            self.cell_href = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_table = False


def parse_filing_document_table(
    html_text: str,
    source: str = "EDGAR filing detail page",
) -> Dict[str, _FilingDocumentMetadata]:
    """Return document type/description metadata keyed by archive filename."""
    parser = _DocumentTableParser()
    parser.feed(html_text)
    parser.close()
    if not parser.seen_table:
        raise SECResponseSchemaError(source, "$", "missing Document Format Files table")

    documents: Dict[str, _FilingDocumentMetadata] = {}
    for index, row in enumerate(parser.rows):
        if len(row) < 5:
            raise SECResponseSchemaError(
                source,
                f"$.document_table[{index}]",
                f"expected at least 5 columns, received {len(row)}",
            )
        document_text, href = row[2]
        if href:
            document_text = unquote(urlsplit(href).path.rsplit("/", 1)[-1])
        name = validate_document_name(
            document_text,
            source,
            f"$.document_table[{index}].document",
        )
        if name in documents:
            raise SECResponseSchemaError(
                source,
                f"$.document_table[{index}].document",
                f"duplicate document filename {name!r}",
            )
        documents[name] = _FilingDocumentMetadata(
            document_type=row[3][0].strip(),
            description=row[1][0].strip(),
        )
    return documents


def base_form(form: str) -> str:
    """Strip amendment suffixes so '497K/A' ranks with '497K'."""
    return form.split("/")[0].strip().upper()


def select_filing(
    filings: List[FilingRef],
    priority: List[str] = PROSPECTUS_FORM_PRIORITY,
) -> Optional[Tuple[FilingRef, str]]:
    """Pure selection: highest-priority prospectus form, newest within that form.

    Returns ``(filing, selection_reason)`` or ``None`` if no prospectus-type
    filing is present. ``selection_reason`` records *why* the filing won and
    notes any newer lower-priority filing that was deliberately skipped.
    """
    by_base: Dict[str, List[FilingRef]] = {}
    for f in filings:
        bf = base_form(f.form)
        if bf in _PROSPECTUS_FORMS:
            by_base.setdefault(bf, []).append(f)
    if not by_base:
        return None

    for form in priority:
        candidates = by_base.get(form)
        if not candidates:
            continue
        chosen = max(candidates, key=lambda f: (f.date, f.accession))
        return chosen, _selection_reason(form, chosen, filings)
    return None


def _selection_reason(chosen_base: str, chosen: FilingRef, all_filings: List[FilingRef]) -> str:
    descr = _FORM_DESCRIPTIONS.get(chosen_base, chosen_base)
    reason = f"selected {chosen.form} dated {chosen.date}: highest-priority available form — {descr}"
    # "Did we skip a newer but lower-priority form?" check
    newer_lower = [
        f
        for f in all_filings
        if base_form(f.form) in _PROSPECTUS_FORMS
        and _PRIORITY_RANK[base_form(f.form)] > _PRIORITY_RANK[chosen_base]
        and f.date > chosen.date
    ]
    if newer_lower:
        nf = max(newer_lower, key=lambda f: f.date)
        reason += f" (note: a newer {nf.form} dated {nf.date} was skipped to honour the priority policy)"
    return reason


def _parse_atom_page(xml_text: str, source: str) -> _AtomPage:
    """Parse filing references and the validated next-page URL from one feed."""
    refs: List[FilingRef] = []
    # ElementTree rejects a `str` carrying an encoding declaration; strip it.
    if isinstance(xml_text, str):
        xml_text = re.sub(r"^\s*<\?xml.*?\?>\s*", "", xml_text, count=1, flags=re.DOTALL)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SECResponseSchemaError(source, "$", f"invalid XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "feed":
        raise SECResponseSchemaError(source, "$", "expected Atom <feed> root element")
    # `{*}` namespace wildcard works in findall/find, not in iter().
    for index, entry in enumerate(root.findall(".//{*}entry")):
        path = f"$.entry[{index}]"
        accession = entry.findtext(".//{*}accession-number")
        date = entry.findtext(".//{*}filing-date")
        form = entry.findtext(".//{*}filing-type")
        href = entry.findtext(".//{*}filing-href")
        if not form:  # fall back to the <category term="..."> label
            category = entry.find("{*}category")
            form = category.get("term") if category is not None else None
        if not accession:
            raise SECResponseSchemaError(source, path, "missing accession-number")
        if not form:
            raise SECResponseSchemaError(source, path, "missing filing-type/category")
        if not date:
            raise SECResponseSchemaError(source, path, "missing filing-date")
        accession = validate_accession(accession.strip(), source, path + ".accession-number")
        filing_date = validate_iso_date(date.strip(), source, path + ".filing-date")
        refs.append(
            FilingRef(
                form=form.strip(),
                date=filing_date,
                accession=accession,
                filing_href=(href.strip() if href else None),
            )
        )

    next_urls = []
    for index, link in enumerate(root.findall("./{*}link")):
        if (link.get("rel") or "").strip().lower() != "next":
            continue
        href = (link.get("href") or "").strip()
        if not href:
            raise SECResponseSchemaError(source, f"$.link[{index}].href", "empty next URL")
        parsed = urlsplit(href)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.sec.gov"
            or parsed.path != "/cgi-bin/browse-edgar"
        ):
            raise SECResponseSchemaError(
                source,
                f"$.link[{index}].href",
                f"unexpected Atom pagination URL {href!r}",
            )
        next_urls.append(href)
    if len(next_urls) > 1:
        raise SECResponseSchemaError(source, "$.link", "multiple next-page URLs")
    return _AtomPage(refs=refs, next_url=(next_urls[0] if next_urls else None))


def parse_atom(xml_text: str, source: str = "browse-edgar Atom feed") -> List[FilingRef]:
    """Parse one browse-edgar Atom page into filing references."""
    return _parse_atom_page(xml_text, source).refs


class EdgarClient:
    def __init__(self, client: SECClient, resolver: Optional[Resolver] = None) -> None:
        self.client = client
        self.resolver = resolver
        self._submissions_cache: Dict[int, dict] = {}
        self._submission_batch_cache: Dict[str, dict] = {}
        self._atom_cache: Dict[str, List[FilingRef]] = {}
        self._atom_form_cache: Dict[str, List[FilingRef]] = {}
        self._atom_history_cache: Dict[Tuple[str, str, str], List[FilingRef]] = {}

    # -- public API --------------------------------------------------------
    def find_prospectus(self, fund: ResolvedFund) -> Optional[Filing]:
        """Find the latest prospectus filing for a fund and resolve its doc URL."""
        selection: Optional[Tuple[FilingRef, str]] = None
        identity_level = IdentityLevel.UNKNOWN
        identity_evidence: List[str] = []
        warnings: List[str] = []

        if fund.class_id:
            selection = self._select_atom_filing(fund.class_id, "class", fund.ticker)
            if selection is not None:
                identity_level = IdentityLevel.CLASS
                identity_evidence = [
                    f"candidate filing selected from the EDGAR class feed for {fund.class_id}"
                ]
            elif fund.series_id:
                warning = (
                    f"class-level lookup for {fund.class_id} found no qualifying prospectus; "
                    f"fell back to series {fund.series_id}"
                )
                warnings.append(warning)

        if selection is None and fund.series_id:
            selection = self._select_atom_filing(fund.series_id, "series", fund.ticker)
            if selection is not None:
                identity_level = IdentityLevel.SERIES
                identity_evidence = [
                    f"candidate filing selected from the EDGAR series feed for {fund.series_id}"
                ]
                if not fund.class_id:
                    warnings.append("class identifier unavailable; selected at series level")

        if selection is None and not fund.class_id and not fund.series_id:
            if self.resolver is not None and len(self.resolver.series_for_cik(fund.cik)) > 1:
                logger.warning(
                    "multi-series registrant detected via ticker.txt; series filtering "
                    "unavailable; proceeding with registrant-level filing lookup."
                )
            refs = self._recent_refs(self._get_submissions(fund.cik))
            selection = select_filing(refs)
            if selection is None:
                selection = select_filing(self._all_submission_refs(fund.cik))
            if selection is not None:
                identity_level = IdentityLevel.REGISTRANT
                identity_evidence = [
                    f"candidate filing selected from registrant submissions for CIK {fund.cik}"
                ]
                warnings.append(
                    "class and series identifiers unavailable; selected at registrant level"
                )

        if selection is None:
            logger.info("No qualifying prospectus filing found for %s.", fund.ticker)
            return None

        chosen, reason = selection
        if base_form(chosen.form) == "497":
            logger.warning(
                "%s: latest prospectus is a 497 — it may be a partial supplement, "
                "not a full prospectus.",
                fund.ticker,
            )

        return self._build_filing(
            fund=fund,
            ref=chosen,
            identity_level=identity_level,
            identity_evidence=identity_evidence,
            selection_reason=reason,
            warnings=warnings,
        )

    def related_prospectus_refs(
        self,
        fund: ResolvedFund,
        selected: Filing,
        target_dates: Sequence[str] = (),
    ) -> List[FilingRef]:
        """Return all older prospectus references from the selected identity scope.

        This is used only after content validation identifies ``selected`` as a
        supplement. Candidate discovery never broadens from class to series or
        registrant: a base document must preserve the selected filing's identity
        provenance. Atom pages and submissions history are exhausted before this
        method returns. References nearest a supplement's stated base-prospectus
        date are ordered first so the caller can stop after a verified match.
        """
        refs: List[FilingRef]
        if selected.identity_level is IdentityLevel.CLASS and fund.class_id:
            refs = self._all_atom_prospectus_refs(fund.class_id, selected.date)
        elif selected.identity_level is IdentityLevel.SERIES and fund.series_id:
            refs = self._all_atom_prospectus_refs(fund.series_id, selected.date)
        elif selected.identity_level is IdentityLevel.REGISTRANT:
            refs = self._all_submission_refs(fund.cik)
        else:
            return []

        unique: Dict[str, FilingRef] = {}
        for ref in refs:
            if (
                ref.accession != selected.accession
                and ref.date <= selected.date
                and base_form(ref.form) in _PROSPECTUS_FORMS
            ):
                unique.setdefault(ref.accession, ref)

        valid_targets = [date.fromisoformat(value) for value in target_dates]
        if valid_targets:
            return sorted(
                unique.values(),
                key=lambda ref: (
                    min(
                        abs((date.fromisoformat(ref.date) - target).days)
                        for target in valid_targets
                    ),
                    _PRIORITY_RANK[base_form(ref.form)],
                    -date.fromisoformat(ref.date).toordinal(),
                    -int(ref.accession.replace("-", "")),
                ),
            )
        return sorted(
            unique.values(),
            key=lambda ref: (
                ref.date,
                len(PROSPECTUS_FORM_PRIORITY) - _PRIORITY_RANK[base_form(ref.form)],
                ref.accession,
            ),
            reverse=True,
        )

    def resolve_related_prospectus(
        self,
        fund: ResolvedFund,
        selected: Filing,
        ref: FilingRef,
    ) -> Filing:
        """Resolve one related reference lazily when the package builder needs it."""
        return self._build_filing(
            fund=fund,
            ref=ref,
            identity_level=selected.identity_level,
            identity_evidence=list(selected.identity_evidence),
            selection_reason=(
                f"older {ref.form} candidate dated {ref.date} from the same "
                f"{selected.identity_level.value} scope"
            ),
        )

    def accession_document_inventory(self, filing: Filing) -> ArchiveInventory:
        """Return every accession file with conservative recovery eligibility."""
        nodash = filing.accession.replace("-", "")
        base_url = config.ARCHIVES_BASE.format(
            cik=filing.registrant_cik,
            accession_nodash=nodash,
        )
        index_url = base_url + "/index.json"
        items = validate_archive_index(self.client.get_json(index_url), index_url)

        detail_url = base_url + f"/{filing.accession}-index.html"
        warnings: List[str] = []
        metadata: Dict[str, _FilingDocumentMetadata] = {}
        try:
            metadata = parse_filing_document_table(
                self.client.get_text(detail_url),
                detail_url,
            )
        except Exception as exc:
            warnings.append(
                "could not validate the filing document table; automatic sibling "
                f"recovery is disabled: {exc}"
            )

        documents: List[ArchiveDocumentRef] = []
        for item in items:
            name = item["name"]
            document_metadata = metadata.get(name)
            eligible, reason = self._recovery_eligibility(name, document_metadata)
            documents.append(
                ArchiveDocumentRef(
                    name=name,
                    url=base_url + f"/{name}",
                    size_bytes=item["size"],
                    document_type=(
                        document_metadata.document_type if document_metadata else ""
                    ),
                    description=(
                        document_metadata.description if document_metadata else ""
                    ),
                    eligible=eligible,
                    exclusion_reason=reason,
                )
            )
        return ArchiveInventory(index_url=index_url, documents=documents, warnings=warnings)

    @staticmethod
    def _recovery_eligibility(
        name: str,
        metadata: Optional[_FilingDocumentMetadata],
    ) -> Tuple[bool, str]:
        low = name.lower()
        if not (low.endswith(".htm") or low.endswith(".html")):
            return False, "not an HTML document"
        if _INDEX_DOCUMENT.search(low):
            return False, "SEC-generated filing index"
        if _XBRL_VIEWER.fullmatch(low):
            return False, "SEC XBRL rendering"
        if _EXHIBIT_FILENAME.match(low):
            return False, "filename identifies an exhibit"
        if metadata is None:
            return False, "not listed in the SEC filing document table"
        document_type = metadata.document_type.strip().upper()
        if document_type.startswith("EX-") or document_type.startswith("EXHIBIT"):
            return False, f"SEC document type {document_type} is an exhibit"
        if base_form(document_type) not in _PROSPECTUS_FORMS:
            label = document_type or "blank"
            return False, f"SEC document type {label} is not a supported prospectus form"
        return True, "supported prospectus-form HTML document"

    def _build_filing(
        self,
        fund: ResolvedFund,
        ref: FilingRef,
        identity_level: IdentityLevel,
        identity_evidence: List[str],
        selection_reason: str,
        warnings: Optional[List[str]] = None,
    ) -> Filing:
        doc_name, heuristic_used = self._resolve_primary_doc(fund.cik, ref)
        accession_nodash = ref.accession.replace("-", "")
        doc_url = (
            config.ARCHIVES_BASE.format(cik=fund.cik, accession_nodash=accession_nodash)
            + f"/{doc_name}"
        )
        filing_warnings = list(warnings or [])
        if heuristic_used:
            filing_warnings.append("primary document selected by fallback size heuristic")
        return Filing(
            registrant_cik=fund.cik,
            accession=ref.accession,
            form=ref.form,
            date=ref.date,
            series_id=fund.series_id,
            class_id=fund.class_id,
            filing_detail_url=ref.filing_href,
            doc_url=doc_url,
            fund_name=ref.description,
            selection_reason=selection_reason,
            heuristic_used=heuristic_used,
            identity_level=identity_level,
            identity_evidence=list(identity_evidence),
            warnings=filing_warnings,
        )

    # -- class/series-filtered filing lookup -------------------------------
    def _select_atom_filing(
        self, identifier: str, identity_name: str, ticker: str
    ) -> Optional[Tuple[FilingRef, str]]:
        refs = self._atom_filings(identifier)
        selection = select_filing(refs)
        if selection is None:
            logger.debug(
                "No prospectus in the %s feed for %s; retrying per-form for %s.",
                identity_name,
                identifier,
                ticker,
            )
            selection = select_filing(self._atom_filings_by_form(identifier))
        return selection

    def _atom_filings(self, identifier: str) -> List[FilingRef]:
        """All recent filings for one class or series identifier."""
        if identifier in self._atom_cache:
            return self._atom_cache[identifier]
        params = {
            "action": "getcompany",
            "CIK": identifier,
            "dateb": "",
            "owner": "include",
            "count": 100,
            "output": "atom",
        }
        refs = parse_atom(self.client.get_text(config.BROWSE_EDGAR_URL, params=params))
        self._atom_cache[identifier] = refs
        return refs

    def _atom_filings_by_form(self, identifier: str) -> List[FilingRef]:
        """Fallback: query each prospectus form explicitly if the feed missed them."""
        if identifier in self._atom_form_cache:
            return self._atom_form_cache[identifier]
        collected: List[FilingRef] = []
        for form in PROSPECTUS_FORM_PRIORITY:
            params = {
                "action": "getcompany",
                "CIK": identifier,
                "type": form,
                "dateb": "",
                "owner": "include",
                "count": 40,
                "output": "atom",
            }
            collected.extend(parse_atom(self.client.get_text(config.BROWSE_EDGAR_URL, params=params)))
        self._atom_form_cache[identifier] = collected
        return collected

    def _all_atom_prospectus_refs(
        self,
        identifier: str,
        dateb: str = "",
    ) -> List[FilingRef]:
        """Exhaust every form-specific Atom page for one class or series."""
        refs: List[FilingRef] = []
        for form in PROSPECTUS_FORM_PRIORITY:
            refs.extend(self._atom_form_history(identifier, form, dateb))
        return refs

    def _atom_form_history(
        self,
        identifier: str,
        form: str,
        dateb: str,
    ) -> List[FilingRef]:
        key = (identifier, form, dateb)
        if key in self._atom_history_cache:
            return self._atom_history_cache[key]

        params = {
            "action": "getcompany",
            "CIK": identifier,
            "type": form,
            "dateb": dateb.replace("-", ""),
            "owner": "include",
            "count": 100,
            "output": "atom",
        }
        source = f"browse-edgar Atom history for {identifier} form {form}"
        page = _parse_atom_page(
            self.client.get_text(config.BROWSE_EDGAR_URL, params=params),
            source,
        )
        refs = list(page.refs)
        visited = set()
        while page.next_url:
            if page.next_url in visited:
                raise SECResponseSchemaError(source, "$.link.next", "pagination loop detected")
            visited.add(page.next_url)
            query = parse_qs(urlsplit(page.next_url).query)
            next_types = query.get("type")
            if (
                query.get("CIK") != [identifier]
                or next_types not in ([form], [form + "%"])
                or query.get("output") != ["atom"]
            ):
                raise SECResponseSchemaError(
                    source,
                    "$.link.next",
                    "next page changed the class/series identifier, form, or output format",
                )
            page = _parse_atom_page(self.client.get_text(page.next_url), page.next_url)
            refs.extend(page.refs)
        self._atom_history_cache[key] = refs
        return refs

    # -- submissions (registrant-level) ------------------------------------
    def _get_submissions(self, cik: int) -> dict:
        if cik not in self._submissions_cache:
            url = config.SUBMISSIONS_URL.format(cik=cik)
            payload = self.client.get_json(url)
            validate_submissions(payload, url)
            self._submissions_cache[cik] = payload
        return self._submissions_cache[cik]

    @staticmethod
    def _recent_refs(submissions: dict) -> List[FilingRef]:
        recent = submissions["filings"]["recent"]
        return EdgarClient._table_refs(recent, "submissions JSON", "$.filings.recent")

    @staticmethod
    def _table_refs(table: dict, source: str, path: str) -> List[FilingRef]:
        validate_filing_table(table, source, path)
        forms = table.get("form", [])
        dates = table.get("filingDate", [])
        accs = table.get("accessionNumber", [])
        pdocs = table.get("primaryDocument", [""] * len(forms))
        descs = table.get("primaryDocDescription", [""] * len(forms))
        return [
            FilingRef(
                form=forms[i],
                date=dates[i],
                accession=accs[i],
                primary_document=pdocs[i] or None,
                description=descs[i] or None,
            )
            for i in range(len(forms))
        ]

    def _all_submission_refs(self, cik: int) -> List[FilingRef]:
        """Return recent plus every historical submissions batch for a CIK."""
        submissions = self._get_submissions(cik)
        refs = self._recent_refs(submissions)
        for batch_meta in submissions["filings"].get("files", []):
            name = batch_meta["name"]
            batch, source = self._get_submission_batch(name)
            refs.extend(self._table_refs(batch, source, "$"))
        return refs

    def _get_submission_batch(self, name: str) -> Tuple[dict, str]:
        url = config.SUBMISSIONS_BATCH_URL.format(name=name)
        if name not in self._submission_batch_cache:
            batch = self.client.get_json(url)
            validate_filing_table(batch, url, "$")
            self._submission_batch_cache[name] = batch
        return self._submission_batch_cache[name], url

    # -- primary document resolution (3-tier) ------------------------------
    def _resolve_primary_doc(self, cik: int, chosen: FilingRef) -> Tuple[str, bool]:
        """Return (document filename, heuristic_used)."""
        if chosen.primary_document:
            return validate_document_name(
                chosen.primary_document,
                "filing reference",
                "$.primaryDocument",
                allow_relative_path=True,
            ), False

        found = self._lookup_primary_doc(cik, chosen.accession)
        if found is not None:
            doc, desc = found
            if desc and not chosen.description:
                chosen.description = desc
            return doc, False

        return self._largest_doc_from_index(cik, chosen.accession), True

    def _lookup_primary_doc(self, cik: int, accession: str) -> Optional[Tuple[str, Optional[str]]]:
        """Find primaryDocument via submissions: recent first, then older batches."""
        submissions = self._get_submissions(cik)
        recent = submissions["filings"]["recent"]
        hit = self._match_accession(recent, accession)
        if hit is not None:
            return hit
        for batch_meta in submissions["filings"].get("files", []):
            name = batch_meta["name"]
            batch, _ = self._get_submission_batch(name)
            hit = self._match_accession(batch, accession)
            if hit is not None:
                return hit
        return None

    @staticmethod
    def _match_accession(table: dict, accession: str) -> Optional[Tuple[str, Optional[str]]]:
        accs = table.get("accessionNumber", [])
        if accession not in accs:
            return None
        i = accs.index(accession)
        pdoc = (table.get("primaryDocument") or [None] * len(accs))[i]
        desc = (table.get("primaryDocDescription") or [None] * len(accs))[i]
        return (pdoc, desc) if pdoc else None

    def _largest_doc_from_index(self, cik: int, accession: str) -> str:
        """Last resort: pick the largest content .htm in the filing index.

        Heuristic, not guaranteed truth — excludes XBRL viewer files, the index
        page itself, and exhibits. The full prospectus is usually the biggest
        content document.
        """
        nodash = accession.replace("-", "")
        url = config.ARCHIVES_BASE.format(cik=cik, accession_nodash=nodash) + "/index.json"
        items = validate_archive_index(self.client.get_json(url), url)
        candidates: List[Tuple[int, str]] = []
        for item in items:
            name = item.get("name", "")
            low = name.lower()
            if not (low.endswith(".htm") or low.endswith(".html")):
                continue
            if (
                _XBRL_VIEWER.fullmatch(low)
                or _INDEX_DOCUMENT.search(low)
                or _EXHIBIT_FILENAME.match(low)
            ):
                continue
            candidates.append((int(item.get("size", 0) or 0), name))
        if not candidates:
            raise RuntimeError(f"No prospectus document found in filing index for {accession}")
        candidates.sort(reverse=True)
        return candidates[0][1]
