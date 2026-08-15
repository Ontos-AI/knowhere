# ECS task definitions

These files are deployment templates for the shared `knowhere-fargate` cluster. They do not contain secret values and are not registered automatically.

The staging templates intentionally omit `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`. With `S3_TYPE=s3`, boto3 obtains temporary authenticated credentials from the ECS task role.

The single staging Secrets Manager secret supplied to the renderer must be a JSON secret with these keys:

- API: `DATABASE_URL`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `CELERY_REDIS_URL`, `SECRET_KEY`, `DS_KEY`, `ALI_API_KEYS`, `ARK_API_KEY`, `GPT_API_KEY`, `MINERU_API_KEYS`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `WEBHOOK_MASTER_KEY`, `LOGFIRE_TOKEN`, `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY`
- Worker: the API keys above plus `CELERY_REDIS_PASSWORD` and `ILOVEAPI_KEYS`

Render only after the secret and log groups exist, substituting the exact immutable ECR image digests and IAM role/secret ARNs:

```bash
API_IMAGE=107424103509.dkr.ecr.us-east-1.amazonaws.com/knowhere/knowhere-backend@sha256:... \
WORKER_IMAGE=107424103509.dkr.ecr.us-east-1.amazonaws.com/knowhere/knowhere-worker@sha256:... \
EXECUTION_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-fargate-staging-execution-role \
API_TASK_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-api-staging-task-role \
WORKER_TASK_ROLE_ARN=arn:aws:iam::107424103509:role/knowhere-worker-staging-task-role \
STAGING_SECRETS_ARN=arn:aws:secretsmanager:us-east-1:107424103509:secret:knowhere/staging/runtime-... \
python deploy/ecs/render_task_definitions.py --output-dir /tmp/knowhere-ecs-rendered
```

The output directory is deployment-only and must not be committed. The renderer fails on missing inputs, unresolved placeholders, or either long-lived S3 credential variable.

## Staging workflow prerequisites

The staging workflow in `.github/workflows/build-images.yml` expects these GitHub Actions secrets:

- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`: the deployment credential used by the existing release workflow.
- `AWS_ECS_STAGING_SECRETS_ARN`: the ARN of the approved JSON runtime secret described above.
- `STAGING_MIGRATION_DATABASE_URL`: the direct Neon URL for the staging migration role. It is used only by the migration container and is never passed to the ECS runtime tasks.

Before an ECS staging deployment, an operator must create and verify the ECS services, network configuration, API load-balancer target, CloudWatch log groups, and runtime secret. The workflow validates those resources and fails without registering or updating a service when any prerequisite is missing. It does not create or delete AWS resources.

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
