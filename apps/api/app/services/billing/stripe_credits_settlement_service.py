from __future__ import annotations

from typing import Any

from app.repositories.payment_record_repository import PaymentRecordRepository
from app.services.billing.price_config_service import PriceConfigService
from app.services.rate_limit.tier_service import TierService
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    KnowhereException,
    StripeServiceException,
)
from shared.models.database.payment_record import PaymentRecord
from shared.services.billing import CreditsService
from shared.utils.utc_now import utc_now_naive


class StripeCreditsSettlementService:
    def __init__(
        self,
        *,
        payment_record_repository: PaymentRecordRepository | None = None,
        price_config_service: PriceConfigService | None = None,
        credits_service: CreditsService | None = None,
    ) -> None:
        self._payment_record_repository = (
            payment_record_repository or PaymentRecordRepository()
        )
        self._price_config_service = price_config_service or PriceConfigService()
        self._credits_service = credits_service or CreditsService()

    async def handle_checkout_completed(
        self,
        db: AsyncSession,
        *,
        event: dict[str, Any],
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

    async def handle_payment_intent_succeeded(
        self,
        db: AsyncSession,
        *,
        event: dict[str, Any],
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
