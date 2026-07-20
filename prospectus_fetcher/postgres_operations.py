"""PostgreSQL implementation of the durable operations repository contract."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Mapping, Optional, Sequence

from sqlalchemy import Engine, and_, create_engine, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError

from .artifact_store import (
    ArtifactStore,
    PreparedArtifact,
    prepare_manifest_artifacts,
)
from .models import FetchResult
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
    completion_record,
    normalize_idempotency_scope,
    normalize_tickers,
    request_fingerprint,
)
from .operations_schema import (
    artifacts,
    job_items,
    jobs,
    review_tasks,
)
from .package import VALIDATION_POLICIES


EXPECTED_ALEMBIC_REVISION = "0001_operations"


class PostgreSQLOperationsStore:
    """PostgreSQL repository with server-time leases and queue-safe claims."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        database_url: Optional[str] = None,
        engine: Optional[Engine] = None,
        verify_schema: bool = True,
    ) -> None:
        if (database_url is None) == (engine is None):
            raise ValueError("provide exactly one of database_url or engine")
        self.artifact_store = artifact_store
        self._owns_engine = engine is None
        self.engine = engine or create_engine(
            database_url,
            pool_pre_ping=True,
        )
        if verify_schema:
            self._verify_schema()

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def __enter__(self) -> "PostgreSQLOperationsStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

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
        job_id = uuid.uuid4()

        with self.engine.begin() as connection:
            inserted_id = connection.execute(
                postgresql_insert(jobs)
                .values(
                    job_id=job_id,
                    idempotency_scope=scope,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    validation_policy=validation_policy,
                    status=JobStatus.SUBMITTED.value,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        jobs.c.idempotency_scope,
                        jobs.c.idempotency_key,
                    ]
                )
                .returning(jobs.c.job_id)
            ).scalar_one_or_none()

            if inserted_id is None:
                existing = connection.execute(
                    select(jobs).where(
                        jobs.c.idempotency_scope == scope,
                        jobs.c.idempotency_key == key,
                    )
                ).mappings().one()
                if existing["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {key!r} already belongs to a "
                        "different normalized request in scope {scope!r}"
                    )
                return self._job(existing)

            namespace = inserted_id
            connection.execute(
                job_items.insert(),
                [
                    {
                        "item_id": uuid.uuid5(namespace, ticker),
                        "job_id": inserted_id,
                        "ticker": ticker,
                        "ordinal": ordinal,
                        "status": ItemStatus.PENDING.value,
                    }
                    for ordinal, ticker in enumerate(normalized)
                ],
            )
            row = connection.execute(
                select(jobs).where(jobs.c.job_id == inserted_id)
            ).mappings().one()
            return self._job(row)

    def get_job(self, job_id: str) -> OperationsJob:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(jobs).where(jobs.c.job_id == _uuid(job_id, "job"))
            ).mappings().first()
        if row is None:
            raise KeyError(f"unknown job {job_id}")
        return self._job(row)

    def list_items(self, job_id: str) -> List[JobItem]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(job_items)
                .where(job_items.c.job_id == _uuid(job_id, "job"))
                .order_by(job_items.c.ordinal)
            ).mappings().all()
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
        job_uuid = _uuid(job_id, "job")

        with self.engine.begin() as connection:
            job_exists = connection.scalar(
                select(jobs.c.job_id).where(jobs.c.job_id == job_uuid)
            )
            if job_exists is None:
                raise KeyError(f"unknown job {job_id}")

            now = self._server_now(connection)
            row = connection.execute(
                select(job_items)
                .where(
                    job_items.c.job_id == job_uuid,
                    or_(
                        job_items.c.status == ItemStatus.PENDING.value,
                        and_(
                            job_items.c.status == ItemStatus.RUNNING.value,
                            job_items.c.lease_expires_at.is_not(None),
                            job_items.c.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(job_items.c.ordinal)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).mappings().first()

            if row is None:
                self._refresh_job_status(connection, job_uuid, now)
                return None

            lease_until = now + timedelta(seconds=lease_seconds)
            claimed = connection.execute(
                job_items.update()
                .where(job_items.c.item_id == row["item_id"])
                .values(
                    status=ItemStatus.RUNNING.value,
                    attempt_count=job_items.c.attempt_count + 1,
                    lease_owner=owner,
                    lease_expires_at=lease_until,
                    updated_at=now,
                )
                .returning(job_items)
            ).mappings().one()
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id == job_uuid)
                .values(status=JobStatus.RUNNING.value, updated_at=now)
            )
            return self._item(claimed)

    def complete_item(
        self,
        item_id: str,
        worker_id: str,
        result: FetchResult,
        manifest: Optional[dict],
    ) -> JobItem:
        item_uuid = _uuid(item_id, "item")
        with self.engine.connect() as connection:
            preview = self._owned_running_item(
                connection,
                item_uuid,
                worker_id,
            )
            job_policy = connection.scalar(
                select(jobs.c.validation_policy).where(
                    jobs.c.job_id == preview["job_id"]
                )
            )
        assert job_policy is not None
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

        with self.engine.begin() as connection:
            row = self._owned_running_item(
                connection,
                item_uuid,
                worker_id,
                lock=True,
            )
            current_policy = connection.scalar(
                select(jobs.c.validation_policy).where(
                    jobs.c.job_id == row["job_id"]
                )
            )
            if current_policy != job_policy:
                raise OperationsContractError(
                    "job validation policy changed during artifact preparation"
                )
            now = self._server_now(connection)
            connection.execute(
                job_items.update()
                .where(job_items.c.item_id == item_uuid)
                .values(
                    status=record.status.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    error=record.error,
                    result_json=record.result_payload,
                    manifest_json=record.manifest_payload,
                    source_manifest_path=result.manifest_path,
                    registrant_cik=record.identity.get("registrant_cik"),
                    series_id=record.identity.get("series_id"),
                    class_id=record.identity.get("class_id"),
                    identity_level=record.identity.get("level"),
                    document_verification=record.package.get("verification"),
                    policy_version=record.package.get(
                        "validation_policy_version"
                    ),
                    updated_at=now,
                )
            )
            self._persist_artifacts(
                connection,
                item_uuid,
                prepared_artifacts,
                now,
            )
            if record.status is ItemStatus.REVIEW_REQUIRED:
                self._create_review_task(
                    connection,
                    item_uuid,
                    result,
                    now,
                )
            self._refresh_job_status(connection, row["job_id"], now)

        return self._get_item(item_uuid)

    def fail_item(
        self,
        item_id: str,
        worker_id: str,
        error: str,
    ) -> JobItem:
        message = error.strip()
        if not message:
            raise OperationsContractError("error must not be empty")
        item_uuid = _uuid(item_id, "item")
        with self.engine.begin() as connection:
            row = self._owned_running_item(
                connection,
                item_uuid,
                worker_id,
                lock=True,
            )
            now = self._server_now(connection)
            connection.execute(
                job_items.update()
                .where(job_items.c.item_id == item_uuid)
                .values(
                    status=ItemStatus.FAILED.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    error=message,
                    updated_at=now,
                )
            )
            self._refresh_job_status(connection, row["job_id"], now)
        return self._get_item(item_uuid)

    def list_review_tasks(
        self,
        status: ReviewStatus = ReviewStatus.PENDING,
    ) -> List[ReviewTask]:
        statement = (
            select(
                review_tasks,
                job_items.c.job_id,
                job_items.c.ticker,
            )
            .join(job_items, review_tasks.c.item_id == job_items.c.item_id)
            .where(review_tasks.c.status == status.value)
            .order_by(review_tasks.c.created_at, review_tasks.c.review_id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            ReviewTask(
                review_id=str(row["review_id"]),
                item_id=str(row["item_id"]),
                job_id=str(row["job_id"]),
                ticker=row["ticker"],
                status=ReviewStatus(row["status"]),
                reason=row["reason"],
                created_at=_timestamp(row["created_at"]),
                resolved_at=_optional_timestamp(row["resolved_at"]),
            )
            for row in rows
        ]

    def artifact_records(self, item_id: str) -> List[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(artifacts)
                .where(artifacts.c.item_id == _uuid(item_id, "item"))
                .order_by(artifacts.c.role, artifacts.c.artifact_id)
            ).mappings().all()
        return [
            {
                **dict(row),
                "artifact_id": str(row["artifact_id"]),
                "item_id": str(row["item_id"]),
                "created_at": _timestamp(row["created_at"]),
            }
            for row in rows
        ]

    def persisted_manifest(self, item_id: str) -> Optional[dict]:
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(job_items.c.manifest_json).where(
                    job_items.c.item_id == _uuid(item_id, "item")
                )
            )
            exists = connection.scalar(
                select(job_items.c.item_id).where(
                    job_items.c.item_id == _uuid(item_id, "item")
                )
            )
        if exists is None:
            raise KeyError(f"unknown item {item_id}")
        return value

    def _verify_schema(self) -> None:
        try:
            with self.engine.connect() as connection:
                revision = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise OperationsContractError(
                "PostgreSQL operations schema is unavailable; run "
                "'alembic upgrade head'"
            ) from exc
        if revision != EXPECTED_ALEMBIC_REVISION:
            raise OperationsContractError(
                f"PostgreSQL schema revision {revision!r} does not match "
                f"{EXPECTED_ALEMBIC_REVISION!r}"
            )

    def _get_item(self, item_id: uuid.UUID) -> JobItem:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(job_items).where(job_items.c.item_id == item_id)
            ).mappings().first()
        if row is None:
            raise KeyError(f"unknown item {item_id}")
        return self._item(row)

    @staticmethod
    def _owned_running_item(
        connection,
        item_id: uuid.UUID,
        worker_id: str,
        lock: bool = False,
    ) -> Mapping:
        statement = select(job_items).where(job_items.c.item_id == item_id)
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
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

    @staticmethod
    def _persist_artifacts(
        connection,
        item_id: uuid.UUID,
        prepared: Sequence[PreparedArtifact],
        now: datetime,
    ) -> None:
        namespace = item_id
        for artifact in prepared:
            identity = "|".join(
                (
                    artifact.role,
                    artifact.accession,
                    artifact.stored.sha256,
                )
            )
            connection.execute(
                postgresql_insert(artifacts)
                .values(
                    artifact_id=uuid.uuid5(namespace, identity),
                    item_id=item_id,
                    role=artifact.role,
                    accession=artifact.accession,
                    source_url=artifact.source_url,
                    storage_provider=artifact.stored.storage_provider,
                    storage_namespace=artifact.stored.storage_namespace,
                    object_key=artifact.stored.object_key,
                    size_bytes=artifact.stored.size_bytes,
                    sha256=artifact.stored.sha256,
                    content_type=artifact.stored.content_type,
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        artifacts.c.item_id,
                        artifacts.c.role,
                        artifacts.c.accession,
                        artifacts.c.sha256,
                    ]
                )
            )

    @staticmethod
    def _create_review_task(
        connection,
        item_id: uuid.UUID,
        result: FetchResult,
        now: datetime,
    ) -> None:
        reason = "; ".join(result.warnings) or (
            f"package verification is {result.document_verification.value}"
        )
        connection.execute(
            postgresql_insert(review_tasks)
            .values(
                review_id=uuid.uuid5(item_id, "review"),
                item_id=item_id,
                status=ReviewStatus.PENDING.value,
                reason=reason,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[review_tasks.c.item_id])
        )

    @staticmethod
    def _refresh_job_status(connection, job_id: uuid.UUID, now: datetime) -> None:
        rows = connection.execute(
            select(job_items.c.status, func.count())
            .where(job_items.c.job_id == job_id)
            .group_by(job_items.c.status)
        ).all()
        counts = {status: count for status, count in rows}
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
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job_id)
            .values(status=status.value, updated_at=now)
        )

    @staticmethod
    def _server_now(connection) -> datetime:
        value = connection.scalar(select(func.clock_timestamp()))
        assert isinstance(value, datetime)
        return value

    @staticmethod
    def _job(row: Mapping) -> OperationsJob:
        return OperationsJob(
            job_id=str(row["job_id"]),
            idempotency_scope=row["idempotency_scope"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            validation_policy=row["validation_policy"],
            status=JobStatus(row["status"]),
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
        )

    @staticmethod
    def _item(row: Mapping) -> JobItem:
        return JobItem(
            item_id=str(row["item_id"]),
            job_id=str(row["job_id"]),
            ticker=row["ticker"],
            ordinal=row["ordinal"],
            status=ItemStatus(row["status"]),
            attempt_count=row["attempt_count"],
            lease_owner=row["lease_owner"],
            lease_expires_at=_optional_timestamp(row["lease_expires_at"]),
            error=row["error"],
            manifest_path=row["source_manifest_path"],
        )


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError) as exc:
        raise OperationsContractError(
            f"invalid {label} identifier {value!r}"
        ) from exc


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise OperationsContractError("database timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_timestamp(value: Optional[datetime]) -> Optional[str]:
    return _timestamp(value) if value is not None else None
