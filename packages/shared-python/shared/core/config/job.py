"""
Job configuration settings
"""

from pydantic import Field
from pydantic_settings import BaseSettings

DEFAULT_JOB_WAITING_EXPIRE_SECONDS = 7200


class JobConfig(BaseSettings):
    """Job configuration settings"""

    JOB_WAITING_EXPIRE_SECONDS: int = Field(
        default=DEFAULT_JOB_WAITING_EXPIRE_SECONDS,
        description="Max seconds a job can stay in pending or waiting-file before auto-failing (default: 2 hours). Also controls presigned S3 URL lifetime.",
    )
    JOB_PROCESSING_EXPIRE_SECONDS: int = Field(
        default=14400,
        description="Max seconds a job can stay in running or converting before auto-failing (default: 4 hours).",
    )
