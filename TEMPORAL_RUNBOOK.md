# Temporal Operations Runbook

## Scope

Milestone 7.3 provides a Python Temporal worker and an idempotent batch
submitter. The implementation runs against a local Temporal development server
or Temporal Cloud without changing workflow code.

Temporal coordinates execution. PostgreSQL is the operational source of record,
and local CAS or S3 is the artifact byte store. Workflow history contains
versioned ticker, filing, identity, and result metadata, but never HTML or PDF
bytes.

This is an adoption-ready development slice, not evidence of a production EKS
deployment. The production gates are listed below.

## Local prerequisites

- Python 3.10 or newer
- Docker
- Temporal CLI

Install the service dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-temporal.txt
```

Start and migrate PostgreSQL:

```bash
docker compose -f docker-compose.postgres.yml up -d --wait
alembic upgrade head
```

Start Temporal in a separate terminal:

```bash
temporal server start-dev
```

## Run a worker

The local defaults match `docker-compose.postgres.yml` and the Temporal
development server:

```bash
python -m prospectus_fetcher.temporal_worker
```

The worker validates the Alembic revision before polling. It creates four
synchronous Activity threads by default, gives each package item an isolated
workspace, and publishes immutable artifacts under `var/artifacts`.

Useful local overrides:

```bash
export PROSPECTUS_DATABASE_URL='postgresql+psycopg://prospectus:prospectus@localhost:55432/prospectus_test'
export PROSPECTUS_ACTIVITY_WORKERS=4
export PROSPECTUS_LEASE_SECONDS=1800
export PROSPECTUS_WORK_ROOT='var/temporal-work'
export PROSPECTUS_LOCAL_ARTIFACT_ROOT='var/artifacts'
export TEMPORAL_ADDRESS='localhost:7233'
export TEMPORAL_NAMESPACE='default'
export TEMPORAL_TASK_QUEUE='fund-prospectus-v1'
```

## Submit work

Return after durable submission:

```bash
python -m prospectus_fetcher.temporal_submit \
  VUSXX QQQ SPY \
  --idempotency-key manual-2026-07-20
```

Wait and print the final stored result:

```bash
python -m prospectus_fetcher.temporal_submit \
  VUSXX QQQ SPY \
  --idempotency-key manual-2026-07-20 \
  --wait
```

The same scope, key, normalized tickers, and validation policy resolve to the
same workflow and PostgreSQL job. Reusing the same scope/key for a different
request fails as an idempotency conflict.

The service submitter defaults to the measured `v7` validation policy. The
standalone CLI deliberately retains `legacy` as its rollback-safe default.

## Recovery check

1. Submit a multi-ticker batch.
2. Stop the worker with `Ctrl-C` while the batch is active.
3. Start the same worker command again.
4. Inspect the workflow in the Temporal UI and query the PostgreSQL job.

Temporal retains completed workflow stages. Exact item claims use a stable
workflow owner, terminal items are returned without reprocessing, and immutable
artifact publication is idempotent. An Activity interrupted before completion
may execute again, so Activity side effects must remain safe under at-least-once
execution.

## Retry ownership

The Temporal worker creates `SECClient(max_retries=0)`. This prevents
`urllib3` retries from occurring invisibly inside a Temporal Activity attempt.

| Failure | Temporal behavior |
|---|---|
| SEC `429` | Retry; honor `Retry-After` up to 15 minutes |
| SEC `5xx` | Retry with bounded exponential backoff |
| Timeout or connection error | Retry |
| Temporary PostgreSQL connection failure | Retry |
| Temporary S3/Boto failure | Retry |
| Invalid SEC schema or identifier | Do not retry |
| Idempotency or persistence contract violation | Do not retry |
| Artifact integrity mismatch | Do not retry |
| Unknown programming error | Do not retry; persist as `pipeline_bug` |

The parent starts bounded ticker child workflows. Filing selection is one
completed Activity result; package construction and persistence are a later
Activity. Therefore a transient package-stage failure does not repeat filing
selection. Candidate-level work inside a failed package Activity is not yet
checkpointed and may repeat.

## Temporal Cloud

API-key authentication:

```bash
export TEMPORAL_ADDRESS='<account>.<region>.tmprl.cloud:7233'
export TEMPORAL_NAMESPACE='<namespace>.<account>'
export TEMPORAL_API_KEY='<secret>'
export TEMPORAL_TLS=true
```

mTLS is supported as an alternative:

```bash
export TEMPORAL_CLIENT_CERT='/run/secrets/temporal/client.pem'
export TEMPORAL_CLIENT_KEY='/run/secrets/temporal/client.key'
export TEMPORAL_SERVER_NAME='<expected-server-name>'
export TEMPORAL_TLS=true
```

Do not configure an API key and mTLS client files together.

## S3 artifact mode

SSE-S3:

```bash
export PROSPECTUS_ARTIFACT_BACKEND=s3
export PROSPECTUS_S3_BUCKET='<bucket>'
export PROSPECTUS_S3_PREFIX='prospectus-artifacts'
export PROSPECTUS_S3_ENCRYPTION='AES256'
```

SSE-KMS:

```bash
export PROSPECTUS_ARTIFACT_BACKEND=s3
export PROSPECTUS_S3_BUCKET='<bucket>'
export PROSPECTUS_S3_ENCRYPTION='aws:kms'
export PROSPECTUS_S3_KMS_KEY_ID='<kms-key-arn>'
```

The worker refuses S3 mode without explicit encryption configuration.

## Verification

```bash
pytest -q

docker compose -f docker-compose.postgres.yml up -d --wait
RUN_POSTGRES_TESTS=1 pytest tests/postgres_contract.py -q

RUN_TEMPORAL_TESTS=1 pytest tests/temporal_contract.py -q
```

The Temporal contract downloads the official ephemeral test server on its first
run. `TEMPORAL_TEST_SERVER_PATH` can point to a preinstalled test-server binary
in offline CI.

## Production gates

Before unattended or horizontally scaled deployment:

1. Replace the process-local SEC limiter with service-wide request control.
2. Add structured logs, metrics, traces, alerting, and retry-attempt history.
3. Validate Temporal Cloud namespace, EKS worker identity, PostgreSQL failover,
   and pool sizing in the target environment.
4. Validate S3 IAM, KMS, versioning, lifecycle, and conditional writes in the
   target AWS account.
5. Define workspace cleanup, artifact retention, orphan reconciliation, and
   backup/restore procedures.
6. Add the authenticated internal API and additive reviewer decision model.
