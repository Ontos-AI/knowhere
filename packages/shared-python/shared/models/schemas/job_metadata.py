"""Job metadata schemas."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from shared.models.schemas.llm_config import LLMConfig, parse_llm_config
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.utils.security_utils import mask_api_key


class JobMetadataBase(BaseModel):
    """Base schema for stored job metadata."""

    # Core fields captured at creation time.
    original_request: Optional[Dict[str, Any]] = Field(
        None, description="Full JobCreate request payload"
    )
    parsing_params: Optional[Dict[str, Any]] = Field(
        None, description="Parsing parameters"
    )
    llm_config: Optional[Dict[str, Any]] = Field(
        None, description="BYOK OpenAI-compatible LLM credentials (v2)"
    )
    data_id: Optional[str] = Field(None, description="User-defined ID")
    webhook: Optional[Dict[str, Any]] = Field(None, description="Webhook configuration")
    document_metadata: Optional[Dict[str, Any]] = Field(
        None, description="Display metadata copied to the published document"
    )
    api_version: Optional[str] = Field(None, description="Public API version")
    processing_generation: Optional[str] = Field(
        None, description="Resolved internal ingestion generation"
    )
    parse_track: Optional[str] = Field(None, description="Resolved parser track")
    page_memory_config: Optional[Dict[str, Any]] = Field(
        None, description="Resolved page-memory worker configuration"
    )
    # result_mode was removed and is no longer supported.

    # Source-file fields.
    source_type: Optional[str] = Field(None, description="Source type")
    source_file_name: Optional[str] = Field(None, description="Source file name")
    source_url: Optional[str] = Field(None, description="Source URL")
    file_url: Optional[str] = Field(None, description="File URL")

    # User config captured during creation.
    user_config: Optional[Dict[str, Any]] = Field(
        None, description="User configuration"
    )

    model_config = ConfigDict(extra="allow")


class JobMetadataHelper:
    """Helper methods for creating and reading job metadata."""

    @staticmethod
    def create_from_request(
        request,
        *,
        api_version: str = "v1",
        parse_track: str = "chunk",
        processing_generation: str = "legacy_chunk",
        page_memory_config: PageMemoryConfig | Dict[str, Any] | None = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build metadata from a public job request."""
        namespace = normalize_retrieval_namespace(request.namespace)
        resolved_page_memory_config: Dict[str, Any] | None
        if isinstance(page_memory_config, PageMemoryConfig):
            resolved_page_memory_config = page_memory_config.to_dict()
        else:
            resolved_page_memory_config = page_memory_config
        # v2-only field; getattr keeps v1 JobCreate (no llm_config) safe.
        raw_llm_config = getattr(request, "llm_config", None)
        llm_config_payload: Dict[str, Any] | None = None
        if isinstance(raw_llm_config, LLMConfig):
            llm_config_payload = raw_llm_config.model_dump()
        elif isinstance(raw_llm_config, dict):
            llm_config_payload = dict(raw_llm_config)
        metadata = {
            "original_request": _dump_public_request(request),
            "api_version": api_version,
            "namespace": namespace,
            "document_id": request.document_id,
            "parse_track": parse_track,
            "processing_generation": processing_generation,
            "parsing_params": (
                request.parsing_params.model_dump() if request.parsing_params else None
            ),
            "document_metadata": request.document_metadata or {},
            "data_id": request.data_id,
            "webhook": request.webhook.model_dump() if request.webhook else None,
        }
        if llm_config_payload is not None:
            metadata["llm_config"] = llm_config_payload
        if resolved_page_memory_config is not None:
            metadata["page_memory_config"] = resolved_page_memory_config
        metadata.update(kwargs)
        return metadata

    @staticmethod
    def set_document_scope(
        metadata: Dict[str, Any],
        *,
        document_id: str,
        namespace: str,
    ) -> None:
        """Store the effective retrieval document scope."""
        metadata["document_id"] = document_id
        metadata["namespace"] = namespace

    @staticmethod
    def set_file_source(metadata: Dict[str, Any], *, source_file_name: str) -> None:
        """Store source metadata for direct file uploads."""
        metadata["source_file_name"] = source_file_name
        metadata["source_type"] = "file"

    @staticmethod
    def set_url_source(
        metadata: Dict[str, Any],
        *,
        source_file_name: str,
        source_url: str,
    ) -> None:
        """Store source metadata for URL ingestion."""
        metadata["source_file_name"] = source_file_name
        metadata["source_url"] = source_url
        metadata["source_type"] = "url"

    @staticmethod
    def get_field(
        metadata: Optional[Dict[str, Any]], field: str, default: Any = None
    ) -> Any:
        """Safely read a field from metadata."""
        if not metadata:
            return default
        return metadata.get(field, default)

    @staticmethod
    def get_string_field(
        metadata: Optional[Dict[str, Any]], field: str, default: str | None = None
    ) -> str | None:
        """Read a string field from metadata."""
        value = JobMetadataHelper.get_field(metadata, field, default)
        return value if isinstance(value, str) else default

    @staticmethod
    def get_original_request(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return the stored creation request payload."""
        original_request = JobMetadataHelper.get_field(metadata, "original_request", {})
        return original_request if isinstance(original_request, dict) else {}

    @staticmethod
    def get_parsing_params_dict(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return stored parsing parameters as a dictionary."""
        parsing_params = JobMetadataHelper.get_field(metadata, "parsing_params", {})
        return parsing_params if isinstance(parsing_params, dict) else {}

    @staticmethod
    def get_namespace(
        metadata: Optional[Dict[str, Any]], default: str | None = None
    ) -> str | None:
        """Return the retrieval namespace stored in metadata."""
        namespace = JobMetadataHelper.get_string_field(metadata, "namespace", default)
        return normalize_retrieval_namespace(namespace) if namespace is not None else None

    @staticmethod
    def get_document_id(metadata: Optional[Dict[str, Any]]) -> str | None:
        """Return the retrieval document id stored in metadata."""
        return JobMetadataHelper.get_string_field(metadata, "document_id")

    @staticmethod
    def get_parse_track(metadata: Optional[Dict[str, Any]]) -> str:
        """Return the parser track stored in metadata."""
        return (
            JobMetadataHelper.get_string_field(metadata, "parse_track", "chunk")
            or "chunk"
        )

    @staticmethod
    def get_api_version(metadata: Optional[Dict[str, Any]]) -> str:
        """Return the API version stored in metadata."""
        return JobMetadataHelper.get_string_field(metadata, "api_version", "v1") or "v1"

    @staticmethod
    def get_processing_generation(metadata: Optional[Dict[str, Any]]) -> str:
        """Return the internal processing generation stored in metadata."""
        return (
            JobMetadataHelper.get_string_field(
                metadata,
                "processing_generation",
                "legacy_chunk",
            )
            or "legacy_chunk"
        )

    @staticmethod
    def get_page_memory_config(
        metadata: Optional[Dict[str, Any]],
    ) -> PageMemoryConfig:
        """Return resolved page-memory worker config stored in metadata."""
        config = JobMetadataHelper.get_field(metadata, "page_memory_config", {})
        return PageMemoryConfig.from_mapping(config)

    @staticmethod
    def get_document_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return display metadata copied to the published document."""
        document_metadata = JobMetadataHelper.get_field(
            metadata,
            "document_metadata",
            {},
        )
        return document_metadata if isinstance(document_metadata, dict) else {}

    @staticmethod
    def get_data_id(metadata: Optional[Dict[str, Any]]) -> str | None:
        """Return the user-defined data id stored in metadata."""
        return JobMetadataHelper.get_string_field(metadata, "data_id")

    @staticmethod
    def get_source_file_name(metadata: Optional[Dict[str, Any]]) -> str | None:
        """Return the source file name stored in metadata."""
        return JobMetadataHelper.get_string_field(metadata, "source_file_name")

    @staticmethod
    def get_source_url(metadata: Optional[Dict[str, Any]]) -> str | None:
        """Return the source URL stored in metadata."""
        return JobMetadataHelper.get_string_field(metadata, "source_url")

    @staticmethod
    def get_parsing_param(
        metadata: Optional[Dict[str, Any]], param: str, default: Any = None
    ) -> Any:
        """Read a value from parsing_params with backward compatibility."""
        if not metadata:
            return default

        parsing_params = metadata.get("parsing_params")
        if parsing_params and isinstance(parsing_params, dict):
            if param in parsing_params:
                return parsing_params.get(param, default)

        # Backward compatibility for older flat metadata layouts.
        if param in metadata:
            return metadata.get(param, default)

        return default

    @staticmethod
    def get_webhook(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return the webhook configuration from metadata."""
        return JobMetadataHelper.get_field(metadata, "webhook")

    @staticmethod
    def get_llm_config(metadata: Optional[Dict[str, Any]]) -> LLMConfig | None:
        """Return the BYOK LLM config stored in metadata, if any."""
        raw = JobMetadataHelper.get_field(metadata, "llm_config", None)
        return parse_llm_config(raw)


def _mask_llm_config_in_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Redact api_key values inside llm_config for public request snapshots."""
    llm_config = payload.get("llm_config")
    if not isinstance(llm_config, dict):
        return payload

    masked = dict(payload)
    masked_llm: Dict[str, Any] = dict(llm_config)
    if isinstance(masked_llm.get("api_key"), str):
        masked_llm["api_key"] = mask_api_key(masked_llm["api_key"])
    for slot in ("text", "vision"):
        provider = masked_llm.get(slot)
        if isinstance(provider, dict):
            provider_copy = dict(provider)
            if "api_key" in provider_copy:
                provider_copy["api_key"] = mask_api_key(
                    provider_copy.get("api_key")
                    if isinstance(provider_copy.get("api_key"), str)
                    else None
                )
            masked_llm[slot] = provider_copy
    masked["llm_config"] = masked_llm
    return masked


def _dump_public_request(request) -> Dict[str, Any]:
    """Dump declared public request fields without hidden compatibility extras."""
    extra_fields = getattr(request, "model_extra", None) or {}
    payload = request.model_dump(exclude=set(extra_fields))
    return _mask_llm_config_in_request(payload)
