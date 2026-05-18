"""Unified job schemas."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class WebhookConfig(BaseModel):
    """Webhook configuration."""

    url: str = Field(..., description="Webhook callback URL")


class ParsingParams(BaseModel):
    """Parsing parameters."""

    model: Literal["base", "advanced"] = Field("base", description="Parsing model")
    ocr_enabled: bool = Field(False, description="Enable OCR")
    doc_type: Literal["auto", "pdf", "docx", "txt", "md"] = Field(
        "auto", description="Document type"
    )
    smart_title_parse: bool = Field(True, description="Enable smart heading parsing")
    summary_image: bool = Field(True, description="Generate image summaries")
    summary_table: bool = Field(True, description="Generate table summaries")
    summary_txt: bool = Field(True, description="Generate text summaries")
    add_frag_desc: Optional[str] = Field(
        "", description="Additional fragment description"
    )
    summary_use_llm: bool = Field(
        False,
        description=(
            "Use LLM to generate coherent hierarchical section summaries. "
            "When True, intermediate (non-leaf) sections get a real semantic "
            "summary instead of a plain title enumeration. "
            "Increases parse time and API token cost."
        ),
    )


class JobCreate(BaseModel):
    """Request payload for creating a job."""

    namespace: Optional[str] = Field(
        None,
        max_length=255,
        description="Retrieval namespace; defaults to default",
    )
    document_id: Optional[str] = Field(
        None, description="Existing document ID for update flows"
    )
    source_type: Literal["file", "url"] = Field(..., description="Document source type")
    source_url: Optional[str] = Field(
        None, description="Source URL; required when source_type=url"
    )
    file_name: Optional[str] = Field(
        None,
        description="File name; required when source_type=file and must include the extension",
    )
    data_id: Optional[str] = Field(None, max_length=128, description="User-defined ID")
    parsing_params: Optional[ParsingParams] = Field(
        None, description="Parsing parameters"
    )
    webhook: Optional[WebhookConfig] = Field(None, description="Webhook configuration")


class JobResponse(BaseModel):
    """Job-creation response."""

    job_id: str = Field(..., description="Job ID")
    namespace: Optional[str] = Field(None, description="Effective namespace")
    document_id: Optional[str] = Field(None, description="Linked document ID")
    status: Literal[
        "pending", "waiting-file", "running", "converting", "done", "failed"
    ] = Field(..., description="Job status")
    source_type: str = Field(..., description="Document source type")
    data_id: Optional[str] = Field(None, description="User-defined ID")
    created_at: datetime = Field(..., description="Creation time")

    # Fields used only in waiting-file state.
    upload_url: Optional[str] = Field(None, description="Upload URL")
    upload_headers: Optional[Dict[str, str]] = Field(
        None, description="Upload request headers"
    )
    expires_in: Optional[int] = Field(None, description="URL expiration in seconds")


class StandardErrorObject(BaseModel):
    """
    Standard error object for embedded error pattern.

    Reuses the same structure as synchronous API errors,
    enabling clients to use the same error handling logic
    for both sync and async (job) errors.
    """

    code: str = Field(
        ..., description="Canonical error code (e.g., INVALID_ARGUMENT, INTERNAL_ERROR)"
    )
    message: str = Field(..., description="Human-readable error message")
    request_id: str = Field(..., description="Original request ID for tracing")
    details: Optional[Dict[str, Any]] = Field(
        None, description="Additional error details (violations, retry_after, etc.)"
    )


class JobResultResponse(BaseModel):
    """Job status query response (for GET /jobs/{job_id}/result)"""

    job_id: str = Field(..., description="Job ID")
    namespace: Optional[str] = Field(None, description="Effective retrieval namespace")
    document_id: Optional[str] = Field(None, description="Linked document ID")
    status: Literal[
        "pending", "waiting-file", "running", "converting", "done", "failed"
    ] = Field(..., description="Job status")
    source_type: str = Field(..., description="File source type")
    data_id: Optional[str] = Field(None, description="User-defined ID")
    created_at: datetime = Field(..., description="Creation time")

    # Status-related fields
    progress: Optional[Dict[str, Any]] = Field(None, description="Progress information")

    # Error field - uses StandardErrorObject for embedded error pattern
    error: Optional[StandardErrorObject] = Field(
        None, description="Error information (only when status=failed)"
    )

    # Result-related fields
    result: Optional[Dict[str, Any]] = Field(
        None, description="Parsing result (contains checksum and statistics)"
    )
    result_url: Optional[str] = Field(
        None, description="Result file URL (ZIP download link)"
    )
    result_url_expires_at: datetime = Field(
        ..., description="Result URL expiration time"
    )

    # Extended fields
    file_name: Optional[str] = Field(None, description="Source file name")
    file_extension: Optional[str] = Field(None, description="File extension, uppercase")
    model: Optional[str] = Field(None, description="Parsing model used")
    ocr_enabled: Optional[bool] = Field(None, description="Whether OCR is enabled")
    duration_seconds: Optional[float] = Field(
        None, description="Job duration (updated_at - created_at, in seconds)"
    )
    credits_spent: Optional[float] = Field(None, description="Credits consumed")


class JobList(BaseModel):
    """Paginated job-list response."""

    jobs: list[JobResultResponse] = Field(..., description="Job list")
    total: int = Field(..., description="Total item count")
    page: int = Field(..., description="Current page number")
    page_size: int = Field(..., description="Items per page")
    total_pages: int = Field(..., description="Total page count")


class ConfirmUploadRequest(BaseModel):
    """Request payload for confirming a file upload."""

    pass  # No extra fields are required; job_id comes from the URL path.
