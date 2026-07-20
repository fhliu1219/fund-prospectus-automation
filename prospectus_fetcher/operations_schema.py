"""SQLAlchemy Core schema for the PostgreSQL operations repository."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


jobs = Table(
    "jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column("idempotency_scope", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("request_fingerprint", String(64), nullable=False),
    Column("validation_policy", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "idempotency_scope",
        "idempotency_key",
        name="uq_jobs_idempotency",
    ),
    CheckConstraint(
        "status IN "
        "('submitted', 'running', 'completed', 'review_required', "
        "'completed_with_errors')",
        name="status",
    ),
)


job_items = Table(
    "job_items",
    metadata,
    Column("item_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "job_id",
        UUID(as_uuid=True),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ticker", String(32), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("error", Text),
    Column("result_json", JSONB),
    Column("manifest_json", JSONB),
    Column("source_manifest_path", Text),
    Column("registrant_cik", BigInteger),
    Column("series_id", String(32)),
    Column("class_id", String(32)),
    Column("identity_level", String(32)),
    Column("document_verification", String(64)),
    Column("policy_version", String(128)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint("job_id", "ticker", name="uq_job_items_ticker"),
    UniqueConstraint("job_id", "ordinal", name="uq_job_items_ordinal"),
    CheckConstraint(
        "status IN "
        "('pending', 'running', 'verified', 'review_required', 'failed')",
        name="status",
    ),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
)

Index(
    "ix_job_items_claim",
    job_items.c.job_id,
    job_items.c.status,
    job_items.c.lease_expires_at,
    job_items.c.ordinal,
)


artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "item_id",
        UUID(as_uuid=True),
        ForeignKey("job_items.item_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", String(64), nullable=False),
    Column("accession", String(32), nullable=False),
    Column("source_url", Text),
    Column("storage_provider", String(64), nullable=False),
    Column("storage_namespace", String(255)),
    Column("object_key", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("content_type", String(255)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "item_id",
        "role",
        "accession",
        "sha256",
        name="uq_artifacts_item_role_accession_sha",
    ),
    CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
)


review_tasks = Table(
    "review_tasks",
    metadata,
    Column("review_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "item_id",
        UUID(as_uuid=True),
        ForeignKey("job_items.item_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("status", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("resolved_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('pending', 'resolved')",
        name="status",
    ),
)

Index(
    "ix_review_tasks_status_created",
    review_tasks.c.status,
    review_tasks.c.created_at,
)


review_events = Table(
    "review_events",
    metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "review_id",
        UUID(as_uuid=True),
        ForeignKey("review_tasks.review_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("event_type", String(64), nullable=False),
    Column("actor", Text),
    Column("rationale", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
