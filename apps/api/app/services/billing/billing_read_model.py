from __future__ import annotations

from typing import Optional

from app.services.billing.price_config_service import PriceConfigService
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.billing import MicroDollar
from shared.core.exceptions.domain_exceptions import StripeServiceException
from shared.models.database.credits_transaction import CreditsTransaction
from shared.models.database.job import Job
from shared.models.database.stripe_price_config import StripePriceConfig
from shared.models.schemas.billing import (
    CreditsBalanceResponse,
    TransactionHistoryResponse,
    UsageStatsResponse,
)
from shared.services.billing import CreditsService


class ParseUsageResponse(BaseModel):
    request_total: int
    mom_growth: float
    credits_used: float
    estimated_amount: Optional[float]
    success_rate: float
    avg_processing_time: float


class BillingReadModel:
    def __init__(
        self,
        *,
        price_config_service: PriceConfigService | None = None,
        credits_service: CreditsService | None = None,
    ) -> None:
        self._price_config_service = price_config_service or PriceConfigService()
        self._credits_service = credits_service or CreditsService()

    async def get_credits_balance(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> CreditsBalanceResponse:
        try:
            await self._credits_service.ensure_user_initialized(db, user_id)
            await db.commit()

            balance_micro_dollar = await self._credits_service.get_balance(db, user_id)
            return CreditsBalanceResponse(
                credits_balance=MicroDollar(balance_micro_dollar).to_credit()
            )
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to get credits balance: {str(exc)}"
            )

    async def get_usage_stats(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        period: str,
    ) -> UsageStatsResponse:
        try:
            stats = await self._credits_service.get_usage_stats(db, user_id, period)
            return UsageStatsResponse(
                period=stats["period"],
                total_credits_used=MicroDollar(stats["total_used"]).to_credit(),
                api_calls_count=stats["transaction_count"],
                success_rate=95.0,
                average_response_time=stats.get("avg_response_time", 0),
                top_endpoints=[],
            )
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to get usage statistics: {str(exc)}"
            )

    async def get_parse_usage_overview(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> ParseUsageResponse:
        try:
            total_micro_credits_used = await self._load_total_parse_micro_credits_used(
                db,
                user_id=user_id,
            )
            success_rate, avg_processing_time = await self._load_parse_job_usage_stats(
                db,
                user_id=user_id,
            )
            estimated_amount = await self._estimate_parse_usage_amount(
                db,
                total_micro_credits_used=total_micro_credits_used,
            )

            return ParseUsageResponse(
                request_total=0,
                mom_growth=0.0,
                credits_used=MicroDollar(total_micro_credits_used).to_credit() or 0,
                estimated_amount=estimated_amount,
                success_rate=round(success_rate, 2),
                avg_processing_time=avg_processing_time,
            )
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to get parse usage overview: {str(exc)}"
            )

    async def get_transaction_history(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        limit: int,
    ) -> list[TransactionHistoryResponse]:
        try:
            transactions = await self._credits_service.get_transaction_history(
                db,
                user_id,
                limit,
            )
            return [
                TransactionHistoryResponse(
                    id=transaction.id,
                    credits_amount=MicroDollar(transaction.credits_amount).to_credit(),
                    transaction_type=transaction.transaction_type,
                    description=transaction.description,
                    created_at=transaction.created_at,
                )
                for transaction in transactions
            ]
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to get transaction history: {str(exc)}"
            )

    async def get_price_configs(
        self,
        db: AsyncSession,
        *,
        product_type: str | None,
    ) -> dict[str, list[dict[str, object]]]:
        try:
            if product_type == "subscription":
                configs = await self._price_config_service.repository.get_all_active(db)
                return {
                    "subscriptions": [
                        _subscription_config_payload(config)
                        for config in configs
                        if config.product_type == "subscription"
                    ],
                    "credits_packages": [],
                }

            if product_type == "credits_package":
                credits_configs = await self._price_config_service.get_all_credits_packages(
                    db
                )
                return {
                    "subscriptions": [],
                    "credits_packages": [
                        _credits_package_config_payload(config)
                        for config in credits_configs
                    ],
                }

            configs = await self._price_config_service.repository.get_all_active(db)
            return {
                "subscriptions": [
                    _subscription_config_payload(config)
                    for config in configs
                    if config.product_type == "subscription"
                ],
                "credits_packages": [
                    _credits_package_config_payload(config)
                    for config in configs
                    if config.product_type == "credits_package"
                ],
            }
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to get price configurations: {str(exc)}"
            )

    async def _load_total_parse_micro_credits_used(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> int:
        credits_row = await db.execute(
            select(func.coalesce(func.sum(CreditsTransaction.credits_amount), 0))
            .where(CreditsTransaction.user_id == user_id)
            .where(CreditsTransaction.transaction_type.in_(["usage", "refund"]))
        )
        return int(abs(credits_row.scalar_one() or 0))

    async def _load_parse_job_usage_stats(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> tuple[float, float]:
        job_row = await db.execute(
            select(
                func.count().filter(Job.status == "done").label("done_cnt"),
                func.count()
                .filter(Job.status.in_(["done", "failed"]))
                .label("terminal_cnt"),
                func.avg(func.extract("epoch", Job.updated_at - Job.created_at))
                .filter(Job.status.in_(["done", "failed"]))
                .label("avg_secs"),
            ).where(Job.user_id == user_id)
        )
        job_stats = job_row.first() or (0, 0, 0.0)
        done_count = getattr(job_stats, "done_cnt", 0) or 0
        terminal_count = getattr(job_stats, "terminal_cnt", 0) or 0
        success_rate = (
            done_count / terminal_count * 100 if terminal_count > 0 else 0.0
        )
        avg_processing_time = round(
            float(getattr(job_stats, "avg_secs", 0.0) or 0.0),
            2,
        )
        return success_rate, avg_processing_time

    async def _estimate_parse_usage_amount(
        self,
        db: AsyncSession,
        *,
        total_micro_credits_used: int,
    ) -> float | None:
        price_row = await db.execute(
            select(StripePriceConfig)
            .where(StripePriceConfig.product_type == "credits_package")
            .where(StripePriceConfig.is_active.is_(True))
            .order_by(StripePriceConfig.created_at)
            .limit(1)
        )
        price_cfg = price_row.scalar_one_or_none()
        if not price_cfg or not price_cfg.credits_amount or price_cfg.credits_amount <= 0:
            return None

        return round(
            price_cfg.amount_cents
            * total_micro_credits_used
            / (100 * price_cfg.credits_amount),
            4,
        )


def _subscription_config_payload(config: StripePriceConfig) -> dict[str, object]:
    metadata = config.extra_metadata or {}
    return {
        "id": config.plan_id,
        "plan_id": config.plan_id,
        "price_id": config.price_id,
        "name": metadata.get("display_name", config.plan_id.upper()),
        "description": metadata.get("description", ""),
        "features": metadata.get("features", []),
        "popular": metadata.get("frontend_config", {}).get("popular", False),
        "amount_cents": config.amount_cents,
        "currency": config.currency,
        "metadata": metadata,
    }


def _credits_package_config_payload(config: StripePriceConfig) -> dict[str, object]:
    metadata = config.extra_metadata or {}
    credit_amount = MicroDollar(config.credits_amount).to_credit()
    return {
        "id": config.plan_id,
        "plan_id": config.plan_id,
        "price_id": config.price_id,
        "name": metadata.get("display_name", f"{credit_amount} Credits"),
        "description": metadata.get("description", ""),
        "credits_amount": credit_amount,
        "amount_cents": config.amount_cents,
        "currency": config.currency,
        "metadata": metadata,
    }
