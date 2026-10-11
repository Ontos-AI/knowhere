# External services

This guide lists the third-party and infrastructure services Knowhere talks
to, and the environment keys that configure them.

Sources:

- `apps/api/.env.example`
- `apps/worker/.env.example`
- [README.md](../README.md) Quick Start and Telemetry sections

Copy those example files to `.env` and fill in the values you need. Do not
commit real credentials, private endpoints, or production URLs.

Most parser and retrieval tuning values have code defaults. Start with the
required external services first, then override model names, provider URLs,
budgets, or concurrency limits only when your deployment needs different
behavior.

## How to read this guide

| Required? | Meaning |
| --- | --- |
| Required for local startup | Listed under "Required for local startup" in `.env.example`. The API and worker need these (or an equivalent local stack) to boot. |
| Required for a feature | Empty is valid until you enable that feature (PDF parse, PPTX convert, billing, webhooks, and so on). |
| Optional override | Commented in `.env.example`. Code already has a default; set the key only to change it. |

**Where keys live.** Unless a row says otherwise, the same key appears in both
`apps/api/.env.example` and `apps/worker/.env.example`. Telemetry keys are
documented on the API side.

**Data leaving the box.** Rows marked **yes** send document bytes, prompts,
images, or metadata to a vendor. Keep that in mind for self-hosted and
air-gapped deployments.

---

## PostgreSQL

| | |
| --- | --- |
| **Purpose** | Primary store for users, documents, chunks, graph, retrieval, and billing state. |
| **Required?** | Required for local startup. |
| **Data leaving the box?** | No, unless you point `DATABASE_URL` at a hosted database. Local-dev uses the Compose Postgres from `./deploy/local-dev/start-dev.sh`. |

| Key | Notes |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy runtime URL (`postgresql+asyncpg://...`). API and worker roles must be `NOSUPERUSER NOBYPASSRLS`. |
| `MIGRATION_DATABASE_URL` | Separate privileged migration connection; never used for API or worker request sessions. |
| `DB_SSL_MODE` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, or `verify-full`. |
| `DB_SSL_CERT` | Optional client certificate path. |
| `DB_SSL_KEY` | Optional client key path. |
| `DB_SSL_ROOT_CERT` | Optional CA path. |

---

## Redis and Celery

| | |
| --- | --- |
| **Purpose** | Cache, rate limiting, job coordination, and the Celery broker / result backend. |
| **Required?** | Required for local startup. |
| **Data leaving the box?** | No, unless you point Redis at a hosted instance. Local-dev uses Compose Redis. |

| Key | Notes |
| --- | --- |
| `REDIS_HOST` | Redis hostname. |
| `REDIS_PORT` | Redis port. |
| `REDIS_PASSWORD` | Leave empty for unauthenticated local Redis. |
| `REDIS_DATABASE` | Logical database index. |
| `CELERY_REDIS_URL` | Full Redis URL for Celery broker, result backend, and RedBeat. |
| `BROKER_POOL_LIMIT` | Celery broker connection-pool size. |
| `RATE_LIMIT_ENABLED` | API rate limiting; not a vendor key. |

---

## S3-compatible object storage

| | |
| --- | --- |
| **Purpose** | Upload originals, store parse artifacts, and (when configured) hold result bundles. Local-dev uses LocalStack. |
| **Required?** | Required for local startup. |
| **Data leaving the box?** | Document files are written to the configured buckets. LocalStack on the local-dev stack stays on the machine. A cloud S3/MinIO/OSS endpoint stores objects with that provider. |

