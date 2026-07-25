"""FastAPI current-user dependency adapters."""

from typing import AsyncGenerator

from app.api.dependencies.auth import get_current_user_id
from app.api.dependencies.route_admission import get_route_admission_context
from app.services.rate_limit.data_structures import (
    CurrentUser,
    RouteAdmissionContext,
)
from app.services.rate_limit.job_admission_service import JobAdmissionService
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db

_job_admission_service = JobAdmissionService()


async def with_current_user(
    route_context: RouteAdmissionContext = Depends(get_route_admission_context),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[CurrentUser, None]:
    current_user = await _job_admission_service.resolve_current_user(
        route_context=route_context,
        user_id=user_id,
        db=db,
    )
    yield current_user
