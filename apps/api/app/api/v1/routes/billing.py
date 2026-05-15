"""Billing API routes."""

from typing import Optional

from app.services.billing.billing_app_service import (
    ParseUsageResponse,
    buy_credits_for_user,
    buy_credits_package_for_user,
    get_credits_balance_for_user,
    get_parse_usage_overview_for_user,
    get_price_configs_payload,
    get_transaction_history_for_user,
    get_usage_stats_for_user,
    handle_stripe_webhook,
)
from app.services.rate_limit.dependencies import CurrentUser, with_current_user
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.billing import (
    BuyCreditsPackageRequest,
    BuyCreditsRequest,
    CheckoutSessionResponse,
    CreditsBalanceResponse,
    PaymentIntentResponse,
    UsageStatsResponse,
)

router = APIRouter(tags=["Billing"])


@router.post("/buy-credits", summary="Buy Credits", response_model=PaymentIntentResponse)
async def buy_credits(
    request: BuyCreditsRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaymentIntentResponse:
    return await buy_credits_for_user(
        request=request,
        user_id=current_user.user_id,
    )


@router.get(
    "/credits",
    summary="Get Credits Balance",
    response_model=CreditsBalanceResponse,
)
async def get_credits_balance(
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreditsBalanceResponse:
    return await get_credits_balance_for_user(db, user_id=current_user.user_id)


@router.get(
    "/usage",
    summary="Get Usage Statistics",
    response_model=UsageStatsResponse,
)
async def get_usage_stats(
    period: str = "month",
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageStatsResponse:
    return await get_usage_stats_for_user(
        db,
        user_id=current_user.user_id,
        period=period,
    )


@router.get(
    "/parse-usage",
    summary="Get Parse Usage Overview",
    response_model=ParseUsageResponse,
)
async def parse_usage_overview(
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParseUsageResponse:
    return await get_parse_usage_overview_for_user(
        db,
        user_id=current_user.user_id,
    )


@router.get("/history", summary="Get Transaction History")
async def get_transaction_history(
    limit: int = 50,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_transaction_history_for_user(
        db,
        user_id=current_user.user_id,
        limit=limit,
    )


@router.get("/price-configs", summary="Get Price Configurations")
async def get_price_configs(
    product_type: Optional[str] = Query(
        None,
        description="Product type: subscription or credits_package",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[dict]]:
    return await get_price_configs_payload(db, product_type=product_type)


@router.post(
    "/buy-credits-package",
    summary="Buy Credits Package by Price ID",
    response_model=CheckoutSessionResponse,
)
async def buy_credits_package(
    request: BuyCreditsPackageRequest,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckoutSessionResponse:
    return await buy_credits_package_for_user(
        db,
        request=request,
        user_id=current_user.user_id,
    )


@router.post("/webhook", summary="Stripe Webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    return await handle_stripe_webhook(
        db,
        payload=await request.body(),
        stripe_signature=request.headers.get("stripe-signature"),
    )
