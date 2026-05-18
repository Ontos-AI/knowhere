from __future__ import annotations

from typing import Any
from uuid import UUID

from app.repositories.payment_record_repository import PaymentRecordRepository
from app.services.billing.price_config_service import PriceConfigService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.logging import logger
from shared.models.database.payment_record import PaymentRecord
from shared.services.billing import CreditsService
from shared.core.time import utc_now_naive


class StripeRefundReconciliationService:
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

    async def reconcile_charge_refund(
        self,
        db: AsyncSession,
        *,
        event: dict[str, Any],
    ) -> dict[str, object]:
        charge = event["data"]["object"]
        charge_id = charge.get("id")
        refund_items = (charge.get("refunds", {}) or {}).get("data", []) or []
        latest_refund = refund_items[-1] if refund_items else None

        payment_intent_id = charge.get("payment_intent")
        refund_id = latest_refund.get("id") if latest_refund else None
        currency = (charge.get("currency") or "cny").upper()
        idempotency_key = refund_id or f"{charge_id}-refund"

        original_record = None
        if payment_intent_id:
            original_record = await self._payment_record_repository.get_by_payment_intent_id(
                db,
                payment_intent_id,
            )

        metadata = charge.get("metadata") or {}
        user_id = metadata.get("user_id") or (
            getattr(original_record, "user_id", None)
        )
        payment_type = (
            metadata.get("type")
            or getattr(original_record, "payment_type", None)
            or "refund"
        )

        if not user_id:
            logger.error(
                f"Refund event is missing user_id; cannot record refund: charge_id={charge_id}"
            )
            return {
                "status": "error",
                "message": "Missing user_id for refund",
                "event_type": "charge.refunded",
            }

        normalized_user_id = self._normalize_user_id(user_id)
        if normalized_user_id is None:
            logger.error(f"Invalid user_id format: {user_id}")
            return {
                "status": "error",
                "message": "Invalid user_id format",
                "event_type": "charge.refunded",
            }

        user_id_str = str(normalized_user_id)
        total_refund_amount_cents = charge.get("amount_refunded") or 0
        origin_total_refund_amount_cents = await self._load_recorded_refund_amount(
            db,
            payment_intent_id=idempotency_key,
            user_id=normalized_user_id,
        )

        refund_amount_cents = (
            total_refund_amount_cents - origin_total_refund_amount_cents
        )
        if refund_amount_cents <= 0:
            logger.info(
                f"Refund already processed, skipping: charge_id={charge_id}, refund_id={refund_id}"
            )
            return {
                "status": "success",
                "event_type": "charge.refunded",
                "message": "Already processed",
                "user_id": normalized_user_id,
                "refund_id": refund_id,
            }

        credits_refunded = await self._calculate_refunded_credits(
            db,
            metadata=metadata,
            original_record=original_record,
            refund_amount_cents=refund_amount_cents,
        )

        if credits_refunded is not None and credits_refunded < 0:
            await self._credits_service.add_credits(
                session=db,
                user_id=user_id_str,
                amount=credits_refunded,
                reason="Refund adjustment",
                transaction_type="refund",
                transaction_metadata={"refund_id": refund_id, "charge_id": charge_id},
            )

        refund_metadata = {
            "refund_id": refund_id,
            "charge_id": charge_id,
            "original_payment_intent_id": payment_intent_id,
            "original_payment_record_id": getattr(original_record, "id", None),
            "reason": (latest_refund or {}).get("reason"),
            "balance_transaction": (latest_refund or {}).get("balance_transaction"),
        }
        refund_record = PaymentRecord(
            payment_intent_id=idempotency_key,
            user_id=normalized_user_id,
            payment_type=payment_type,
            amount_cents=-abs(refund_amount_cents),
            currency=currency,
            status="succeeded",
            credits_amount=credits_refunded,
            plan_id=getattr(original_record, "plan_id", None),
            stripe_subscription_id=getattr(
                original_record,
                "stripe_subscription_id",
                None,
            ),
            processed_at=utc_now_naive(),
            extra_metadata=refund_metadata,
        )
        db.add(refund_record)
        await db.commit()
        await db.refresh(refund_record)

        logger.info(
            f"Refund record created: user_id={normalized_user_id}, amount_cents={refund_record.amount_cents}, "
            f"refund_id={refund_id}, charge_id={charge_id}"
        )
        return {
            "status": "success",
            "event_type": "charge.refunded",
            "user_id": normalized_user_id,
            "refund_amount_cents": abs(refund_amount_cents),
            "payment_intent_id": payment_intent_id,
            "refund_id": refund_id,
        }

    async def _load_recorded_refund_amount(
        self,
        db: AsyncSession,
        *,
        payment_intent_id: str,
        user_id: UUID,
    ) -> int:
        result = await db.execute(
            select(func.sum(PaymentRecord.amount_cents))
            .where(PaymentRecord.payment_intent_id == payment_intent_id)
            .where(PaymentRecord.user_id == user_id)
            .where(PaymentRecord.amount_cents < 0)
        )
        return int(abs(result.scalar() or 0))

    async def _calculate_refunded_credits(
        self,
        db: AsyncSession,
        *,
        metadata: dict[str, Any],
        original_record: PaymentRecord | None,
        refund_amount_cents: int,
    ) -> int | None:
        credits_refunded: int | None = None
        price_id = metadata.get("price_id") or (
            getattr(original_record, "extra_metadata", {}) or {}
        ).get("price_id")
        if price_id:
            try:
                price_cfg = await self._price_config_service.get_price_config(
                    db,
                    price_id,
                )
                if price_cfg and price_cfg.amount_cents and price_cfg.credits_amount:
                    credits_refunded = -int(
                        price_cfg.credits_amount
                        * abs(refund_amount_cents)
                        / abs(price_cfg.amount_cents)
                    )
            except Exception as exc:
                logger.warning(
                    f"Failed to calculate refunded Credits, price_id={price_id}: {exc}"
                )
                credits_refunded = None

        if (
            credits_refunded is None
            and original_record
            and original_record.credits_amount
            and original_record.amount_cents
        ):
            credits_refunded = -int(
                abs(original_record.credits_amount)
                * abs(refund_amount_cents)
                / abs(original_record.amount_cents)
            )

        return credits_refunded

    def _normalize_user_id(
        self,
        user_id: str | UUID,
    ) -> UUID | None:
        if isinstance(user_id, UUID):
            return user_id

        try:
            return UUID(user_id)
        except ValueError:
            return None
