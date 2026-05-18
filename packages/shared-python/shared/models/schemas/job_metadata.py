"""Job metadata schemas."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace


class JobMetadataBase(BaseModel):
    """Base schema for stored job metadata."""

    # Core fields captured at creation time.
    original_request: Optional[Dict[str, Any]] = Field(
        None, description="Full JobCreate request payload"
    )
    parsing_params: Optional[Dict[str, Any]] = Field(
        None, description="Parsing parameters"
    )
    data_id: Optional[str] = Field(None, description="User-defined ID")
    webhook: Optional[Dict[str, Any]] = Field(None, description="Webhook configuration")
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
    def create_from_request(request, **kwargs) -> Dict[str, Any]:
        """Build metadata from a JobCreate request without embedding user_config."""
        namespace = normalize_retrieval_namespace(request.namespace)
        metadata = {
            "original_request": request.model_dump(),
            "namespace": namespace,
            "document_id": request.document_id,
            "parsing_params": (
                request.parsing_params.model_dump() if request.parsing_params else None
            ),
            "data_id": request.data_id,
            "webhook": request.webhook.model_dump() if request.webhook else None,
        }
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
