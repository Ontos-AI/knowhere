# External Services

Knowhere can run with a small set of required infrastructure services. Most
parser and retrieval tuning knobs have code defaults; configure only the
external services and provider keys your deployment actually uses.

## Required For API And Worker Startup

- `DATABASE_URL`: PostgreSQL connection URL.
- Redis: configure `REDIS_HOST` / `REDIS_PORT` or use the local defaults, plus
  `CELERY_REDIS_URL` for worker task delivery.
- S3-compatible storage: `S3_BUCKET_NAME`, `S3_ACCESS_KEY_ID`,
  `S3_SECRET_ACCESS_KEY`, and `S3_TEMP_PATH`. For local development, the
  example files point these at LocalStack.
- `TMP_PATH`: local temporary directory. The directory must exist.

## Required For Parsing And Retrieval

Configure at least one OpenAI-compatible text LLM provider key:

- `DS_KEY` for DeepSeek. `DS_URL` defaults to `https://api.deepseek.com/v1`.
- `ALI_API_KEYS` for DashScope/Qwen models. `ALI_URL` has a code default.
- `GPT_API_KEY` for OpenAI-compatible GPT models.
- `GLM_API_KEY` for Zhipu GLM models. `GLM_URL` has a code default.

`NORMOL_MODEL` defaults to `deepseek-chat`. `HIERARCHY_LLM_MODEL` defaults to
empty and falls back to `NORMOL_MODEL`, so deployments do not need to set it
unless they intentionally want a separate hierarchy-recognition model.

## Feature-Specific Providers

- PDF parsing currently routes non-atlas PDFs through MinerU, so PDF ingestion
  requires `MINERU_API_KEYS`.
- Image summaries, OCR, atlas classification, and image-aware retrieval require
  a vision-capable provider for `IMAGE_MODEL` / `IMAGE_MODEL_MAX`. The model
  names default to Qwen vision models, so configure `ALI_API_KEYS` or override
  those model names to match your provider.
- PPTX-to-PDF conversion can use iLovePDF. Configure `ILOVEAPI_PUBLIC_KEY` and
  `ILOVEAPI_SECRET_KEY` only if you need that provider path.
- Billing and outbound webhooks are optional. Configure Stripe, Moesif, or
  QStash only when those integrations are enabled for your deployment.

## Values With Code Defaults

The following groups are intentionally optional in `.env` files:

- Retrieval workflow knobs: `RETRIEVAL_AGENTIC_ENABLED`,
  `RETRIEVAL_PLANNER_THINKING_BUDGET`, `RETRIEVAL_DECOMPOSITION_MAX_STEPS`,
  `RETRIEVAL_WALLET_*`, and `RETRIEVAL_WORKFLOW_PARALLEL_MAX`.
- Agentic retrieval internals: `RETRIEVAL_AGENTIC_MAX_REVISIONS`,
  `RETRIEVAL_AGENTIC_MAX_NAV_DEPTH`, `RETRIEVAL_AGENTIC_TOKEN_BUDGET_TOTAL`,
  `RETRIEVAL_AGENTIC_PLANNING_RATIO`,
  `RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET`, and related trace or verbose flags.
- Parser defaults: `MINERU_URL`, MinerU rate-limit settings,
  `ILOVEAPI_BASE_URL`, `ILOVEAPI_TIMEOUT`, `SPLIT_CHAR`, and `ALL_DF_COLS`.
- File handling defaults: `SUPPORTED_EXTENSIONS` and `MAX_FILE_SIZE`.

Leave these unset for a normal fork or self-hosted deployment. Set them only
when you need to deliberately change behavior.
