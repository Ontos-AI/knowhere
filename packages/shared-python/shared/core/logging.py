import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict

from loguru import logger

if TYPE_CHECKING:
    from logfire.types import ExceptionCallbackHelper

_log_context: ContextVar[Dict[str, Any]] = ContextVar("log_context", default={})
_DEFAULT_CONSOLE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {extra[event]} | {message}"
)
_DEVELOPMENT_CONSOLE_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level> <cyan>{extra}</cyan>"


class LogEvent(Enum):
    """
    Registry of canonical event names for structured logging.

    Event names follow the pattern: domain.action.result
    - Use these constants instead of raw strings
    - Add new events to this registry in the same PR
    - Event names are stable; message text can evolve
    """

    # HTTP events
    HTTP_REQUEST_START = "http.request.start"
    HTTP_REQUEST_COMPLETE = "http.request.complete"

    # Exception events
    EXCEPTION_CLIENT = "exception.client"  # 4xx errors
    EXCEPTION_SYSTEM = "exception.system"  # 5xx errors

    # Worker events
    WORKER_TASK_START = "worker.task.start"
    WORKER_TASK_COMPLETE = "worker.task.complete"
    WORKER_TASK_RETRY = "worker.task.retry"
    WORKER_TASK_FAILURE = "worker.task.failure"

    # Correlation diagnostics
    CORRELATION_REQUEST_ID_MISSING = "correlation.request_id_missing"

    # System events
    LOGGING_CONFIGURED = "logging.configured"
    APP_LOG = "app.log"

    S3_WEBHOOK_EVENT = "s3.webhook"

    # iLoveAPI document conversion events
    ILOVEAPI_REQUEST_START = "iloveapi.request.start"
    ILOVEAPI_REQUEST_COMPLETE = "iloveapi.request.complete"
    ILOVEAPI_REQUEST_FAIL = "iloveapi.request.fail"
    ILOVEAPI_FALLBACK = "iloveapi.fallback"
    ILOVEAPI_RATE_LIMITED = "iloveapi.rate_limited"
    ILOVEAPI_CONCURRENCY_EXCEEDED = "iloveapi.concurrency_exceeded"


@contextmanager
def log_context(**kwargs):
    """
    Global context helper backed by ContextVar + Loguru contextualize().
    """
    current = _log_context.get()
    merged = {**current, **kwargs}
    token = _log_context.set(merged)
    try:
        with logger.contextualize(**kwargs):
            yield
    finally:
        _log_context.reset(token)


def get_log_context() -> Dict[str, Any]:
    """Get current merged log context."""
    return _log_context.get().copy()


def _is_expected_client_exception(exception: BaseException) -> bool:
    """Identify handled 4xx exceptions that should stay warnings in Logfire."""
    from shared.core.exceptions.knowhere_exception import KnowhereException

    if isinstance(exception, KnowhereException):
        return 400 <= exception.http_status_code < 500

    try:
        from fastapi import HTTPException as FastAPIHTTPException
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError:
        return False

    return (
        isinstance(
            exception,
            (FastAPIHTTPException, StarletteHTTPException),
        )
        and 400 <= exception.status_code < 500
    )


def _downgrade_expected_logfire_exception(
    helper: "ExceptionCallbackHelper",
) -> None:
    """Prevent handled client errors from creating Logfire exception issues."""
    if not _is_expected_client_exception(helper.exception):
        return

    helper.level = "warning"
    helper.no_record_exception()


