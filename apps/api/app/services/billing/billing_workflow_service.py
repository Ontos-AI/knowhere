from __future__ import annotations

from app.services.billing.billing_command_workflow import BillingCommandWorkflow
from app.services.billing.billing_read_model import BillingReadModel, ParseUsageResponse
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.billing import (
    BuyCreditsPackageRequest,
    BuyCreditsRequest,
    CheckoutSessionResponse,
    CreditsBalanceResponse,
    PaymentIntentResponse,
    TransactionHistoryResponse,
    UsageStatsResponse,
)

__all__ = ["BillingWorkflowService", "ParseUsageResponse"]


class BillingWorkflowService:
    def __init__(
        self,
        *,
        command_workflow: BillingCommandWorkflow | None = None,
        read_model: BillingReadModel | None = None,
    ) -> None:
        self._command_workflow = command_workflow or BillingCommandWorkflow()
        self._read_model = read_model or BillingReadModel()

    async def buy_credits(
        self,
        *,
        request: BuyCreditsRequest,
        user_id: str,
    ) -> PaymentIntentResponse:
        return await self._command_workflow.buy_credits(
            request=request,
            user_id=user_id,
        )

    async def get_credits_balance(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> CreditsBalanceResponse:
        return await self._read_model.get_credits_balance(db, user_id=user_id)

    async def get_usage_stats(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        period: str,
    ) -> UsageStatsResponse:
        return await self._read_model.get_usage_stats(
            db,
            user_id=user_id,
            period=period,
        )

    async def get_parse_usage_overview(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> ParseUsageResponse:
        return await self._read_model.get_parse_usage_overview(db, user_id=user_id)

    async def get_transaction_history(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        limit: int,
    ) -> list[TransactionHistoryResponse]:
        return await self._read_model.get_transaction_history(
            db,
            user_id=user_id,
            limit=limit,
        )

    async def get_price_configs(
        self,
        db: AsyncSession,
        *,
        product_type: str | None,
    ) -> dict[str, list[dict[str, object]]]:
        return await self._read_model.get_price_configs(
            db,
            product_type=product_type,
        )

    async def buy_credits_package(
        self,
        db: AsyncSession,
        *,
        request: BuyCreditsPackageRequest,
        user_id: str,
    ) -> CheckoutSessionResponse:
        return await self._command_workflow.buy_credits_package(
            db,
            request=request,
            user_id=user_id,
        )

    async def handle_stripe_webhook(
        self,
        db: AsyncSession,
        *,
        payload: bytes,
        stripe_signature: str | None,
    ) -> dict[str, object]:
        return await self._command_workflow.handle_stripe_webhook(
            db,
            payload=payload,
            stripe_signature=stripe_signature,
        )
