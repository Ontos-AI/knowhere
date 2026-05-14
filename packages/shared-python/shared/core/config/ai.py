"""AI model configuration."""

from pydantic import BaseModel, Field


class AIConfig(BaseModel):
    """AI model configuration."""

    # Provider credentials and primary model selection.
    GLM_API_KEY: str = Field(default="", description="Zhipu GLM API key")
    GLM_URL: str = Field(
        default="https://open.bigmodel.cn/api/paas/v4", description="Zhipu GLM API URL"
    )
    DS_KEY: str = Field(default="", description="DeepSeek API key")
    DS_URL: str = Field(
        default="https://api.deepseek.com/v1", description="DeepSeek API URL"
    )
    GPT_API_KEY: str = Field(default="", description="OpenAI API key")
    # Default behavior: text/table summaries use deepseek-chat. Hierarchy parsing
    # can be overridden independently with HIERARCHY_LLM_MODEL. Existing
    # environment overrides for NORMOL_MODEL / HIERARCHY_LLM_MODEL /
    # IMAGE_MODEL / IMAGE_MODEL_MAX remain supported.
    NORMOL_MODEL: str = Field(
        default="deepseek-chat",
        description="Default text model for summaries and general text LLM calls",
    )
    HIERARCHY_LLM_MODEL: str = Field(
        default="",
        description="Heading and outline recognition model; falls back to NORMOL_MODEL when empty",
    )
    IMAGE_MODEL: str = Field(
        default="qwen3.5-flash",
        description="Image model for image summary, atlas, and OCR flows",
    )

    IMAGE_MODEL_MAX: str = Field(
        default="qwen3.5-flash",
        description="Higher-capability image model for OCR and ask-image Q&A",
    )
    RETRIEVAL_DECOMPOSITION_ENABLED: bool = Field(
        default=False,
        description="Enable query-decomposition workflow before agentic retrieval.",
    )
    RETRIEVAL_PLANNER_MODEL: str = Field(
        default="",
        description="Reasoning-capable model used by the workflow query planner.",
    )
    RETRIEVAL_PLANNER_THINKING_BUDGET: int = Field(
        default=4000,
        description="Token budget for the query planner thinking call.",
    )
    RETRIEVAL_DECOMPOSITION_MAX_STEPS: int = Field(
        default=5,
        description="Maximum number of planned workflow steps.",
    )
    RETRIEVAL_WALLET_TOTAL_BUDGET: int = Field(
        default=200000,
        description="Total workflow token wallet for decomposed retrieval.",
    )
    RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET: int = Field(
        default=40000,
        description="Default token budget issued to each retrieve step.",
    )
    RETRIEVAL_WALLET_PER_SYNTHESIZE_STEP_BUDGET: int = Field(
        default=6000,
        description="Default token budget issued to each synthesize step.",
    )
    RETRIEVAL_WORKFLOW_PARALLEL_MAX: int = Field(
        default=3,
        description="Maximum concurrent workflow steps in the same DAG batch.",
    )

    # Runtime LLM controls.
    LLM_MOCK_ENABLED: bool = Field(
        default=False,
        description="Short-circuit all OpenAI-compatible LLM calls and return canned mock responses.",
    )
    OPENAI_CLIENT_TIMEOUT: int = Field(
        default=300, description="OpenAI-compatible client timeout in seconds"
    )
    SUMMARY_LLM_MAX_CONCURRENT: int = Field(
        default=8,
        description="Max concurrent gevent greenlets for parallel post-heading summary LLM calls -- image/table/text (Dashscope).",
    )

    # Compatibility fields retained during migration.
    ARK_API_KEY: str = Field(
        default="", description="ARK API key (compatibility field)"
    )
    ARK_URL: str = Field(default="", description="ARK URL (compatibility field)")
    ALI_API_KEYS: str = Field(
        default="",
        description="Ali API key pool. Supports JSON array or comma/newline-separated values; entries may use token_id=api_key format.",
    )
    ALI_URL: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="Aliyun DashScope URL (compatibility field)",
    )
    ALI_TOKEN_RPM_LIMIT: int = Field(
        default=300,
        description="Per-token requests-per-minute limit for Ali API keys.",
    )
    ALI_TOKEN_DAILY_LIMIT: int = Field(
        default=10000,
        description="Per-token daily request limit for Ali API keys.",
    )
    ALI_TOKEN_COOLDOWN_SECONDS: int = Field(
        default=60,
        description="Cooldown seconds after an Ali token receives 429.",
    )
    ALI_INLINE_MAX_RETRIES: int = Field(
        default=3,
        description="Maximum inline retries when an Ali token is rate-limited (429). Each retry acquires the next available token.",
    )
    ALI_SDK_MAX_RETRIES: int = Field(
        default=3,
        description="OpenAI SDK max_retries per token for transient 429s (exponential backoff + jitter).",
    )
    ILOVEAPI_PUBLIC_KEY: str = Field(
        default="", description="iLoveAPI public key (PPTX-to-PDF)"
    )
    ILOVEAPI_SECRET_KEY: str = Field(
        default="", description="iLoveAPI secret key (PPTX-to-PDF)"
    )
    ILOVEAPI_BASE_URL: str = Field(
        default="https://api.ilovepdf.com/v1", description="iLoveAPI base URL"
    )
    ILOVEAPI_TIMEOUT: int = Field(
        default=120, description="iLoveAPI request timeout in seconds"
    )
    ILOVEAPI_KEYS: str = Field(
        default="",
        description="iLoveAPI project pool as a JSON array of objects with public_key and secret_key. Each entry is a separate iLoveAPI project with its own credit quota.",
    )
    ILOVEAPI_TOKEN_RPM_LIMIT: int = Field(
        default=25,
        description="Per-project requests-per-minute burst limit for iLoveAPI. Safety net against 429 throttling.",
    )
    ILOVEAPI_TOKEN_DAILY_LIMIT: int = Field(
        default=250,
        description="Per-project daily file limit for iLoveAPI. The free tier is about 250 files per month for officepdf conversions.",
    )
    ILOVEAPI_MAX_CONCURRENT: int = Field(
        default=5,
        description="Max concurrent in-flight iLoveAPI conversions across all workers. Fail-open to LibreOffice when exceeded.",
    )
    SPLIT_CHAR: str = Field(default="/", description="Path separator")
    ALL_DF_COLS: str = Field(
        default="content,path,type,length,keywords,summary,know_id,tokens,connectto,addtime,page_nums",
        description="All dataframe columns (compatibility field)",
    )
