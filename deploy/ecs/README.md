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

## Local Docker smoke test

LocalStack Community does not implement the ECS API used by this deployment, so it cannot validate Fargate orchestration. The runtime can still be checked locally with Docker:

```bash
./deploy/ecs/docker-runtime-smoke.sh
```

The script creates an isolated Docker network, starts disposable PostgreSQL and Redis containers, builds the API and worker images, runs Alembic, and verifies the API and worker container health checks. It uses filesystem object storage and mock LLM responses, calls no AWS API, and removes its containers and network on exit. Override `POSTGRES_IMAGE` or `REDIS_IMAGE` when the official images are not available locally. When `http_proxy` or `https_proxy` is set, the script passes those values only to the Docker build process.