| Key | Notes |
| --- | --- |
| `S3_TYPE` | Storage backend. `.env.example` sets `s3` for local-dev LocalStack. |
| `S3_BUCKET_NAME` | Upload / source bucket. |
| `S3_RESULTS_BUCKET` | Results bucket (also used by billing). |
| `S3_ACCESS_KEY_ID` | Access key. Pair with `S3_SECRET_ACCESS_KEY`. |
| `S3_SECRET_ACCESS_KEY` | Secret key. |
| `S3_ENDPOINT_URL` | Custom endpoint (LocalStack, MinIO, or other S3-compatible APIs). |
| `S3_PRIVATE_DOMAIN` | Private asset domain. |
| `S3_TEMP_PATH` | Local temp path used during storage operations. |
| `S3_REGION` | Region name; may be empty for MinIO. |
| `S3_USE_SSL` | TLS for the storage client. |
| `S3_ADDRESSING_STYLE` | `auto`, `path`, or `virtual`. |
| `S3_WEBHOOK_AUTH_TOKEN` | Shared secret for S3/MinIO event webhooks. |
| `OSS_ENDPOINT` | Aliyun OSS endpoint when `S3_TYPE=oss`. Required for that backend. |
| `OSS_EVENT_CALLBACK_KEY` | OSS upload-callback signing key. Feature-specific. |
| `OSS_EVENT_VERIFY_SIGNATURE` | Verify OSS event signatures. |

---

## DeepSeek / OpenAI-compatible LLM

| | |
| --- | --- |
| **Purpose** | Text and table summarization, heading hierarchy, and other OpenAI-compatible chat calls. The default text model is `deepseek-v4-flash`. |
| **Required?** | Configure the provider key for the models you select. The default text and vision models use `DS_KEY`; an alternate provider requires matching model and provider settings. URLs and model names have code defaults. |
| **Data leaving the box?** | **Yes.** Chunk text, table HTML, and prompts are sent to the provider you enable. |

| Key | Notes |
| --- | --- |
| `DS_KEY` | DeepSeek API key. |
| `DS_URL` | Optional. Default is documented in `.env.example`. |
| `GPT_API_KEY` | Retained configuration field; the shared client does not select it. Setting this key alone does not configure an OpenAI provider. |
| `GLM_API_KEY` | Zhipu GLM API key. |
| `GLM_URL` | Optional GLM base URL. |
| `ARK_API_KEY` | Volcengine Ark API key. |
| `ARK_URL` | Optional Ark URL. |
| `NORMOL_MODEL` | Optional text-model override (summaries and general LLM calls). |
| `HIERARCHY_LLM_MODEL` | Optional heading-hierarchy model; falls back to `NORMOL_MODEL`. |

The shared client routes Qwen, GLM, and Ark model names to their respective
providers. Other model names use `DS_URL` and `DS_KEY` unless the caller
supplies an explicit API URL and key. For a custom OpenAI-compatible endpoint,
set `DS_URL`, `DS_KEY`, and a model available at that endpoint.

---

## Vision models (DeepSeek / DashScope)

| | |
| --- | --- |
| **Purpose** | Image summaries, OCR, PDF coarse classification, atlas routing, and other vision calls. Default `IMAGE_MODEL` is `deepseek-flash` (DeepSeek-V4.1-Flash). |
| **Required?** | Required for image / OCR / atlas / V2 Vision Page parsing. The default model uses `DS_KEY` and `DS_URL`. Qwen models use `ALI_API_KEYS` and `ALI_URL` when selected as overrides. |
| **Data leaving the box?** | **Yes.** Extracted images and vision prompts are sent to the configured VLM endpoint (DeepSeek by default). |

| Key | Notes |
| --- | --- |
| `IMAGE_MODEL` | Optional VLM override; default is `deepseek-flash`. |
| `ASSET_MODEL` | Optional page-memory chart/table bounding-box VLM override. Empty follows `IMAGE_MODEL`. |
| `ALI_API_KEYS` | Aliyun DashScope key pool for Qwen overrides (JSON array, comma/newline list, or `token_id=api_key`). |
| `ALI_URL` | Optional DashScope-compatible base URL for Qwen overrides. |

---

## Cursor (agent_explore harness)

| | |
| --- | --- |
| **Purpose** | Default agentic retrieval path (`agent_explore`). The harness talks to Cursor when `AGENT_EXPLORE_HARNESS` is `cursor_sdk`. |
| **Required?** | Required for the default agentic route when the harness is `cursor_sdk`: set `CURSOR_API_KEY`. Set `use_agentic=false` on the query to stay on classic map-unit BM25 and skip this vendor. The `openai` harness reuses the OpenAI-compatible LLM keys above instead of Cursor. |
| **Data leaving the box?** | **Yes, when `cursor_sdk` is used.** Query text and corpus-tool traffic go to Cursor. |

