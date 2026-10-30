# Isolated progressive integration stack

This test stack runs the current API and exactly two ordinary Workers. Each
Worker retains two CPUs, 4 GiB and concurrency 10. It does not activate the
publication-storage PoC or change production infrastructure.

Copy `.env.example` and `runtime.env.example` to private files outside the
build context. Generate fresh PostgreSQL passwords, webhook encryption and
local queue credentials. Keep production databases, buckets and queues out of
this environment. Docker ports bind to loopback by default.

Use the same Compose arguments for every command:

```sh
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml build api worker-a
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml up -d postgres redis storage
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml run --rm migrate
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml up -d api worker-a worker-b
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml run --rm --no-deps storage-notifications
docker compose --env-file /private/compose.env -f deploy/progressive-e2e/compose.yaml run --rm --no-deps bootstrap-auth
```

Migrations and standalone identity initialization use the privileged test role.
The running API and Workers use a separate non-superuser role without
`BYPASSRLS`. The bootstrap helper writes a private, exclusive `auth.json`; reuse
that identity rather than overwriting the file.

Storage initialization creates only the two named disposable buckets and the
owned SNS topic. After API health, `storage-notifications` installs and verifies
one confirmed HTTP subscription. The ordinary URL-ingestion path requires this
real S3 ObjectCreated notification; an explicit confirm-upload request does not
prove that path. Repeating subscription setup must reuse the existing
subscription. The uploads prefix is `uploads/`; result writes do not notify.

The Windows run also loads `compose.windows.yaml`, which builds PostgreSQL 15
with pgvector from its official packages and uses the available LocalStack
image. Its Docker client configuration, private environment files, source
bundles and evidence remain under the dedicated remote task directory. This
does not require changing machine-wide Docker credentials or registry settings.

The observed LocalStack recreation did not retain the earlier bucket state,
despite the mounted volume and `PERSISTENCE=1`. Treat this emulator as disposable:
do not recreate it during a measured run, or combine earlier snapshot IDs with
a later storage instance. Retain evidence outside containers. This setup does
not validate durable production S3 or hosted SNS delivery.

`run-http-acceptance.py` drives real APIs and reads the resulting PostgreSQL,
Redis and S3 state. It requires fresh host-captured health evidence for both
Workers; it does not enable Celery remote control. Its generated three-page
fixture checks page assets, immutable snapshots, namespace fences, coarse
navigation and read-triggered Fine. Results are integration evidence, not a
large-document timing or production capacity result.

The matching Hipo harness is in its repository at
`test/e2e/progressive-remote/`. It packages the current SDK source locally and
uses the same Docker network, local signed QStash and test Knowhere identity.
Build artifacts containing its guarded testing routes are for this isolated
environment.
