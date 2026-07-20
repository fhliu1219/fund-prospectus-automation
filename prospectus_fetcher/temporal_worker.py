"""Run the Temporal worker that executes prospectus retrieval Activities."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping, Optional

from sqlalchemy import create_engine
from temporalio.worker import Worker

from . import config
from .artifact_store import LocalContentAddressedArtifactStore
from .cli import ProspectusFetcher
from .postgres_operations import PostgreSQLOperationsStore
from .s3_artifact_store import S3ArtifactStore
from .sec_client import SECClient, SECRequestThrottle
from .temporal_activities import ProspectusTemporalActivities
from .temporal_config import TemporalConnectionSettings, connect_temporal
from .temporal_workflows import (
    FundProspectusBatchWorkflow,
    FundProspectusTickerWorkflow,
)


logger = logging.getLogger(__name__)

_DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://prospectus:prospectus@"
    "localhost:55432/prospectus_test"
)


def _positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


@dataclass(frozen=True)
class TemporalWorkerSettings:
    database_url: str
    output_root: Path
    artifact_backend: str
    local_artifact_root: Path
    s3_bucket: Optional[str]
    s3_prefix: str
    s3_encryption: Optional[str]
    s3_kms_key_id: Optional[str]
    activity_workers: int
    lease_seconds: int

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "TemporalWorkerSettings":
        values = os.environ if environ is None else environ
        backend = values.get(
            "PROSPECTUS_ARTIFACT_BACKEND",
            "local",
        ).strip().lower()
        if backend not in {"local", "s3"}:
            raise ValueError(
                "PROSPECTUS_ARTIFACT_BACKEND must be 'local' or 's3'"
            )
        bucket = values.get("PROSPECTUS_S3_BUCKET", "").strip() or None
        encryption = (
            values.get("PROSPECTUS_S3_ENCRYPTION", "").strip() or None
        )
        kms_key_id = (
            values.get("PROSPECTUS_S3_KMS_KEY_ID", "").strip() or None
        )
        if backend == "s3" and bucket is None:
            raise ValueError(
                "PROSPECTUS_S3_BUCKET is required for the S3 artifact backend"
            )
        if backend == "s3" and encryption is None:
            raise ValueError(
                "PROSPECTUS_S3_ENCRYPTION is required for the S3 backend"
            )
        database_url = values.get(
            "PROSPECTUS_DATABASE_URL",
            _DEFAULT_DATABASE_URL,
        ).strip()
        if not database_url:
            raise ValueError("PROSPECTUS_DATABASE_URL must not be empty")
        return cls(
            database_url=database_url,
            output_root=Path(
                values.get(
                    "PROSPECTUS_WORK_ROOT",
                    "var/temporal-work",
                )
            ),
            artifact_backend=backend,
            local_artifact_root=Path(
                values.get(
                    "PROSPECTUS_LOCAL_ARTIFACT_ROOT",
                    "var/artifacts",
                )
            ),
            s3_bucket=bucket,
            s3_prefix=values.get(
                "PROSPECTUS_S3_PREFIX",
                "prospectus-artifacts",
            ).strip("/"),
            s3_encryption=encryption,
            s3_kms_key_id=kms_key_id,
            activity_workers=_positive_int(
                values.get("PROSPECTUS_ACTIVITY_WORKERS", "4"),
                "PROSPECTUS_ACTIVITY_WORKERS",
            ),
            lease_seconds=_positive_int(
                values.get("PROSPECTUS_LEASE_SECONDS", "1800"),
                "PROSPECTUS_LEASE_SECONDS",
            ),
        )


def build_artifact_store(settings: TemporalWorkerSettings):
    if settings.artifact_backend == "s3":
        assert settings.s3_bucket is not None
        return S3ArtifactStore(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            encryption=settings.s3_encryption,
            kms_key_id=settings.s3_kms_key_id,
        )
    return LocalContentAddressedArtifactStore(
        settings.local_artifact_root,
        namespace="temporal-worker",
    )


async def run_worker(
    connection: TemporalConnectionSettings,
    settings: TemporalWorkerSettings,
) -> None:
    client = await connect_temporal(connection)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    artifact_store = build_artifact_store(settings)
    settings.output_root.mkdir(parents=True, exist_ok=True)

    with PostgreSQLOperationsStore(
        engine=engine,
        artifact_store=artifact_store,
        verify_schema=True,
    ):
        pass

    throttle = SECRequestThrottle(config.MAX_REQUESTS_PER_SECOND)

    def store_factory():
        return PostgreSQLOperationsStore(
            engine=engine,
            artifact_store=artifact_store,
            verify_schema=False,
        )

    def fetcher_factory(
        validation_policy: str,
        workspace_id: Optional[str],
    ) -> ProspectusFetcher:
        workspace = workspace_id or "resolution-only"
        client_for_activity = SECClient(
            max_retries=0,
            throttle=throttle,
        )
        return ProspectusFetcher(
            output_dir=str(settings.output_root / workspace),
            validation_policy=validation_policy,
            sec_client=client_for_activity,
        )

    activities = ProspectusTemporalActivities(
        store_factory=store_factory,
        fetcher_factory=fetcher_factory,
        lease_seconds=settings.lease_seconds,
    )
    executor = ThreadPoolExecutor(
        max_workers=settings.activity_workers,
        thread_name_prefix="prospectus-activity",
    )
    try:
        worker = Worker(
            client,
            task_queue=connection.task_queue,
            workflows=[
                FundProspectusBatchWorkflow,
                FundProspectusTickerWorkflow,
            ],
            activities=[
                activities.prepare_job,
                activities.claim_item,
                activities.resolve_filing,
                activities.build_and_persist,
                activities.record_failure,
                activities.read_job_status,
            ],
            activity_executor=executor,
            max_concurrent_activities=settings.activity_workers,
            graceful_shutdown_timeout=timedelta(minutes=2),
        )
        logger.info(
            "Temporal worker started: namespace=%s task_queue=%s activities=%d",
            connection.namespace,
            connection.task_queue,
            settings.activity_workers,
        )
        await worker.run()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the prospectus Temporal worker",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        connection = TemporalConnectionSettings.from_env()
        settings = TemporalWorkerSettings.from_env()
        asyncio.run(run_worker(connection, settings))
    except KeyboardInterrupt:
        logger.info("Temporal worker stopped")
    except Exception:
        logger.exception("Temporal worker failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
