"""Guest registration business logic."""

import hashlib
from datetime import datetime
from typing import NoReturn
from uuid import uuid4

from app.repositories.guest_device_repository import GuestDeviceRepository
from app.services.rate_limit.config import RateLimitConfig
from app.services.rate_limit.data_structures import TierLimits
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    UnavailableException,
)
from shared.models.database.guest_device import GuestDevice
from shared.models.database.user import User
from shared.models.database.user_balance import UserBalance
from shared.models.schemas.guest import (
    GuestRateLimitInfo,
    GuestRegisterResponse,
)
from shared.services.billing.credits_service import CreditsService
from shared.utils.api_keys import generate_api_key, hash_api_key, mask_api_key

_GUEST_TIER: str = "guest"
_GUEST_KEY_NAME_PREFIX: str = "guest-device"
_GUEST_DISPLAY_NAME_PREFIX: str = "Guest "
_GUEST_DISPLAY_NAME_MAX_LENGTH: int = 255
_GUEST_EMAIL_DOMAIN: str = "guest.knowhere.local"
_GUEST_RATE_LIMIT_RETRY_AFTER_SECONDS: int = 60


class GuestRegistrationService:
    """Handles guest device registration and API key issuance."""

    def __init__(self) -> None:
        self._device_repo = GuestDeviceRepository()
        self._credits_service = CreditsService()

    async def register_guest(
        self,
        session: AsyncSession,
        device_id: str,
        client: str,
        platform: str,
        app_version: str | None,
    ) -> GuestRegisterResponse:
        """Register or re-register a guest device.

        - If the device_id is new, create a guest user + non-expiring API key + device record.
        - If the device_id already exists, reject the request to prevent key takeover.
        - Handles concurrent first-registration race by waiting for the winner row
          and returning the same duplicate-registration conflict.
        """
        expires_at = None
        key_name = f"{_GUEST_KEY_NAME_PREFIX}-{device_id[:32]}"

        existing = await self._device_repo.get_by_device_id(session, device_id)

        if existing is not None:
            await self._device_repo.get_by_device_id_for_update(session, device_id)
            self._raise_existing_device_conflict(device_id)

        try:
            return await self._create_new_guest(
                session, device_id, client, platform, app_version, key_name, expires_at
            )
        except IntegrityError as error:
            if not self._is_concurrent_guest_registration_error(error):
                raise
            await session.rollback()
            locked = await self._device_repo.get_by_device_id_for_update(
                session, device_id
            )
            if locked is None:
                raise
            logger.info(
                "Guest registration conflict detected after concurrent create: device_id_hash={}",
                self._get_device_id_log_token(device_id),
            )
            self._raise_existing_device_conflict(device_id)

    async def _create_new_guest(
        self,
        session: AsyncSession,
        device_id: str,
        client: str,
        platform: str,
        app_version: str | None,
        key_name: str,
        expires_at: datetime | None,
    ) -> GuestRegisterResponse:
        """Create a brand-new guest user, API key, and device record.

        Everything is flushed (not committed) until the device row — which
        carries the unique device_id constraint — is inserted.  A single
        commit at the end ensures atomicity: if the device insert fails
        (e.g. concurrent race) the user/balance/key are rolled back too.
        """
        rate_limit = self._get_guest_rate_limit_info()
        user_id = str(uuid4())
        guest_name = self._build_guest_name(device_id)
        guest_email = self._build_guest_email(user_id)

        user = User(id=user_id, name=guest_name, email=guest_email)
        session.add(user)

        await self._credits_service.ensure_user_initialized(session, user_id)

        balance = await session.get(UserBalance, user_id)
        if balance is not None:
            balance.user_tier = _GUEST_TIER
            await session.flush()

        api_key = await self._create_api_key_without_commit(
            session, user_id, key_name, expires_at
        )

        api_key_id = await self._resolve_api_key_id(session, api_key)

        device = GuestDevice(
            device_id=device_id,
            user_id=user_id,
            api_key_id=api_key_id,
            client=client,
            platform=platform,
            app_version=app_version,
        )
        session.add(device)
        await session.commit()

        logger.info(
            "Created guest: device_id_hash={}, user_id={}",
            self._get_device_id_log_token(device_id),
            user_id,
        )
        return self._build_response(user_id, device_id, api_key, expires_at, rate_limit)

    async def _create_api_key_without_commit(
        self,
        session: AsyncSession,
        user_id: str,
        name: str,
        expires_at: datetime | None,
    ) -> str:
        """Generate an API key record and flush (but do not commit).

        This keeps the guest API key row in the same transaction as the device
        row so guest registration stays atomic.
        """
        from shared.models.database.api_key import APIKey

        api_key = generate_api_key()
        key_hash = hash_api_key(api_key)
        key_mask = mask_api_key(api_key)

        api_key_record = APIKey(
            user_id=user_id,
            key_hash=key_hash,
            key_mask=key_mask,
            name=name,
            enabled_modules=["guest"],
            expires_at=expires_at,
        )
        session.add(api_key_record)
        await session.flush()

        return api_key

    def _build_response(
        self,
        user_id: str,
        device_id: str,
        api_key: str,
        expires_at: datetime | None,
        rate_limit: GuestRateLimitInfo,
    ) -> GuestRegisterResponse:
        """Assemble the registration response with rate-limit metadata."""
        return GuestRegisterResponse(
            guest_user_id=user_id,
            device_id=device_id,
            api_key=api_key,
            rate_limit=rate_limit,
            expires_at=expires_at,
        )

    @staticmethod
    def _get_guest_rate_limit_info() -> GuestRateLimitInfo:
        """Read guest tier limits from the live config or fail closed."""
        config = RateLimitConfig.get_instance()
        tier: TierLimits | None = config.tier_map.get(_GUEST_TIER)
        if tier is None:
            raise UnavailableException(
                internal_message="Guest tier limits are not loaded in RateLimitConfig",
                retry_after=_GUEST_RATE_LIMIT_RETRY_AFTER_SECONDS,
            )
        return GuestRateLimitInfo(
            rpm=tier.rpm_limit,
            daily_quota=tier.daily_quota,
            max_concurrent_jobs=tier.max_concurrent_jobs,
        )

    @staticmethod
    def _build_guest_name(device_id: str) -> str:
        """Build a deterministic display name for guest-owned user rows."""
        available_length = _GUEST_DISPLAY_NAME_MAX_LENGTH - len(
            _GUEST_DISPLAY_NAME_PREFIX
        )
        normalized_device_id = device_id.strip()
        guest_suffix = normalized_device_id[:available_length]
        if guest_suffix:
            return f"{_GUEST_DISPLAY_NAME_PREFIX}{guest_suffix}"
        return _GUEST_DISPLAY_NAME_PREFIX.strip()

    @staticmethod
    def _build_guest_email(user_id: str) -> str:
        """Build a unique placeholder email for guest-owned user rows."""
        return f"guest+{user_id}@{_GUEST_EMAIL_DOMAIN}"

    @staticmethod
    def _is_concurrent_guest_registration_error(error: IntegrityError) -> bool:
        """Return True only for device_id uniqueness races on guest_devices."""
        statement = (error.statement or "").lower()
        error_text = str(getattr(error, "orig", error)).lower()

        targets_guest_device = (
            "guest_devices" in statement
            or "guest_devices" in error_text
            or "ix_guest_devices_device_id" in error_text
        )
        is_unique_violation = (
            "duplicate key value" in error_text
            or "uniqueviolation" in error_text
            or "unique constraint" in error_text
        )

        return (
            targets_guest_device and is_unique_violation and "device_id" in error_text
        )

    @staticmethod
    def _get_device_id_log_token(device_id: str) -> str:
        """Return a short stable token for logs without exposing the raw device_id."""
        return hashlib.sha256(device_id.encode()).hexdigest()[:12]

    @classmethod
    def _raise_existing_device_conflict(cls, device_id: str) -> NoReturn:
        """Reject duplicate guest registration for an existing device."""
        device_token = cls._get_device_id_log_token(device_id)
        raise ConflictException(
            user_message="This device is already registered. Use the existing guest API key for job APIs.",
            resource="GuestDevice",
            internal_message=f"Guest device already registered: device_id_hash={device_token}",
        )

    @staticmethod
    async def _resolve_api_key_id(session: AsyncSession, api_key: str) -> str | None:
        """Resolve the DB id for a just-created API key by its hash."""
        from sqlalchemy import select

        from shared.models.database.api_key import APIKey

        key_hash = hash_api_key(api_key)
        result = await session.execute(
            select(APIKey.id).where(APIKey.key_hash == key_hash).limit(1)
        )
        return result.scalar_one_or_none()
