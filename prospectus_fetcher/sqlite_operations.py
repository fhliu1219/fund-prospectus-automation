"""SQLite reference adapter for persistent jobs, artifacts, and review tasks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .models import DocumentVerification, FetchResult
from .operations import (
    IdempotencyConflict,
    ItemStatus,
    JobItem,
    JobStatus,
    LeaseOwnershipError,
    OperationsContractError,
    OperationsJob,
    ReviewStatus,
    ReviewTask,
)
from .package import VALIDATION_POLICIES


SCHEMA_VERSION = 1

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    validation_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_items (
    item_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error TEXT,
    result_json TEXT,
    manifest_json TEXT,
    manifest_path TEXT,
    registrant_cik INTEGER,
    series_id TEXT,
    class_id TEXT,
    identity_level TEXT,
    document_verification TEXT,
    policy_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, ticker),
    UNIQUE(job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES job_items(item_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    accession TEXT NOT NULL,
    source_url TEXT,
    local_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(item_id, role, accession, sha256)
);

CREATE TABLE IF NOT EXISTS review_tasks (
    review_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE REFERENCES job_items(item_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS review_events (
    event_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES review_tasks(review_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT,
    rationale TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_items_claim
    ON job_items(job_id, status, lease_expires_at, ordinal);
CREATE INDEX IF NOT EXISTS idx_review_tasks_status
    ON review_tasks(status, created_at);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _normalize_tickers(tickers: Sequence[str]) -> List[str]:
    values: List[str] = []
    seen = set()
    for raw in tickers:
        ticker = raw.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            values.append(ticker)
    if not values:
        raise OperationsContractError("at least one ticker is required")
    return values


def _request_fingerprint(tickers: Sequence[str], validation_policy: str) -> str:
    payload = json.dumps(
        {
            "tickers": list(tickers),
            "validation_policy": validation_policy,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SQLiteOperationsStore:
    """Durable local implementation of the Milestone 7 operations contract."""

    def __init__(
        self,
        path: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = path
        self.clock = clock
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteOperationsStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _migrate(self) -> None:
        migrations_exist = self.connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        if migrations_exist is not None:
            newest = self.connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if newest is not None and newest > SCHEMA_VERSION:
                raise OperationsContractError(
                    f"database schema version {newest} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )

        with self.connection:
            self.connection.executescript(_MIGRATION_1)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (SCHEMA_VERSION, _timestamp(self.clock())),
            )

    def create_job(
        self,
        idempotency_key: str,
        tickers: Sequence[str],
        validation_policy: str,
    ) -> OperationsJob:
        key = idempotency_key.strip()
        if not key:
            raise OperationsContractError("idempotency_key must not be empty")
        if validation_policy not in VALIDATION_POLICIES:
            raise OperationsContractError(
                f"validation_policy must be one of {', '.join(VALIDATION_POLICIES)}"
            )
        normalized = _normalize_tickers(tickers)
        fingerprint = _request_fingerprint(normalized, validation_policy)
        now = _timestamp(self.clock())

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {key!r} already belongs to a "
                        "different normalized request"
                    )
                self.connection.commit()
                return self._job(existing)

            job_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO jobs(
                    job_id, idempotency_key, request_fingerprint,
                    validation_policy, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    key,
                    fingerprint,
                    validation_policy,
                    JobStatus.SUBMITTED.value,
                    now,
                    now,
                ),
            )
            namespace = uuid.UUID(job_id)
            for ordinal, ticker in enumerate(normalized):
                item_id = str(uuid.uuid5(namespace, ticker))
                self.connection.execute(
                    """
                    INSERT INTO job_items(
                        item_id, job_id, ticker, ordinal, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        job_id,
                        ticker,
                        ordinal,
                        ItemStatus.PENDING.value,
                        now,
                        now,
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> OperationsJob:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown job {job_id}")
        return self._job(row)

    def list_items(self, job_id: str) -> List[JobItem]:
        rows = self.connection.execute(
            "SELECT * FROM job_items WHERE job_id = ? ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        return [self._item(row) for row in rows]

    def claim_next_item(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> Optional[JobItem]:
        owner = worker_id.strip()
        if not owner:
            raise OperationsContractError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise OperationsContractError("lease_seconds must be positive")
        now_value = self.clock()
        now = _timestamp(now_value)
        lease_until = _timestamp(now_value + timedelta(seconds=lease_seconds))

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            job_exists = self.connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job_exists is None:
                raise KeyError(f"unknown job {job_id}")
            row = self.connection.execute(
                """
                SELECT * FROM job_items
                WHERE job_id = ?
                  AND (
                    status = ?
                    OR (
                      status = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    )
                  )
                ORDER BY ordinal
                LIMIT 1
                """,
                (
                    job_id,
                    ItemStatus.PENDING.value,
                    ItemStatus.RUNNING.value,
                    now,
                ),
            ).fetchone()
            if row is None:
                self._refresh_job_status(job_id, now)
                self.connection.commit()
                return None

            self.connection.execute(
                """
                UPDATE job_items
                SET status = ?, attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (
                    ItemStatus.RUNNING.value,
                    owner,
                    lease_until,
                    now,
                    row["item_id"],
                ),
            )
            self.connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (JobStatus.RUNNING.value, now, job_id),
            )
            claimed = self.connection.execute(
                "SELECT * FROM job_items WHERE item_id = ?",
                (row["item_id"],),
            ).fetchone()
            self.connection.commit()
            assert claimed is not None
            return self._item(claimed)
        except Exception:
            self.connection.rollback()
            raise

    def complete_item(
        self,
        item_id: str,
        worker_id: str,
        result: FetchResult,
        manifest: Optional[dict],
    ) -> JobItem:
        now = _timestamp(self.clock())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._owned_running_item(item_id, worker_id)
            if result.ticker != row["ticker"]:
                raise OperationsContractError(
                    f"result ticker {result.ticker!r} does not match claimed "
                    f"ticker {row['ticker']!r}"
                )

            if result.ok:
                if manifest is None:
                    raise OperationsContractError(
                        f"{result.ticker}: successful result requires a manifest"
                    )
                status = (
                    ItemStatus.VERIFIED
                    if result.document_verification
                    is DocumentVerification.VERIFIED
                    else ItemStatus.REVIEW_REQUIRED
                )
                error = None
            else:
                if manifest is not None:
                    raise OperationsContractError(
                        f"{result.ticker}: failed result must not include a manifest"
                    )
                status = ItemStatus.FAILED
                error = result.error or "retrieval failed without an error message"

            if manifest is not None:
                if manifest.get("ticker") != result.ticker:
                    raise OperationsContractError(
                        "manifest ticker does not match the completed result"
                    )
                package = manifest.get("package")
                if not isinstance(package, dict):
                    raise OperationsContractError(
                        "manifest.package must be an object"
                    )
                job_policy = self.connection.execute(
                    "SELECT validation_policy FROM jobs WHERE job_id = ?",
                    (row["job_id"],),
                ).fetchone()["validation_policy"]
                if package.get("validation_policy") != job_policy:
                    raise OperationsContractError(
                        "manifest validation policy does not match the job"
                    )
                if (
                    package.get("verification")
                    != result.document_verification.value
                ):
                    raise OperationsContractError(
                        "manifest verification does not match the completed result"
                    )

            result_json = json.dumps(
                {
                    "ticker": result.ticker,
                    "status": result.status,
                    "form": result.form,
                    "filing_date": result.date,
                    "path": result.path,
                    "error": result.error,
                    "document_kind": result.document_kind.value,
                    "document_verification": result.document_verification.value,
                    "warnings": result.warnings,
                },
                sort_keys=True,
            )
            identity = manifest.get("identity", {}) if manifest else {}
            package = manifest.get("package", {}) if manifest else {}
            self.connection.execute(
                """
                UPDATE job_items
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    error = ?, result_json = ?, manifest_json = ?,
                    manifest_path = ?, registrant_cik = ?, series_id = ?,
                    class_id = ?, identity_level = ?,
                    document_verification = ?, policy_version = ?,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (
                    status.value,
                    error,
                    result_json,
                    (
                        json.dumps(manifest, sort_keys=True)
                        if manifest is not None
                        else None
                    ),
                    result.manifest_path,
                    identity.get("registrant_cik"),
                    identity.get("series_id"),
                    identity.get("class_id"),
                    identity.get("level"),
                    package.get("verification"),
                    package.get("validation_policy_version"),
                    now,
                    item_id,
                ),
            )

            if manifest is not None:
                self._persist_artifacts(item_id, manifest, now)
            if status is ItemStatus.REVIEW_REQUIRED:
                self._create_review_task(item_id, result, now)

            self._refresh_job_status(row["job_id"], now)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._get_item(item_id)

    def fail_item(
        self,
        item_id: str,
        worker_id: str,
        error: str,
    ) -> JobItem:
        message = error.strip()
        if not message:
            raise OperationsContractError("error must not be empty")
        now = _timestamp(self.clock())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._owned_running_item(item_id, worker_id)
            self.connection.execute(
                """
                UPDATE job_items
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    error = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (ItemStatus.FAILED.value, message, now, item_id),
            )
            self._refresh_job_status(row["job_id"], now)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._get_item(item_id)

    def list_review_tasks(
        self,
        status: ReviewStatus = ReviewStatus.PENDING,
    ) -> List[ReviewTask]:
        rows = self.connection.execute(
            """
            SELECT review_tasks.*, job_items.job_id, job_items.ticker
            FROM review_tasks
            JOIN job_items USING(item_id)
            WHERE review_tasks.status = ?
            ORDER BY review_tasks.created_at, review_tasks.review_id
            """,
            (status.value,),
        ).fetchall()
        return [
            ReviewTask(
                review_id=row["review_id"],
                item_id=row["item_id"],
                job_id=row["job_id"],
                ticker=row["ticker"],
                status=ReviewStatus(row["status"]),
                reason=row["reason"],
                created_at=row["created_at"],
                resolved_at=row["resolved_at"],
            )
            for row in rows
        ]

    def artifact_records(self, item_id: str) -> List[dict]:
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE item_id = ? ORDER BY role, artifact_id",
            (item_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def persisted_manifest(self, item_id: str) -> Optional[dict]:
        row = self.connection.execute(
            "SELECT manifest_json FROM job_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown item {item_id}")
        return json.loads(row["manifest_json"]) if row["manifest_json"] else None

    def _owned_running_item(self, item_id: str, worker_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM job_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown item {item_id}")
        if (
            row["status"] != ItemStatus.RUNNING.value
            or row["lease_owner"] != worker_id
        ):
            raise LeaseOwnershipError(
                f"worker {worker_id!r} does not own running item {item_id}"
            )
        return row

    def _get_item(self, item_id: str) -> JobItem:
        row = self.connection.execute(
            "SELECT * FROM job_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown item {item_id}")
        return self._item(row)

    def _persist_artifacts(self, item_id: str, manifest: dict, now: str) -> None:
        documents = manifest.get("documents")
        if not isinstance(documents, list):
            raise OperationsContractError("manifest.documents must be an array")
        namespace = uuid.UUID(item_id)
        for index, document in enumerate(documents):
            if not isinstance(document, dict):
                raise OperationsContractError(
                    f"manifest.documents[{index}] must be an object"
                )
            required = ("role", "accession", "path", "size_bytes", "sha256")
            missing = [name for name in required if document.get(name) in {None, ""}]
            if missing:
                raise OperationsContractError(
                    f"manifest.documents[{index}] missing {', '.join(missing)}"
                )
            identity = "|".join(
                (
                    str(document["role"]),
                    str(document["accession"]),
                    str(document["sha256"]),
                )
            )
            artifact_id = str(uuid.uuid5(namespace, identity))
            path = Path(str(document["path"]))
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise OperationsContractError(
                    f"manifest.documents[{index}] artifact cannot be read: {exc}"
                ) from exc
            actual_size = len(content)
            actual_sha = hashlib.sha256(content).hexdigest()
            if int(document["size_bytes"]) != actual_size:
                raise OperationsContractError(
                    f"manifest.documents[{index}] size mismatch: expected "
                    f"{document['size_bytes']}, received {actual_size}"
                )
            if document["sha256"] != actual_sha:
                raise OperationsContractError(
                    f"manifest.documents[{index}] checksum mismatch: expected "
                    f"{document['sha256']}, received {actual_sha}"
                )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    artifact_id, item_id, role, accession, source_url,
                    local_path, size_bytes, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    item_id,
                    document["role"],
                    document["accession"],
                    document.get("source_url"),
                    document["path"],
                    int(document["size_bytes"]),
                    document["sha256"],
                    now,
                ),
            )

    def _create_review_task(
        self,
        item_id: str,
        result: FetchResult,
        now: str,
    ) -> None:
        review_id = str(uuid.uuid5(uuid.UUID(item_id), "review"))
        reason = "; ".join(result.warnings) or (
            f"package verification is {result.document_verification.value}"
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO review_tasks(
                review_id, item_id, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                review_id,
                item_id,
                ReviewStatus.PENDING.value,
                reason,
                now,
            ),
        )

    def _refresh_job_status(self, job_id: str, now: str) -> None:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM job_items
            WHERE job_id = ?
            GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        if counts.get(ItemStatus.RUNNING.value):
            status = JobStatus.RUNNING
        elif counts.get(ItemStatus.PENDING.value):
            status = JobStatus.RUNNING
        elif counts.get(ItemStatus.FAILED.value):
            status = JobStatus.COMPLETED_WITH_ERRORS
        elif counts.get(ItemStatus.REVIEW_REQUIRED.value):
            status = JobStatus.REVIEW_REQUIRED
        else:
            status = JobStatus.COMPLETED
        self.connection.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (status.value, now, job_id),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> OperationsJob:
        return OperationsJob(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            validation_policy=row["validation_policy"],
            status=JobStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> JobItem:
        return JobItem(
            item_id=row["item_id"],
            job_id=row["job_id"],
            ticker=row["ticker"],
            ordinal=row["ordinal"],
            status=ItemStatus(row["status"]),
            attempt_count=row["attempt_count"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            error=row["error"],
            manifest_path=row["manifest_path"],
        )
