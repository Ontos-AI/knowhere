# ECS task definitions

These files are deployment templates for the shared `knowhere-fargate` cluster. They do not contain secret values and are not registered automatically.

The templates intentionally omit `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`. With `S3_TYPE=s3`, boto3 obtains temporary authenticated credentials from the ECS task role. The same templates render staging or production values; the release workflow selects production only for a published release whose tag points to `main`.

The environment-specific Secrets Manager secret supplied to the renderer must be a JSON secret with these keys:

- API: `DATABASE_URL`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `CELERY_REDIS_URL`, `SECRET_KEY`, `DS_KEY`, `ALI_API_KEYS`, `ARK_API_KEY`, `GPT_API_KEY`, `MINERU_API_KEYS`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `WEBHOOK_MASTER_KEY`, `LOGFIRE_TOKEN`, `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY`
- Worker: the API keys above plus `CELERY_REDIS_PASSWORD` and `ILOVEAPI_KEYS`

Render only after the secret and log groups exist, substituting the exact immutable ECR image digests and IAM role/secret ARNs:

```bash
API_IMAGE=107424103509.dkr.ecr.us-east-1.amazonaws.com/knowhere/knowhere-backend@sha256:... \
WORKER_IMAGE=107424103509.dkr.ecr.us-east-1.amazonaws.com/knowhere/knowhere-worker@sha256:... \
EXECUTION_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-fargate-staging-execution-role \
API_TASK_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-api-staging-task-role \
WORKER_TASK_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-worker-staging-task-role \
SECRETS_ARN=arn:aws:secretsmanager:us-east-1:107424103509:secret:knowhere/staging/runtime-... \
DEPLOYMENT_ENVIRONMENT=staging \
RUNTIME_ENVIRONMENT=staging \
APP_ENV=staging \
DB_SSL_MODE=require \
API_DB_POOL_SIZE=5 \
API_DB_MAX_OVERFLOW=5 \
WORKER_DB_SYNC_POOL_SIZE=2 \
WORKER_DB_SYNC_MAX_OVERFLOW=2 \
S3_BUCKET_NAME=knowhere-storage-staging \
INTERNAL_DASHBOARD_ENDPOINT=https://staging.knowhereto.ai \
FRONTEND_URL=https://staging.knowhereto.ai \
API_WEBHOOK_ENDPOINT=https://api-staging.knowhereto.ai/v1/internal/s3-events \
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:107424103509:knowhere-staging-s3-events \
QSTASH_CALLBACK_BASE_URL=https://api-staging.knowhereto.ai/api/v1 \
API_CPU=256 \
API_MEMORY=1024 \
WORKER_CPU=2048 \
WORKER_MEMORY=4096 \
python deploy/ecs/render_task_definitions.py --environment staging --output-dir /tmp/knowhere-ecs-rendered
```

The output directory is deployment-only and must not be committed. The renderer fails on missing inputs, unresolved placeholders, or either long-lived S3 credential variable.

The staging workflow keeps the API at 256 CPU / 1024 MiB. The production release
workflow sets the API to 512 CPU / 2048 MiB (2 GiB) and the worker to 2048 CPU /
4096 MiB, so production API memory remains at 2 GiB across future releases.

## Staging workflow prerequisites

The staging workflow in `.github/workflows/build-images.yml` expects these GitHub Actions secrets:

- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`: the deployment credential used by the existing release workflow.
- `AWS_ECS_STAGING_SECRETS_ARN`: the ARN of the approved JSON runtime secret described above.
- `STAGING_MIGRATION_DATABASE_URL`: the direct Neon URL for the staging migration role. It is used only by the migration container and is never passed to the ECS runtime tasks.

Before an ECS staging deployment, an operator must create and verify the ECS services, network configuration, API load-balancer target, CloudWatch log groups, and runtime secret. The workflow validates those resources and fails without registering or updating a service when any prerequisite is missing. It does not create or delete AWS resources.

## Production workflow prerequisites

The production release path uses the same ECS cluster and renders the approved
2 vCPU / 4 GiB worker task size with `WORKER_CONCURRENCY=10`. It requires these
additional GitHub Actions secrets:

- `AWS_ECS_PROD_EXECUTION_ROLE_ARN`
- `AWS_ECS_PROD_API_TASK_ROLE_ARN`
- `AWS_ECS_PROD_WORKER_TASK_ROLE_ARN`
- `AWS_ECS_PROD_SECRETS_ARN`
- `PRODUCTION_MIGRATION_DATABASE_URL` (direct migration-only database URL)

Before publishing the production release, an operator must create and verify
the production runtime secret, IAM roles, API and worker ECS services, API
target group/listener rule, production ACM certificate attachment, and
`/ecs/knowhere-api-prod` and `/ecs/knowhere-worker-prod` log groups. The release
workflow validates these resources, registers immutable image-digest task
definitions, runs the production migration first, and then updates the ECS
services. It does not create or delete AWS resources.

## Required post-deploy backfill

Follow the complete
[`retrieval-serving-index-rollout-runbook.md`](../../docs/design/retrieval-serving-index-rollout-runbook.md)
for schema verification, statistics maintenance, readiness gates, parity,
monitoring, pause/resume, and rollback.

The additive migration does not populate the four per-channel statistics for
existing revisions. Until those revisions are ready, retrieval remains
quality-preserving but uses the legacy scoring path. Every release containing
this change must include the following DevOps action in its release
notification.

Run the commands as a one-off container using the newly deployed API image and
the production database secret. Do not run them inside the long-lived API task.

```bash
# Read-only statistics inventory, after migration
python /app/scripts/backfill_map_unit_statistics.py --check --batch-size 100

