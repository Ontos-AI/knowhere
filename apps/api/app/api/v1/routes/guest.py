"""Guest registration routes."""

from app.services.guest.guest_registration_service import GuestRegistrationService
from app.api.dependencies.job_admission import require_route_system_limit
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.guest import GuestRegisterRequest, GuestRegisterResponse

router = APIRouter()


@router.post("", summary="Register guest device", response_model=GuestRegisterResponse)
async def register_guest(
    payload: GuestRegisterRequest,
    _system_limit: None = Depends(require_route_system_limit),
    db: AsyncSession = Depends(get_db),
) -> GuestRegisterResponse:
    """Register a guest device and return an API key.

    If the device_id is already registered, the endpoint returns a conflict
    instead of rotating the existing guest API key.
    """
    service = GuestRegistrationService()
    return await service.register_guest(
        session=db,
        device_id=payload.device_id,
        client=payload.client,
        platform=payload.platform,
        app_version=payload.app_version,
    )