| Key | Notes |
| --- | --- |
| `AGENT_EXPLORE_HARNESS` | Optional. Commented in `.env.example`. Default is `cursor_sdk`. The other documented value is `openai`. |
| `CURSOR_API_KEY` | Required when the harness is `cursor_sdk`. Commented in `.env.example`. |
| `AGENT_EXPLORE_CURSOR_MODEL` | Optional. Commented in `.env.example`; example default is `composer-2.5`. |

---

## MinerU

| | |
| --- | --- |
| **Purpose** | V1 chunk-track PDF parsing, including PPTX after conversion. Knowhere sends PDFs (or reusable S3 URLs) to MinerU, then rebuilds hierarchy from the Markdown / layout output. V2 PDF/PPTX Vision Page parsing uses vision models instead. |
| **Required?** | Required for the standard V1 chunk-based PDF/PowerPoint pipeline (`MINERU_API_KEYS`). The V1 atlas route and V2 Vision Page parsing bypass MinerU. |
| **Data leaving the box?** | **Yes, when MinerU parsing is used.** PDF bytes or presigned object URLs go to the MinerU API. |

| Key | Notes |
| --- | --- |
| `MINERU_API_KEYS` | MinerU API key pool (same pool syntax as `ALI_API_KEYS`). |
| `MINERU_URL` | Optional API base URL. |
| `MINERU_TOKEN_RPM_LIMIT` | Optional per-token RPM limit. |
| `MINERU_TOKEN_DAILY_LIMIT` | Optional per-token daily limit. |
| `MINERU_TOKEN_COOLDOWN_SECONDS` | Optional cooldown after rate limiting. |
| `MINERU_UPLOAD_MODE_ENABLED` | Optional. Default prefers S3 URL mode when the source object is reusable. Set true only when MinerU cannot fetch your storage URLs. |
| `MINERU_SHARD_CONCURRENCY` | Local cap on concurrent MinerU calls for oversized PDF shards. |

Related local PDF limits in the same `.env.example` files (not vendor keys):
`MAX_PDF_PAGE_LIMIT`, `OVERSIZED_PDF_SHARD_ENABLED`, `OVERSIZED_PDF_SOFT_LIMIT`,
`PDF_PROFILE_TOC_ENABLED`.

---

## iLoveAPI

| | |
| --- | --- |
| **Purpose** | Optional PPTX → PDF conversion for V1 chunk parsing or V2 Vision Page normalization. |
| **Required?** | Optional when local LibreOffice is available. Both tracks fall back to LibreOffice when iLoveAPI is unavailable. V1 conversion is followed by MinerU; V2 uses vision models. |
| **Data leaving the box?** | **Yes, when iLoveAPI is used.** Presentation files are uploaded to iLoveAPI / iLovePDF. LibreOffice conversion is local. |

| Key | Notes |
| --- | --- |
| `ILOVEAPI_PUBLIC_KEY` | Project public key. |
| `ILOVEAPI_SECRET_KEY` | Project secret key. |
| `ILOVEAPI_BASE_URL` | Optional API base URL. |
| `ILOVEAPI_TIMEOUT` | Optional request timeout in seconds. |

---

## PostHog telemetry

| | |
| --- | --- |
| **Purpose** | Anonymous self-hosted product telemetry (install liveness, usage aggregates, client/document mix). Schema and allowlists: [ADR-0004](adr/0004-anonymous-self-hosted-telemetry.md). |
| **Required?** | Default-on. Not required to run the product. Opt out with `TELEMETRY_ENABLED=false`. Keys are in `apps/api/.env.example` only. |
| **Data leaving the box?** | **Yes, if enabled** — only allowlisted anonymous events. Events must not include filenames, prompts, emails, IPs, geo, or document content. |