# Optional canary: apply one affected document first
python /app/scripts/backfill_map_unit_statistics.py \
  --document-id <document-id> \
  --batch-size 100 \
  --apply

# Apply statistics to complete format-v2 indexes
python /app/scripts/backfill_map_unit_statistics.py --apply --batch-size 100

# Final statistics and full serving-readiness checks
python /app/scripts/backfill_map_unit_statistics.py --check --batch-size 100
python /app/scripts/backfill_map_unit_indexes.py --check
```

The statistics script commits each document revision independently and is safe
to rerun. Missing or legacy indexes reported as `skipped` require the existing
full `backfill_map_unit_indexes.py --apply --document-id <document-id>` path.
Run only one maintenance process at a time. Verify the canary retrieval before
starting the full apply. New or republished documents build their index
automatically during publication.

## Manual staging availability

`.github/workflows/manage-staging.yml` exposes three manually dispatched
operations. It does not run on a branch, release, cron, or other automatic
trigger, and it does not create or delete AWS resources.

- `status` verifies AWS account `107424103509`, reports API and worker
  desired/running/pending counts and task-definition revisions, and reads the
  queued and processing backlog groups from the permanent `jobs` ledger.
- `start` requires an owner-approval issue/comment reference, starts the worker
  service at desired count `2`, waits for two healthy workers, then starts the
  API at desired count `1`, verifies the public health endpoint, and reports
  the elapsed cold-start readiness time.
- `stop` requires an owner-approval issue/comment reference, stops the API,
  waits the accepted 30-minute worker drain window, then stops the workers and
  verifies both services at desired/running `0/0`.

The workflow configures and uses only the named AWS profile `knowhere`; the
command verifies the account before any ECS call. It retrieves the runtime
database URL from the existing staging Secrets Manager secret only to query the
job ledger. Secret values and the database URL are never written to workflow
output or the GitHub step summary. The ledger is the durable business backlog;
it is intentionally reported instead of raw Redis queue length because the
hosted GitHub runner has no route to the private ElastiCache endpoint.

For `start` or `stop`, paste the explicit owner-approval URL from an issue, pull
request, or comment in an `Ontos-AI` GitHub repository into
`approval_reference`. Free-form notes and URLs outside the organization are
rejected. `status` is read-only and does not require a reference.
Do not begin an upload, billing, webhook, deployment-validation, or long-running
worker test close to the scheduled cutoff. Once EventBridge schedules exist,
an extended staging session must disable both stop schedules with an owner and
expiry before starting such a test, then restore them afterward.

The repository workflow is only the manual operator interface. The four
weekday start/stop schedules and their least-privilege execution role remain
AWS EventBridge Scheduler resources governed by infrastructure issue #30.

## Local Docker smoke test

LocalStack Community does not implement the ECS API used by this deployment, so it cannot validate Fargate orchestration. The runtime can still be checked locally with Docker:

```bash
./deploy/ecs/docker-runtime-smoke.sh
```

The script creates an isolated Docker network, starts disposable PostgreSQL and Redis containers, builds the API and worker images, runs Alembic, and verifies the API and worker container health checks. It uses filesystem object storage and mock LLM responses, calls no AWS API, and removes its containers and network on exit. Override `POSTGRES_IMAGE` or `REDIS_IMAGE` when the official images are not available locally. When `http_proxy` or `https_proxy` is set, the script passes those values only to the Docker build process.
