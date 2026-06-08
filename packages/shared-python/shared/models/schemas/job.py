"""
统一Job相关Schema（符合PRD规范）
"""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class WebhookConfig(BaseModel):
    """Webhook配置"""

    url: str = Field(..., description="Webhook回调URL")
    
class ParsingParams(BaseModel):
    """解析参数"""
    model: Literal["base", "advanced"] = Field("base", description="使用的模型")
    ocr_enabled: bool = Field(False, description="是否启用OCR")
    kb_dir: Optional[str] = Field("Default_Root", description="知识库目录")
    doc_type: Literal["auto", "pdf", "docx", "txt", "md"] = Field("auto", description="文档类型")
    smart_title_parse: bool = Field(True, description="智能标题解析")
    summary_image: bool = Field(True, description="是否生成图片摘要")
    summary_table: bool = Field(True, description="是否生成表格摘要")
    summary_txt: bool = Field(True, description="是否生成文本摘要")
    add_frag_desc: Optional[str] = Field("", description="添加片段描述")
    is_atlas: bool = Field(False, description="是否将PDF显式按图集解析")


class JobCreate(BaseModel):
    """创建任务请求"""

    source_type: Literal["file", "url"] = Field(..., description="文档来源类型")
    source_url: Optional[str] = Field(
        None, description="文件URL（source_type=url时必填）"
    )
    file_name: Optional[str] = Field(
        None, description="文件名（source_type=file时必填，需包含扩展名）"
    )
    data_id: Optional[str] = Field(None, max_length=128, description="用户自定义ID")
    parsing_params: Optional[ParsingParams] = Field(None, description="解析参数")
    webhook: Optional[WebhookConfig] = Field(None, description="Webhook配置")


class JobResponse(BaseModel):
    """任务响应"""

    job_id: str = Field(..., description="任务ID")
    status: Literal["pending", "waiting-file", "running", "converting", "done", "failed"] = Field(..., description="任务状态")
    source_type: str = Field(..., description="文件来源类型")
    data_id: Optional[str] = Field(None, description="用户自定义ID")
    created_at: datetime = Field(..., description="创建时间")

    # waiting-file状态特有字段
    upload_url: Optional[str] = Field(None, description="上传URL")
    upload_headers: Optional[Dict[str, str]] = Field(None, description="上传请求头")
    expires_in: Optional[int] = Field(None, description="URL过期时间（秒）")


class StandardErrorObject(BaseModel):
    """
    Standard error object for embedded error pattern.
    
    Reuses the same structure as synchronous API errors,
    enabling clients to use the same error handling logic
    for both sync and async (job) errors.
    """
    code: str = Field(..., description="Canonical error code (e.g., INVALID_ARGUMENT, INTERNAL_ERROR)")
    message: str = Field(..., description="Human-readable error message")
    request_id: str = Field(..., description="Original request ID for tracing")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional error details (violations, retry_after, etc.)")


class JobResultResponse(BaseModel):
    """Job status query response (for GET /jobs/{job_id}/result)"""

    job_id: str = Field(..., description="Job ID")
    status: Literal["pending", "waiting-file", "running", "converting", "done", "failed"] = Field(..., description="Job status")
    source_type: str = Field(..., description="File source type")
    data_id: Optional[str] = Field(None, description="User-defined ID")
    created_at: datetime = Field(..., description="Creation time")

    # Status-related fields
    progress: Optional[Dict[str, Any]] = Field(None, description="Progress information")
    
    # Error field - uses StandardErrorObject for embedded error pattern
    error: Optional[StandardErrorObject] = Field(None, description="Error information (only when status=failed)")

    # Result-related fields
    result: Optional[Dict[str, Any]] = Field(None, description="Parsing result (contains checksum and statistics)")
    result_url: Optional[str] = Field(None, description="Result file URL (ZIP download link)")
    result_url_expires_at: datetime = Field(..., description="Result URL expiration time")
    
    # Extended fields
    file_name: Optional[str] = Field(None, description="Source file name")
    file_extension: Optional[str] = Field(None, description="File extension, uppercase")
    model: Optional[str] = Field(None, description="Parsing model used")
    ocr_enabled: Optional[bool] = Field(None, description="Whether OCR is enabled")
    duration_seconds: Optional[float] = Field(None, description="Job duration (updated_at - created_at, in seconds)")
    credits_spent: Optional[float] = Field(None, description="Credits consumed")

class JobList(BaseModel):
    """任务列表响应"""

    jobs: list[JobResultResponse] = Field(..., description="任务列表")
    total: int = Field(..., description="总数量")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页数量")
    total_pages: int = Field(..., description="总页数")


class ConfirmUploadRequest(BaseModel):
    """确认上传请求"""

    pass  # 无需额外参数，从URL路径获取job_id
