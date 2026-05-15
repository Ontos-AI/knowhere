from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

import stripe
from app.repositories.payment_record_repository import PaymentRecordRepository
from app.services.billing.price_config_service import PriceConfigService
from app.services.billing.stripe_refund_reconciliation_service import (
    StripeRefundReconciliationService,
)
from app.services.rate_limit.tier_service import TierService
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
from shared.models.database.payment_record import PaymentRecord
from shared.services.billing import CreditsService
from shared.utils.utc_now import utc_now_naive

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
        refund_reconciliation_service: StripeRefundReconciliationService | None = None,
    ) -> None:
        self._configure_stripe_api()
        self._payment_record_repository = (
            payment_record_repository or PaymentRecordRepository()
        )
        self._price_config_service = price_config_service or PriceConfigService()
        self._credits_service = credits_service or CreditsService()
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
        session = event["data"]["object"]
        session_id = str(session["id"])
        mode = session.get("mode")
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")
        payment_type = metadata.get("type")
        quantity = int(metadata.get("quantity", 1))

        if not user_id:
            logger.warning(
                f"Checkout session {session_id} is missing user_id metadata; likely a test event, skipping"
            )
            return {
                "status": "ignored",
                "message": "Missing user_id metadata (likely test event)",
                "checkout_session_id": session_id,
                "event_type": "checkout.session.completed",
            }

        if await self._payment_record_repository.is_processed(
            db,
            checkout_session_id=session_id,
        ):
            logger.info(f"Checkout session {session_id} already processed, skipping")
            return {
                "status": "ignored",
                "message": "Already processed",
                "checkout_session_id": session_id,
            }

        payment_metadata = {
            "session_id": session_id,
            "stripe_session": session,
        }
        payment_record = PaymentRecord(
            checkout_session_id=session_id,
            user_id=user_id,
            payment_type=payment_type or "unknown",
            amount_cents=session.get("amount_total", 0),
            currency=session.get("currency", "cny").upper(),
            status="pending",
            extra_metadata=payment_metadata,
        )
        db.add(payment_record)
        await db.flush()

        try:
            if mode != "payment" or payment_type != "credits_package":
                logger.warning(f"Unknown payment type: mode={mode}, type={payment_type}")
                return {"status": "ignored", "message": "Unknown payment type"}

            price_id = metadata.get("price_id")
            if not price_id:
                logger.error(f"Incomplete Credits pack info: price_id={price_id}")
                return {"status": "error", "message": "Missing price_id"}

            price_config = await self._price_config_service.get_price_config(db, price_id)
            configured_credits_amount = price_config.credits_amount
            if configured_credits_amount is None:
                logger.error(
                    f"Credits amount is not configured for price ID {price_id}"
                )
                return {
                    "status": "error",
                    "message": "Credits amount not configured",
                }
            credits_amount = configured_credits_amount * quantity

            product_description = f"Credits pack - {credits_amount} Credits"
            if price_config.extra_metadata and price_config.extra_metadata.get(
                "description"
            ):
                product_description = str(
                    price_config.extra_metadata.get("description")
                )

            payment_record.extra_metadata = {
                **payment_metadata,
                "product_description": product_description,
                "price_id": price_id,
                "credits_amount": credits_amount,
                "product_metadata": price_config.extra_metadata or {},
            }
            await self._credits_service.add_credits(
                session=db,
                user_id=user_id,
                amount=credits_amount,
                reason=f"Purchase credits pack: {product_description}",
                stripe_payment_id=session.get("payment_intent"),
            )
            payment_record.status = "succeeded"
            payment_record.credits_amount = credits_amount
            payment_record.processed_at = utc_now_naive()

            await TierService.refresh_tier(user_id, db)
            await db.commit()
            await db.refresh(payment_record)

            logger.info(
                f"Credits pack purchase succeeded: user_id={user_id}, credits={credits_amount}, price_id={price_id}"
            )
            return {
                "status": "success",
                "event_type": "checkout.session.completed",
                "user_id": user_id,
                "credits_amount": credits_amount,
                "payment_type": "credits_package",
            }
        except KnowhereException:
            raise
        except Exception as exc:
            logger.error(
                f"Failed to process checkout.session.completed: {exc}",
                exc_info=True,
            )
            payment_record.status = "failed"
            payment_record.extra_metadata = {
                **(payment_record.extra_metadata or {}),
                "error": str(exc),
            }
            await db.commit()
            raise StripeServiceException(
                internal_message=(
                    "Failed to process checkout.session.completed: "
                    f"{str(exc)}"
                ),
                original_exception=exc,
            )

    async def _handle_payment_intent_succeeded(
        self,
        db: AsyncSession,
        event: StripeEvent,
    ) -> dict[str, object]:
        payment_intent = event["data"]["object"]
        payment_intent_id = str(payment_intent["id"])
        metadata = payment_intent.get("metadata", {})
        user_id = metadata.get("user_id")
        payment_type = metadata.get("type")

        if payment_type != "credits":
            logger.info(
                f"PaymentIntent {payment_intent_id} is not a Credits payment, skipping"
            )
            return {"status": "ignored", "payment_intent_id": payment_intent_id}

        if not user_id:
            logger.warning(
                f"PaymentIntent {payment_intent_id} is missing user_id metadata; likely a test event, skipping"
            )
            return {
                "status": "ignored",
                "message": "Missing user_id metadata (likely test event)",
                "payment_intent_id": payment_intent_id,
            }

        if await self._payment_record_repository.is_processed(
            db,
            payment_intent_id=payment_intent_id,
        ):
            logger.info(
                f"PaymentIntent {payment_intent_id} already processed, skipping"
            )
            return {
                "status": "ignored",
                "message": "Already processed",
                "payment_intent_id": payment_intent_id,
            }

        payment_metadata = {
            "payment_intent_id": payment_intent_id,
            "stripe_payment_intent": payment_intent,
        }
        payment_record = PaymentRecord(
            payment_intent_id=payment_intent_id,
            user_id=user_id,
            payment_type="credits_package",
            amount_cents=payment_intent.get("amount", 0),
            currency=payment_intent.get("currency", "cny").upper(),
            status="pending",
            extra_metadata=payment_metadata,
        )
        db.add(payment_record)
        await db.flush()

        try:
            credits_amount_str = metadata.get("credits_amount")
            if not credits_amount_str:
                logger.error(
                    f"PaymentIntent {payment_intent_id} is missing credits_amount"
                )
                payment_record.status = "failed"
                payment_record.extra_metadata = {
                    **(payment_record.extra_metadata or {}),
                    "error": "Missing credits_amount",
                }
                await db.commit()
                return {"status": "error", "message": "Missing credits_amount"}

            credits_amount = int(credits_amount_str)
            payment_record.extra_metadata = {
                **payment_metadata,
                "product_description": f"Credits package - {credits_amount} Credits",
                "credits_amount": credits_amount,
                "payment_method": "payment_intent",
            }
            await self._credits_service.add_credits(
                session=db,
                user_id=user_id,
                amount=credits_amount,
                reason=f"buy credits - {credits_amount} Credits",
                stripe_payment_id=payment_intent_id,
            )
            payment_record.status = "succeeded"
            payment_record.credits_amount = credits_amount
            payment_record.processed_at = utc_now_naive()

            await TierService.refresh_tier(user_id, db)
            await db.commit()
            await db.refresh(payment_record)

            logger.info(
                f"buy credits success: user_id={user_id}, credits={credits_amount}, payment_intent_id={payment_intent_id}"
            )
            return {
                "status": "success",
                "event_type": "payment_intent.succeeded",
                "user_id": user_id,
                "credits_amount": credits_amount,
                "payment_type": "credits_package",
            }
        except Exception as exc:
            logger.error(f"Failed to process Credits purchase: {exc}", exc_info=True)
            payment_record.status = "failed"
            payment_record.extra_metadata = {
                **(payment_record.extra_metadata or {}),
                "error": str(exc),
            }
            await db.commit()
            raise StripeServiceException(
                internal_message=f"Failed to process Credits purchase: {str(exc)}",
                original_exception=exc,
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
