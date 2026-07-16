"""Runtime contracts for the external SEC response shapes used by the pipeline."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List


_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_FUND_ID_RE = re.compile(r"^[SC]\d{9}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SECResponseSchemaError(ValueError):
    """An SEC response was reachable but incompatible with the expected contract."""

    def __init__(self, source: str, path: str, detail: str) -> None:
        self.source = source
        self.path = path
        self.detail = detail
        super().__init__(f"SEC schema error in {source} at {path}: {detail}")


def _error(source: str, path: str, detail: str) -> None:
    raise SECResponseSchemaError(source, path, detail)


def _mapping(value: Any, source: str, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _error(source, path, f"expected object, received {type(value).__name__}")
    return value


def _list(value: Any, source: str, path: str) -> List[Any]:
    if not isinstance(value, list):
        _error(source, path, f"expected array, received {type(value).__name__}")
    return value


def validate_accession(value: Any, source: str, path: str) -> str:
    if not isinstance(value, str) or not _ACCESSION_RE.fullmatch(value.strip()):
        _error(source, path, "expected accession in 0000000000-00-000000 format")
    return value.strip()


def validate_iso_date(value: Any, source: str, path: str) -> str:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value.strip()):
        _error(source, path, "expected ISO date string")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        _error(source, path, f"invalid ISO date {value!r}")
    return parsed.isoformat()


def validate_document_name(
    value: Any,
    source: str,
    path: str,
    allow_relative_path: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(source, path, "expected non-empty document filename")
    name = value.strip()
    parts = name.split("/")
    unsafe = (
        name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or any(part in {"", ".", ".."} for part in parts)
        or (not allow_relative_path and len(parts) != 1)
    )
    if unsafe:
        _error(source, path, f"unsafe document filename {name!r}")
    return name


def validate_mf_tickers(payload: Any, source: str = "company_tickers_mf.json") -> None:
    root = _mapping(payload, source, "$")
    fields = _list(root.get("fields"), source, "$.fields")
    if not all(isinstance(field, str) for field in fields):
        _error(source, "$.fields", "every field name must be a string")

    required = ("cik", "seriesId", "classId", "symbol")
    missing = [field for field in required if field not in fields]
    if missing:
        _error(source, "$.fields", "missing required field(s): " + ", ".join(missing))
    indexes = {field: fields.index(field) for field in required}

    rows = _list(root.get("data"), source, "$.data")
    for row_number, value in enumerate(rows):
        path = f"$.data[{row_number}]"
        row = _list(value, source, path)
        if len(row) < len(fields):
            _error(source, path, f"row has {len(row)} values for {len(fields)} fields")

        cik = row[indexes["cik"]]
        try:
            valid_cik = int(cik)
        except (TypeError, ValueError):
            _error(source, path + ".cik", f"expected positive integer, received {cik!r}")
        if valid_cik <= 0:
            _error(source, path + ".cik", "expected positive integer")

        symbol = row[indexes["symbol"]]
        if not isinstance(symbol, str):
            _error(source, path + ".symbol", "expected ticker string (empty is allowed)")

        for field in ("seriesId", "classId"):
            identifier = row[indexes[field]]
            if identifier in (None, ""):
                continue
            if not isinstance(identifier, str) or not _FUND_ID_RE.fullmatch(identifier):
                _error(source, path + f".{field}", f"invalid SEC fund identifier {identifier!r}")


def validate_ticker_text(text: Any, source: str = "ticker.txt") -> None:
    if not isinstance(text, str):
        _error(source, "$", f"expected text, received {type(text).__name__}")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip().isdigit():
            _error(source, f"line {line_number}", "expected <ticker><TAB><positive CIK>")
        if int(parts[1]) <= 0:
            _error(source, f"line {line_number}", "CIK must be positive")


def validate_filing_table(
    value: Any,
    source: str,
    path: str,
) -> None:
    table = _mapping(value, source, path)
    required = ("accessionNumber", "filingDate", "form")
    arrays: Dict[str, List[Any]] = {}
    for field in required:
        arrays[field] = _list(table.get(field), source, f"{path}.{field}")

    expected_length = len(arrays["accessionNumber"])
    for field in required[1:]:
        if len(arrays[field]) != expected_length:
            _error(
                source,
                f"{path}.{field}",
                f"array length {len(arrays[field])} does not match accessionNumber "
                f"length {expected_length}",
            )

    for field in ("primaryDocument", "primaryDocDescription"):
        if field not in table:
            continue
        values = _list(table[field], source, f"{path}.{field}")
        if len(values) != expected_length:
            _error(
                source,
                f"{path}.{field}",
                f"array length {len(values)} does not match accessionNumber "
                f"length {expected_length}",
            )

    primary_documents = table.get("primaryDocument", [None] * expected_length)
    for index in range(expected_length):
        row_path = f"{path}[{index}]"
        validate_accession(arrays["accessionNumber"][index], source, row_path + ".accessionNumber")
        validate_iso_date(arrays["filingDate"][index], source, row_path + ".filingDate")
        form = arrays["form"][index]
        if not isinstance(form, str) or not form.strip():
            _error(source, row_path + ".form", "expected non-empty form string")
        primary_document = primary_documents[index]
        if primary_document not in (None, ""):
            validate_document_name(
                primary_document,
                source,
                row_path + ".primaryDocument",
                allow_relative_path=True,
            )


def validate_submissions(payload: Any, source: str) -> None:
    root = _mapping(payload, source, "$")
    filings = _mapping(root.get("filings"), source, "$.filings")
    validate_filing_table(filings.get("recent"), source, "$.filings.recent")

    files = filings.get("files", [])
    files = _list(files, source, "$.filings.files")
    for index, value in enumerate(files):
        item = _mapping(value, source, f"$.filings.files[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            _error(source, f"$.filings.files[{index}].name", "expected batch filename")
        validate_document_name(name, source, f"$.filings.files[{index}].name")


def validate_archive_index(payload: Any, source: str) -> List[Dict[str, Any]]:
    root = _mapping(payload, source, "$")
    directory = _mapping(root.get("directory"), source, "$.directory")
    items = _list(directory.get("item"), source, "$.directory.item")
    validated: List[Dict[str, Any]] = []
    for index, value in enumerate(items):
        path = f"$.directory.item[{index}]"
        item = _mapping(value, source, path)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            _error(source, path + ".name", "expected non-empty filename")
        if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            _error(source, path + ".name", f"unsafe archive item name {name!r}")

        raw_size = item.get("size", 0)
        try:
            size = int(raw_size or 0)
        except (TypeError, ValueError):
            _error(source, path + ".size", f"expected non-negative integer, received {raw_size!r}")
        if size < 0:
            _error(source, path + ".size", "expected non-negative integer")
        normalized = dict(item)
        normalized["name"] = name.strip()
        normalized["size"] = size
        validated.append(normalized)
    return validated
