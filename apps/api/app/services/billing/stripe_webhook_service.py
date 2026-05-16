from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

import stripe
from app.repositories.payment_record_repository import PaymentRecordRepository
from app.services.billing.stripe_credits_settlement_service import (
    StripeCreditsSettlementService,
)
from app.services.billing.price_config_service import PriceConfigService
from app.services.billing.stripe_refund_reconciliation_service import (
    StripeRefundReconciliationService,
)
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    AuthException,
    KnowhereException,
    StripeServiceException,
    SystemSettingMissingException,
    ValidationException,
)
from shared.core.logging import logger
from shared.services.billing import CreditsService

StripeEvent: TypeAlias = dict[str, Any]
StripeWebhookHandler: TypeAlias = Callable[
    [AsyncSession, StripeEvent], Awaitable[dict[str, object]]
]


class StripeWebhookService:
    def __init__(
        self,
        *,
        payment_record_repository: PaymentRecordRepository | None = None,
        price_config_service: PriceConfigService | None = None,
        credits_service: CreditsService | None = None,
        credits_settlement_service: StripeCreditsSettlementService | None = None,
        refund_reconciliation_service: StripeRefundReconciliationService | None = None,
    ) -> None:
        self._configure_stripe_api()
        self._payment_record_repository = (
            payment_record_repository or PaymentRecordRepository()
        )
        self._price_config_service = price_config_service or PriceConfigService()
        self._credits_service = credits_service or CreditsService()
        self._credits_settlement_service = (
            credits_settlement_service
            or StripeCreditsSettlementService(
                payment_record_repository=self._payment_record_repository,
                price_config_service=self._price_config_service,
                credits_service=self._credits_service,
            )
        )
        self._refund_reconciliation_service = (
            refund_reconciliation_service
            or StripeRefundReconciliationService(
                payment_record_repository=self._payment_record_repository,
                price_config_service=self._price_config_service,
                credits_service=self._credits_service,
            )
        )

    async def handle_webhook(
        self,
        db: AsyncSession,
        *,
        payload: bytes,
        sig_header: str,
    ) -> dict[str, object]:
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                settings.STRIPE_WEBHOOK_SECRET,
            )
            return await self._dispatch_event(db, event)
        except ValueError as exc:
            logger.error(f"Invalid payload: {exc}")
            raise ValidationException(
                user_message="Invalid webhook payload",
                violations=[
                    {"field": "payload", "description": "Webhook payload is malformed"}
                ],
            )
        except stripe.SignatureVerificationError as exc:
            logger.error(f"Invalid signature: {exc}")
            raise AuthException(
                user_message="Invalid webhook signature",
                internal_message=f"Webhook signature verification failed: {exc}",
            )

    async def _dispatch_event(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        event_type = str(event["type"])
        handler = self._event_handlers().get(event_type)
        if handler is None:
            return {"status": "ignored", "event_type": event_type}
        return await handler(db, event)

    def _event_handlers(self) -> dict[str, StripeWebhookHandler]:
        return {
            "checkout.session.completed": self._handle_checkout_completed,
            "payment_intent.succeeded": self._handle_payment_intent_succeeded,
            "invoice.payment_succeeded": self._handle_payment_succeeded,
            "customer.subscription.deleted": self._handle_subscription_deleted,
            "charge.refunded": self._handle_charge_refunded,
        }

    async def _handle_checkout_completed(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        return await self._credits_settlement_service.handle_checkout_completed(
            db,
            event=event,
        )

    async def _handle_payment_intent_succeeded(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        return await self._credits_settlement_service.handle_payment_intent_succeeded(
            db,
            event=event,
        )

    async def _handle_payment_succeeded(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        del db
        invoice = event["data"]["object"]
        subscription_id = invoice.get("subscription")

        if not subscription_id:
            logger.warning("Invoice is missing subscription ID")
            return {"status": "ignored", "message": "Missing subscription_id"}

        return {"status": "ignored", "message": "Subscription renewal not implemented"}

    async def _handle_subscription_deleted(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        del db
        subscription = event["data"]["object"]
        stripe_subscription_id = str(subscription["id"])

        try:
            logger.warning(
                "Local subscription record not found: "
                f"stripe_subscription_id={stripe_subscription_id}"
            )
            return {"status": "success", "subscription_id": stripe_subscription_id}
        except KnowhereException:
            raise
        except Exception as exc:
            logger.error(
                f"Failed to process customer.subscription.deleted: {exc}",
                exc_info=True,
            )
            raise StripeServiceException(
                internal_message=(
                    "Failed to process customer.subscription.deleted: "
                    f"{str(exc)}"
                ),
                original_exception=exc,
            )

    async def _handle_charge_refunded(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        return await self._refund_reconciliation_service.reconcile_charge_refund(
            db,
            event=event,
        )

    def _configure_stripe_api(self) -> None:
        if not settings.STRIPE_SECRET_KEY:
            raise SystemSettingMissingException(
                internal_message="Stripe API key not configured (STRIPE_SECRET_KEY)"
            )
        stripe.api_key = settings.STRIPE_SECRET_KEY