| Key | Notes |
| --- | --- |
| `TELEMETRY_ENABLED` | `true` by default. Set `false` to disable. |
| `TELEMETRY_POSTHOG_HOST` | Optional ingestion host. |
| `TELEMETRY_POSTHOG_PROJECT_KEY` | Optional project token. Commented in `apps/api/.env.example`. |
| `TELEMETRY_INSTALLATION_ID` | Optional operator-provided install id. |
| `TELEMETRY_INSTALLATION_ID_PATH` | File used to persist a generated install id. |
| `TELEMETRY_BATCH_SIZE` | Optional event batch size. |
| `TELEMETRY_REQUEST_TIMEOUT_SECONDS` | Optional outbound timeout. |
| `TELEMETRY_DEPLOYMENT_MODE` | Optional label on events (default `self_hosted`). |
| `TELEMETRY_AGGREGATE_INTERVAL_SECONDS` | Optional aggregate snapshot interval. |

---

## Stripe

| | |
| --- | --- |
| **Purpose** | Credits / checkout billing for hosted product flows. |
| **Required?** | Required when `BILLING_ENABLED=true` and you accept payments. Self-hosted and API-only local work can leave Stripe empty (`BILLING_ENABLED` is `false` in the worker example). |
| **Data leaving the box?** | **Yes, when enabled.** Customer and payment objects go to Stripe. Document content is not the billing payload. |

| Key | Notes |
| --- | --- |
| `BILLING_ENABLED` | Master switch for Stripe/credits billing. |
| `STRIPE_SECRET_KEY` | Stripe secret key. |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret. |
| `FREE_PLAN_INITIAL_CREDITS` | API-only. Starting credits for the free plan. |
| `FRONTEND_URL` | API-only. Checkout success/cancel return URL. |

---

## Other optional integrations

These are listed in `.env.example` under observability, webhooks, or
analytics. They are not required to parse documents locally.

### Logfire

| | |
| --- | --- |
| **Purpose** | Optional distributed tracing / logging. |
| **Required?** | Optional. Leave `LOGFIRE_TOKEN` empty unless you use Logfire. |
| **Data leaving the box?** | **Yes, if a token is set.** Traces and log metadata go to Logfire. |

| Key | Notes |
| --- | --- |
| `LOGFIRE_TOKEN` | Logfire API token. |

### QStash (outbound webhooks)

| | |
| --- | --- |
| **Purpose** | Deliver async webhooks through Upstash QStash, with signed callbacks into the API. |
| **Required?** | Required for QStash-backed webhook delivery. |
| **Data leaving the box?** | **Yes, if enabled.** Webhook payloads go through QStash to the configured webhook endpoint. Delivery-status callbacks return to the API. |

| Key | Notes |
| --- | --- |
| `QSTASH_TOKEN` | QStash API token. |
| `QSTASH_CALLBACK_BASE_URL` | Public API base URL used to build QStash callback paths. |
| `QSTASH_MAX_RETRIES` | Delivery retry count. |
| `QSTASH_CURRENT_SIGNING_KEY` | Required for QStash callback signature verification, together with the next signing key. |
| `QSTASH_NEXT_SIGNING_KEY` | Required for QStash callback signature verification and key rotation, together with the current signing key. |
| `WEBHOOK_MASTER_KEY` | Local webhook encryption key, not a QStash credential. |

### Moesif (retained configuration)

| | |
| --- | --- |
| **Purpose** | Retained API analytics configuration; this repository has no active Moesif middleware or outbound analytics client. |
| **Required?** | Not used by the current runtime. |
| **Data leaving the box?** | None through a Moesif integration in the current implementation. |

| Key | Notes |
| --- | --- |
| `MOESIF_APPLICATION_ID` | Retained Moesif application-id setting; setting it alone does not enable analytics. |

---

## Keys that are not vendor credentials

The same `.env.example` files also contain local runtime knobs (file limits,
parser schema, entity types, standalone API mode, dashboard URL, and
`RETRIEVAL_POSTGRES_FTS_CANDIDATE_LIMIT`). They are not external services.
See the comments in those files.

HTML and HTM ingestion is implemented in the worker
(`formats/html` + `SUPPORTED_EXTENSIONS`). It does not add a separate vendor.
