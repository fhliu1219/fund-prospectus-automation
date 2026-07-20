"""Create the PostgreSQL operations contract.

Revision ID: 0001_operations
Revises:
Create Date: 2026-07-20
"""

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_operations"
down_revision: Optional[str] = None
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("validation_policy", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN "
            "('submitted', 'running', 'completed', 'review_required', "
            "'completed_with_errors')",
            name="ck_jobs_status",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_jobs"),
        sa.UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_jobs_idempotency",
        ),
    )
    op.create_table(
        "job_items",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("source_manifest_path", sa.Text()),
        sa.Column("registrant_cik", sa.BigInteger()),
        sa.Column("series_id", sa.String(32)),
        sa.Column("class_id", sa.String(32)),
        sa.Column("identity_level", sa.String(32)),
        sa.Column("document_verification", sa.String(64)),
        sa.Column("policy_version", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_job_items_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN "
            "('pending', 'running', 'verified', 'review_required', 'failed')",
            name="ck_job_items_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.job_id"],
            name="fk_job_items_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", name="pk_job_items"),
        sa.UniqueConstraint(
            "job_id",
            "ordinal",
            name="uq_job_items_ordinal",
        ),
        sa.UniqueConstraint(
            "job_id",
            "ticker",
            name="uq_job_items_ticker",
        ),
    )
    op.create_index(
        "ix_job_items_claim",
        "job_items",
        ["job_id", "status", "lease_expires_at", "ordinal"],
    )
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("accession", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("storage_provider", sa.String(64), nullable=False),
        sa.Column("storage_namespace", sa.String(255)),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_artifacts_size_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["job_items.item_id"],
            name="fk_artifacts_item_id_job_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_artifacts"),
        sa.UniqueConstraint(
            "item_id",
            "role",
            "accession",
            "sha256",
            name="uq_artifacts_item_role_accession_sha",
        ),
    )
    op.create_table(
        "review_tasks",
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_review_tasks_status",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["job_items.item_id"],
            name="fk_review_tasks_item_id_job_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name="pk_review_tasks"),
        sa.UniqueConstraint("item_id", name="uq_review_tasks_item_id"),
    )
    op.create_index(
        "ix_review_tasks_status_created",
        "review_tasks",
        ["status", "created_at"],
    )
    op.create_table(
        "review_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("rationale", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["review_tasks.review_id"],
            name="fk_review_events_review_id_review_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_review_events"),
    )


def downgrade() -> None:
    op.drop_table("review_events")
    op.drop_index("ix_review_tasks_status_created", table_name="review_tasks")
    op.drop_table("review_tasks")
    op.drop_table("artifacts")
    op.drop_index("ix_job_items_claim", table_name="job_items")
    op.drop_table("job_items")
    op.drop_table("jobs")
