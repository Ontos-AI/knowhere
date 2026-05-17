"""Webhook secret resolution for direct deliveries."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    SystemSettingInvalidException,
    SystemSettingMissingException,
)
from shared.models.database.job import Job
from shared.models.database.webhook import WebhookEvent
from shared.repositories.webhook_secret_repository import WebhookSecretRepository


class WebhookSecretResolver:
    """Resolve the active endpoint secret for a WebhookEvent delivery."""

    def __init__(self, repository: WebhookSecretRepository | None = None) -> None:
        self._repository = repository or WebhookSecretRepository()

    async def resolve_for_event(
        self, db: AsyncSession, event: WebhookEvent
    ) -> str | None:
        user_id = await self._get_job_owner(db, event.job_id)
        if not user_id:
            logger.warning(f"Could not resolve secret: Job {event.job_id} has no user_id")
            return None

        return await self.resolve_for_endpoint(
            db,
            user_id=user_id,
            endpoint=event.target_url,
        )

    async def resolve_for_endpoint(
        self, db: AsyncSession, *, user_id: str, endpoint: str
    ) -> str | None:
        try:
            secret = await self._repository.get_or_create_secret(
                db, user_id, endpoint=endpoint
            )

            if secret:
                secret.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.add(secret)

            return self._repository.decrypt_secret(secret)

        except (SystemSettingMissingException, SystemSettingInvalidException):
            raise
        except Exception as error:
            logger.error(f"Failed to resolve/create secret for user {user_id}: {error}")
            return None

    async def _get_job_owner(self, db: AsyncSession, job_id: str) -> str | None:
        result = await db.execute(select(Job.user_id).where(Job.job_id == job_id))
        return result.scalar_one_or_none()
