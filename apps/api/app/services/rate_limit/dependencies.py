"""FastAPI adapters for the Job Admission module."""

from typing import AsyncGenerator

from app.core.dependencies import get_current_user_id
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.job_admission_service import JobAdmissionService
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from shared.core.database import get_db
_job_admission_service = JobAdmissionService()

async def with_current_user(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> AsyncGenerator[CurrentUser, None]:
    current_user = await _job_admission_service.resolve_current_user(
        request=request,
        user_id=user_id,
    )
    yield current_user


async def require_billing_limits(
    request: Request,
    current_user: CurrentUser = Depends(with_current_user),
    _db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[CurrentUser, None]:
    del request
    del _db
    await _job_admission_service.enforce_billing_limits(current_user=current_user)
    yield current_user


async def require_route_system_limit(request: Request) -> None:
    await _job_admission_service.enforce_route_system_limit(request=request)


async def enforce_job_creation_capacity(
    request: Request,
    db: AsyncSession,
    current_user: CurrentUser,
) -> None:
    del request
    await _job_admission_service.enforce_job_creation_capacity(
        db=db,
        current_user=current_user,
    )
