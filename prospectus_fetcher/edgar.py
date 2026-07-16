"""Locate the latest prospectus filing for a resolved fund and its document URL.

The hard part of this workflow lives here. Two facts drive the design:

1. **One registrant (CIK) holds many funds.** "Vanguard Admiral Funds" can file a
   dozen different funds' 497Ks on the same day, so picking the registrant's
   "most recent 497K" can silently return the *wrong* fund. When we know the
   fund's ``seriesId`` we therefore filter filings by series via browse-edgar.
2. **"Prospectus" is several form types.** We rank them by an explicit, swappable
   policy and pick the best available, normalising amended (``/A``) variants.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import config
from .models import Filing, IdentityLevel, ResolvedFund
from .resolver import Resolver
from .sec_client import SECClient

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

_XBRL_VIEWER = re.compile(r"^r\d+\.htm", re.IGNORECASE)


@dataclass
class FilingRef:
    """A lightweight reference to one filing, from either Atom or submissions."""

    form: str                              # actual form as filed, e.g. "497K/A"
    date: str                              # "YYYY-MM-DD"
    accession: str                         # "0001193125-25-325229"
    primary_document: Optional[str] = None  # known only from submissions JSON
    description: Optional[str] = None       # primaryDocDescription (fund name)
    filing_href: Optional[str] = None       # filing index page (from Atom)


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


def parse_atom(xml_text: str) -> List[FilingRef]:
    """Parse a browse-edgar Atom feed into FilingRefs.

    Uses ``{*}`` namespace wildcards because EDGAR nests its (non-Atom) elements
    under the feed's default Atom namespace.
    """
    refs: List[FilingRef] = []
    # ElementTree rejects a `str` carrying an encoding declaration; strip it.
    if isinstance(xml_text, str):
        xml_text = re.sub(r"^\s*<\?xml.*?\?>\s*", "", xml_text, count=1, flags=re.DOTALL)
    root = ET.fromstring(xml_text)
    # `{*}` namespace wildcard works in findall/find, not in iter().
    for entry in root.findall(".//{*}entry"):
        accession = entry.findtext(".//{*}accession-number")
        date = entry.findtext(".//{*}filing-date")
        form = entry.findtext(".//{*}filing-type")
        href = entry.findtext(".//{*}filing-href")
        if not form:  # fall back to the <category term="..."> label
            category = entry.find("{*}category")
            form = category.get("term") if category is not None else None
        if accession and form and date:
            refs.append(
                FilingRef(
                    form=form.strip(),
                    date=date.strip(),
                    accession=accession.strip(),
                    filing_href=(href.strip() if href else None),
                )
            )
    return refs


class EdgarClient:
    def __init__(self, client: SECClient, resolver: Optional[Resolver] = None) -> None:
        self.client = client
        self.resolver = resolver
        self._submissions_cache: Dict[int, dict] = {}

    # -- public API --------------------------------------------------------
    def find_prospectus(self, fund: ResolvedFund) -> Optional[Filing]:
        """Find the latest prospectus filing for a fund and resolve its doc URL."""
        if fund.series_id:
            refs = self._series_filings(fund.series_id)
            selection = select_filing(refs)
            if selection is None:
                logger.debug("No prospectus in the series feed for %s; retrying per-form.", fund.ticker)
                selection = select_filing(self._series_filings_by_form(fund.series_id))
            series_id = fund.series_id
        else:
            if self.resolver is not None and len(self.resolver.series_for_cik(fund.cik)) > 1:
                logger.warning(
                    "multi-series registrant detected via ticker.txt; series filtering "
                    "unavailable; proceeding with registrant-level filing lookup."
                )
            refs = self._recent_refs(self._get_submissions(fund.cik))
            selection = select_filing(refs)
            series_id = None

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

        doc_name, heuristic_used = self._resolve_primary_doc(fund.cik, chosen)
        accession_nodash = chosen.accession.replace("-", "")
        doc_url = (
            config.ARCHIVES_BASE.format(cik=fund.cik, accession_nodash=accession_nodash)
            + f"/{doc_name}"
        )
        if series_id:
            identity_level = IdentityLevel.SERIES
            identity_evidence = [
                f"candidate filing selected from the EDGAR series feed for {series_id}"
            ]
        else:
            identity_level = IdentityLevel.REGISTRANT
            identity_evidence = [
                f"candidate filing selected from registrant submissions for CIK {fund.cik}"
            ]

        warnings = []
        if heuristic_used:
            warnings.append("primary document selected by fallback size heuristic")

        return Filing(
            registrant_cik=fund.cik,
            accession=chosen.accession,
            form=chosen.form,
            date=chosen.date,
            series_id=series_id,
            filing_detail_url=chosen.filing_href,
            doc_url=doc_url,
            fund_name=chosen.description,
            selection_reason=reason,
            heuristic_used=heuristic_used,
            identity_level=identity_level,
            identity_evidence=identity_evidence,
            warnings=warnings,
        )

    # -- series-filtered filing lookup -------------------------------------
    def _series_filings(self, series_id: str) -> List[FilingRef]:
        """All recent filings for a single fund series (one request, all forms)."""
        params = {
            "action": "getcompany",
            "CIK": series_id,
            "dateb": "",
            "owner": "include",
            "count": 100,
            "output": "atom",
        }
        return parse_atom(self.client.get_text(config.BROWSE_EDGAR_URL, params=params))

    def _series_filings_by_form(self, series_id: str) -> List[FilingRef]:
        """Fallback: query each prospectus form explicitly if the feed missed them."""
        collected: List[FilingRef] = []
        for form in PROSPECTUS_FORM_PRIORITY:
            params = {
                "action": "getcompany",
                "CIK": series_id,
                "type": form,
                "dateb": "",
                "owner": "include",
                "count": 40,
                "output": "atom",
            }
            collected.extend(parse_atom(self.client.get_text(config.BROWSE_EDGAR_URL, params=params)))
        return collected

    # -- submissions (registrant-level) ------------------------------------
    def _get_submissions(self, cik: int) -> dict:
        if cik not in self._submissions_cache:
            self._submissions_cache[cik] = self.client.get_json(
                config.SUBMISSIONS_URL.format(cik=cik)
            )
        return self._submissions_cache[cik]

    @staticmethod
    def _recent_refs(submissions: dict) -> List[FilingRef]:
        recent = submissions["filings"]["recent"]
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        pdocs = recent.get("primaryDocument", [""] * len(forms))
        descs = recent.get("primaryDocDescription", [""] * len(forms))
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

    # -- primary document resolution (3-tier) ------------------------------
    def _resolve_primary_doc(self, cik: int, chosen: FilingRef) -> Tuple[str, bool]:
        """Return (document filename, heuristic_used)."""
        if chosen.primary_document:
            return chosen.primary_document, False

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
            batch = self.client.get_json(
                config.SUBMISSIONS_BATCH_URL.format(name=batch_meta["name"])
            )
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
        items = self.client.get_json(url).get("directory", {}).get("item", [])
        candidates: List[Tuple[int, str]] = []
        for item in items:
            name = item.get("name", "")
            low = name.lower()
            if not (low.endswith(".htm") or low.endswith(".html")):
                continue
            if _XBRL_VIEWER.match(low) or "-index." in low:
                continue
            candidates.append((int(item.get("size", 0) or 0), name))
        if not candidates:
            raise RuntimeError(f"No prospectus document found in filing index for {accession}")
        candidates.sort(reverse=True)
        return candidates[0][1]
