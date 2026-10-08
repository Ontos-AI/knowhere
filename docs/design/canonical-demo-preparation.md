# Prepare canonical demo bundles

`prepare_demo_bundles.py` uploads the immutable ZIP and raw artifacts for
catalog demo sources ahead of user materialization. It uses the same content
version, Redis lease and completion marker as the API path. It does not create
jobs, documents, credits or publication state.

Run from the repository root with the API's normal storage and Redis environment
configured (`S3_TYPE`, result bucket, endpoint and credentials, `REDIS_HOST` and
`REDIS_PORT`). The source checkout must include `apps/api/app/data/demo_documents`.

```sh
uv run --package knowhere-api-app python apps/api/scripts/prepare_demo_bundles.py demo-spacex-s1
uv run --package knowhere-api-app python apps/api/scripts/prepare_demo_bundles.py demo-spacex-s1 --upload
uv run --package knowhere-api-app python apps/api/scripts/prepare_demo_bundles.py --all --upload
```

The default invocation lists selected sources and makes no storage or Redis
calls. `--upload` is required for writes. A completed version is reported as
`reused` and uploads no objects. New versions are reported as `created` with
their exact ZIP key and raw prefix. A failure exits nonzero; rerunning is safe
because the completion marker is written only after all artifacts finish.

Use the same configured result bucket as the API. The
`DEMO_CANONICAL_BUNDLE_ENABLED` API setting controls new materializations, not
this explicit preparation command. Keep the prefix-aware readers deployed for
existing canonical results.