def setup_logging(service_name: str):
    """
    Setup structured logging with optional Logfire integration.

    This function configures:
    - Human-readable text logging to stdout
    - Base schema fields for all logs
    - Optional Logfire structured telemetry
    - Log level filtering

    Args:
        service_name: Must be "knowhere-api" or "knowhere-worker"
        component: Must be "api" or "worker"
        app: FastAPI app instance (optional, for FastAPI instrumentation)

    Raises:
        ValueError: If service_name is not recognized

    Usage:
        # API startup (apps/api/main.py)
        setup_logging(service_name="knowhere-api")
        app = FastAPI(...)
        # Then instrument FastAPI separately if needed

        # Worker startup (apps/worker/worker.py)
        from celery.signals import worker_init

        @worker_init.connect()
        def init_worker(*args, **kwargs):
            setup_logging(service_name="knowhere-worker")
    """
    # Import settings here to avoid circular imports
    from shared.core.config import settings

    # Validate service_name
    if service_name not in ["knowhere-api", "knowhere-worker"]:
        raise ValueError(
            f"Invalid service_name: {service_name}. "
            "Must be 'knowhere-api' or 'knowhere-worker'"
        )

    # Remove all existing handlers
    logger.remove()

    # Set base context BEFORE any log emission so every line has base fields
    logger.configure(
        extra={
            "schema_version": "1.0",
            "environment": settings.APP_ENV,
            "event": LogEvent.APP_LOG.value,
        }
    )

    # Console handler - respects LOG_LEVEL
    log_level = settings.LOG_LEVEL
    show_bind_data = settings.ENVIRONMENT == "development"

    # enqueue=True uses a background thread for log writes, which deadlocks
    # with gevent's cooperative scheduling on stdout. Disable for worker.
    use_enqueue = service_name != "knowhere-worker"

    logger.add(
        sys.stdout,
        level=log_level,
        enqueue=use_enqueue,
        format=(
            _DEVELOPMENT_CONSOLE_FORMAT if show_bind_data else _DEFAULT_CONSOLE_FORMAT
        ),
    )

    # Logfire integration
    if settings.LOGFIRE_TOKEN:
        try:
            import logfire

            logfire.configure(
                service_name=service_name,
                token=settings.LOGFIRE_TOKEN,
                environment=settings.APP_ENV,
                console=False,
                distributed_tracing=True,
                advanced=logfire.AdvancedOptions(
                    exception_callback=_downgrade_expected_logfire_exception,
                ),
            )

            # Logfire sink: keep structured fields from bind()/contextualize().
            logger.add(
                **logfire.loguru_handler(),
                level="INFO",
            )

            # Instrument based on service type
            if service_name == "knowhere-api":
                logfire.instrument_celery()  # Required for trace propagation (enqueue side)
                logfire.instrument_httpx()  # Instrument HTTP client calls

                # Instrument database
                from shared.core.database import engine

                logfire.instrument_sqlalchemy(engine=engine)

                # Redis instrumentation disabled to reduce production noise/cost.

            elif service_name == "knowhere-worker":
                from shared.core.otel_gevent_compat import patch_otel_context_for_gevent

                patch_otel_context_for_gevent()
                logfire.instrument_celery()
                logfire.instrument_httpx()

                # Instrument sync database engine (worker uses psycopg2 via gevent)
                try:
                    from shared.core.database_sync import get_sync_engine

                    logfire.instrument_sqlalchemy(engine=get_sync_engine())
                except ImportError:
                    logger.bind(event=LogEvent.LOGGING_CONFIGURED.value).warning(
                        "Sync database engine not available for Logfire instrumentation"
                    )

                # Redis instrumentation disabled to reduce production noise/cost.

            logger.bind(event=LogEvent.LOGGING_CONFIGURED.value).info(
                f"Logfire integration enabled for {service_name}"
            )
        except ImportError:
            logger.bind(event=LogEvent.LOGGING_CONFIGURED.value).warning(
                "Logfire integration requested but logfire package not installed"
            )

    # Configure standard logging library to use loguru
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    _configure_noisy_third_party_loggers()

    logger.bind(event=LogEvent.LOGGING_CONFIGURED.value).info(
        f"Logging configured for {service_name} (level={log_level})"
    )


def _configure_noisy_third_party_loggers() -> None:
    """Reduce high-volume debug noise from third-party libraries."""
    noisy_logger_names = (
        "urllib3",
        "httpcore",
        "httpx",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
    )
    for logger_name in noisy_logger_names:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = logging.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )
