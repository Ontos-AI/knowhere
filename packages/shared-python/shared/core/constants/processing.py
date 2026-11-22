"""Processing-related constants."""


class ProcessingConstants:
    """Processing-related constants."""

    # File processing.
    MAX_THRESHOLD = 2000
    MIN_THRESHOLD = 500
    SUMMARY_LEN = 200
    KEYWORDS_NUM = 3
    POSTPROCESS_SUMMARY_LEN = 1500
    IMG_MAX_TOKENS = 200
    IMG_OCR_MAX_TOKENS = 2000
    MAX_TOKENS_MULTIPLIER = 1.3
    MAX_TOKENS_HEADER_MULTIPLIER = 1.5
    MAX_TOKENS_REWRITE_MULTIPLIER = 1.2
    MAX_TOKENS_SUMMARY_MULTIPLIER = 1.2
    MAX_TOKENS_JUDGE = 20
    MAX_TOKENS_TITLE_MULTIPLIER = 200
    MAX_TOKENS_SUBS_MULTIPLIER = 1.2
    MAX_TOKENS_REWRITE_SENTENCE_MULTIPLIER = 1.3
    MAX_TOKENS_QA_MULTIPLIER = 1.5
    MAX_TOKENS_QA_OUTPUT_MULTIPLIER = 1.2
    MAX_TOKENS_QA_OUTPUT_LIMIT = 20

    # Image processing.
    IMG_MIN_SIZE = 10 * 1024  # 10KB
    IMG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
    IMG_QUALITY = 100
    # Resolution page images and rendered graphics are drawn at.
    PAGE_RENDER_DPI = 144
    IMG_CHUNK_SIZE = 1024 * 1024  # 1MB

    # PostgreSQL connection-pool configuration.
    # Base pool size, tuned to CPU core count.
    DB_POOL_SIZE = 20
    # Maximum overflow connections allowed during load spikes.
    DB_MAX_OVERFLOW = 30
    # Connection recycle time in seconds; 30 minutes avoids stale long-lived connections.
    DB_POOL_RECYCLE = 1800
    # Connection timeout in seconds.
    DB_CONNECT_TIMEOUT = 30
    # Pool timeout in seconds; maximum wait time for acquiring a connection.
    DB_POOL_TIMEOUT = 30
    # Pre-ping connections to ensure they are still valid.
    DB_POOL_PRE_PING = True
    # Reset-on-return strategy for pooled connections.
    DB_POOL_RESET_ON_RETURN = "commit"
    # Maximum connection lifetime in seconds.
    DB_MAX_CONNECTION_LIFETIME = 3600

    # PostgreSQL performance tuning.
    # Query cache size.
    DB_QUERY_CACHE_SIZE = 1000
    # Batch operation size.
    DB_BATCH_SIZE = 1000
    # Transaction isolation level.
    DB_ISOLATION_LEVEL = "READ_COMMITTED"
    # Autocommit mode.
    DB_AUTOCOMMIT = False
    # Whether to prewarm the pool.
    DB_POOL_PREWARM = True
    # Connection validation query.
    DB_VALIDATION_QUERY = "SELECT 1"
    # Connection validation timeout.
    DB_VALIDATION_TIMEOUT = 5

    # Training parameters.
    TRAIN_BATCH_SIZE = 16
    MEMORY_K = 2
    MEMORY_LR = 0.001
    MEMORY_BATCH_SIZE = 2
    MEMORY_N_EPOCHS = 3
    MEMORY_MAX_INPUT_LENGTH = 2048
