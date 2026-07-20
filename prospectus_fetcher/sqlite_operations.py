"""SQLite reference adapter for persistent jobs, artifacts, and review tasks."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .artifact_store import (
    ArtifactStore,
    LocalContentAddressedArtifactStore,
    PreparedArtifact,
    prepare_manifest_artifacts,
)
from .models import FetchResult
from .operations import (
    completion_record,
    IdempotencyConflict,
    ItemStatus,
    JobItem,
    JobStatus,
    LeaseOwnershipError,
    OperationsContractError,
    OperationsJob,
    ReviewStatus,
    ReviewTask,
    normalize_idempotency_scope,
    normalize_tickers,
    request_fingerprint,
)
from .package import VALIDATION_POLICIES


SCHEMA_VERSION = 2

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

_MIGRATION_2 = """
CREATE TABLE jobs_v2 (
    job_id TEXT PRIMARY KEY,
    idempotency_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    validation_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(idempotency_scope, idempotency_key)
);

INSERT INTO jobs_v2(
    job_id, idempotency_scope, idempotency_key, request_fingerprint,
    validation_policy, status, created_at, updated_at
)
SELECT
    job_id, 'internal', idempotency_key, request_fingerprint,
    validation_policy, status, created_at, updated_at
FROM jobs;

CREATE TABLE artifacts_v2 (
    artifact_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES job_items(item_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    accession TEXT NOT NULL,
    source_url TEXT,
    storage_provider TEXT NOT NULL,
    storage_namespace TEXT,
    object_key TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content_type TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(item_id, role, accession, sha256)
);

INSERT INTO artifacts_v2(
    artifact_id, item_id, role, accession, source_url, storage_provider,
    storage_namespace, object_key, size_bytes, sha256, content_type, created_at
)
SELECT
    artifact_id, item_id, role, accession, source_url, 'legacy-local',
    'legacy', local_path, size_bytes, sha256, NULL, created_at
FROM artifacts;

DROP TABLE artifacts;
DROP TABLE jobs;
ALTER TABLE jobs_v2 RENAME TO jobs;
ALTER TABLE artifacts_v2 RENAME TO artifacts;
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class SQLiteOperationsStore:
    """Durable local implementation of the Milestone 7 operations contract."""

    def __init__(
        self,
        path: str,
        clock: Callable[[], datetime] = _utc_now,
        artifact_store: Optional[ArtifactStore] = None,
    ) -> None:
        self.path = path
        self.clock = clock
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if artifact_store is None:
            if path == ":memory:":
                raise OperationsContractError(
                    "an artifact_store is required for an in-memory database"
                )
            artifact_store = LocalContentAddressedArtifactStore(
                Path(f"{path}.artifacts")
            )
        self.artifact_store = artifact_store
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

        newest = (
            self.connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if migrations_exist is not None
            else None
        )
        if newest is None:
            with self.connection:
                self.connection.executescript(_MIGRATION_1)
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (1, _timestamp(self.clock())),
                )
            newest = 1
        if newest < 2:
            self._apply_migration_2()

    def _apply_migration_2(self) -> None:
        self.connection.executescript(
            "PRAGMA foreign_keys = OFF;\nBEGIN IMMEDIATE;\n" + _MIGRATION_2
        )
        try:
            violations = self.connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise OperationsContractError(
                    "SQLite migration 2 produced foreign-key violations"
                )
            self.connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (2, _timestamp(self.clock())),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def create_job(
        self,
        idempotency_key: str,
        tickers: Sequence[str],
        validation_policy: str,
        idempotency_scope: str = "internal",
    ) -> OperationsJob:
        key = idempotency_key.strip()
        if not key:
            raise OperationsContractError("idempotency_key must not be empty")
        scope = normalize_idempotency_scope(idempotency_scope)
        if validation_policy not in VALIDATION_POLICIES:
            raise OperationsContractError(
                f"validation_policy must be one of {', '.join(VALIDATION_POLICIES)}"
            )
        normalized = normalize_tickers(tickers)
        fingerprint = request_fingerprint(normalized, validation_policy)
        now = _timestamp(self.clock())

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE idempotency_scope = ? AND idempotency_key = ?
                """,
                (scope, key),
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
                    job_id, idempotency_scope, idempotency_key,
                    request_fingerprint,
                    validation_policy, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    scope,
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
        preview = self._owned_running_item(item_id, worker_id)
        job_policy = self.connection.execute(
            "SELECT validation_policy FROM jobs WHERE job_id = ?",
            (preview["job_id"],),
        ).fetchone()["validation_policy"]
        record = completion_record(
            preview["ticker"],
            job_policy,
            result,
            manifest,
        )
        prepared_artifacts = (
            prepare_manifest_artifacts(manifest, self.artifact_store)
            if manifest is not None
            else []
        )
        now = _timestamp(self.clock())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._owned_running_item(item_id, worker_id)
            current_job_policy = self.connection.execute(
                "SELECT validation_policy FROM jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()["validation_policy"]
            if current_job_policy != job_policy:
                raise OperationsContractError(
                    "job validation policy changed during artifact preparation"
                )
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
                    record.status.value,
                    record.error,
                    json.dumps(record.result_payload, sort_keys=True),
                    (
                        json.dumps(record.manifest_payload, sort_keys=True)
                        if record.manifest_payload is not None
                        else None
                    ),
                    result.manifest_path,
                    record.identity.get("registrant_cik"),
                    record.identity.get("series_id"),
                    record.identity.get("class_id"),
                    record.identity.get("level"),
                    record.package.get("verification"),
                    record.package.get("validation_policy_version"),
                    now,
                    item_id,
                ),
            )

            self._persist_artifacts(item_id, prepared_artifacts, now)
            if record.status is ItemStatus.REVIEW_REQUIRED:
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

    def _persist_artifacts(
        self,
        item_id: str,
        artifacts: Sequence[PreparedArtifact],
        now: str,
    ) -> None:
        namespace = uuid.UUID(item_id)
        for artifact in artifacts:
            identity = "|".join(
                (
                    artifact.role,
                    artifact.accession,
                    artifact.stored.sha256,
                )
            )
            artifact_id = str(uuid.uuid5(namespace, identity))
            self.connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    artifact_id, item_id, role, accession, source_url,
                    storage_provider, storage_namespace, object_key,
                    size_bytes, sha256, content_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    item_id,
                    artifact.role,
                    artifact.accession,
                    artifact.source_url,
                    artifact.stored.storage_provider,
                    artifact.stored.storage_namespace,
                    artifact.stored.object_key,
                    artifact.stored.size_bytes,
                    artifact.stored.sha256,
                    artifact.stored.content_type,
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
            idempotency_scope=row["idempotency_scope"],
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
