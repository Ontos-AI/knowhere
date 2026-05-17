from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import and_, select

from shared.core.exceptions.domain_exceptions import (
    SystemSettingInvalidException,
    SystemSettingMissingException,
)
from shared.models.database.webhook_secret import (
    WebhookSecret,
    WebhookSecretStatus,
)
from shared.services.encryption import get_fernet_service


class QStashSecretResolver:
    """Sync webhook secret resolver for QStash publication."""

    def resolve(self, db: Any, *, user_id: str, endpoint: str) -> Optional[str]:
        try:
            fernet = get_fernet_service()
        except (SystemSettingMissingException, SystemSettingInvalidException) as exc:
            logger.error(f"Configuration error during secret resolution: {exc}")
            return None

        secret = self._find_active_secret(db, user_id=user_id, endpoint=endpoint)
        if secret is None:
            raw_secret = fernet.generate_webhook_secret()
            secret = WebhookSecret(
                user_id=user_id,
                endpoint=endpoint,
                secret_encrypted=fernet.encrypt(raw_secret),
                status=WebhookSecretStatus.ACTIVE,
            )
            db.add(secret)
            db.commit()
            db.refresh(secret)

        secret.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(secret)
        return fernet.decrypt(secret.secret_encrypted)

    def _find_active_secret(
        self,
        db: Any,
        *,
        user_id: str,
        endpoint: str,
    ) -> WebhookSecret | None:
        if endpoint:
            result = db.execute(
                select(WebhookSecret).where(
                    and_(
                        WebhookSecret.user_id == user_id,
                        WebhookSecret.endpoint == endpoint,
                        WebhookSecret.status == WebhookSecretStatus.ACTIVE,
                    )
                )
            )
            secret = result.scalar_one_or_none()
            if secret is not None:
                return secret

        result = db.execute(
            select(WebhookSecret).where(
                and_(
                    WebhookSecret.user_id == user_id,
                    WebhookSecret.endpoint.is_(None),
                    WebhookSecret.status == WebhookSecretStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()
