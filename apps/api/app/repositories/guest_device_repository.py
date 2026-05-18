"""Guest device data access layer."""
from typing import Optional

from app.repositories.base_repository import BaseRepository
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.guest_device import GuestDevice
from shared.utils.utc_now import utc_now_naive


class GuestDeviceRepository(BaseRepository[GuestDevice, dict, dict]):
    """Data access for guest device pairings."""

    def __init__(self) -> None:
        super().__init__(GuestDevice)

    async def get_by_device_id(
        self, session: AsyncSession, device_id: str
    ) -> Optional[GuestDevice]:
        """Look up a guest device pairing by client-provided device_id."""
        result = await session.execute(
            select(GuestDevice).where(GuestDevice.device_id == device_id)
        )
        return result.scalar_one_or_none()

    async def get_by_device_id_for_update(
        self, session: AsyncSession, device_id: str
    ) -> Optional[GuestDevice]:
        """Look up and lock a guest device row (SELECT ... FOR UPDATE)."""
        result = await session.execute(
            select(GuestDevice)
            .where(GuestDevice.device_id == device_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def update_api_key(
        self, session: AsyncSession, device_id: str, api_key_id: str
    ) -> Optional[GuestDevice]:
        """Update the api_key_id for an existing device pairing."""
        await session.execute(
            update(GuestDevice)
            .where(GuestDevice.device_id == device_id)
            .values(api_key_id=api_key_id, updated_at=utc_now_naive())
        )
        return await self.get_by_device_id(session, device_id)
