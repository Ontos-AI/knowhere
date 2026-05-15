from __future__ import annotations

from typing import Any

import stripe
from app.services.billing.price_config_service import PriceConfigService
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    StripeServiceException,
    SystemSettingMissingException,
    ValidationException,
)
from shared.core.logging import logger
from shared.repositories.credits_repository import CreditsRepository
from shared.services.billing import CreditsService


class StripePurchaseService:
    def __init__(
        self,
        *,
        price_config_service: PriceConfigService | None = None,
        credits_repository: CreditsRepository | None = None,
        credits_service: CreditsService | None = None,
    ) -> None:
        self._configure_stripe_api()
        self._price_config_service = price_config_service or PriceConfigService()
        self._credits_repository = credits_repository or CreditsRepository()
        self._credits_service = credits_service or CreditsService()

    async def create_credits_package_checkout_session(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        quantity: int,
        email: str | None = None,
    ) -> str:
        try:
            config = await self._price_config_service.get_price_config(db, price_id)
            if not config.is_credits_package():
                raise ValidationException(
                    user_message="Invalid price configuration",
                    violations=[
                        {
                            "field": "price_id",
                            "description": f"Price ID {price_id} is not a credits package",
                        }
                    ],
                )

            customer_id = await self._resolve_customer_id(
                db,
                user_id=user_id,
                email=email,
            )
            metadata = {
                "user_id": str(user_id),
                "price_id": str(price_id),
                "type": "credits_package",
                "credits_amount": (
                    str(config.credits_amount) if config.credits_amount else None
                ),
                "quantity": str(quantity),
            }
            session_params: dict[str, Any] = {
                "customer": customer_id,
                "customer_update": {"address": "auto"},
                "client_reference_id": str(user_id),
                "line_items": [
                    {
                        "price": price_id,
                        "quantity": quantity,
                    }
                ],
                "mode": "payment",
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata,
                "payment_intent_data": {"metadata": metadata},
                "allow_promotion_codes": True,
                "adaptive_pricing": {"enabled": False},
                "billing_address_collection": "required",
            }
            session = stripe.checkout.Session.create(**session_params)
            await db.commit()
            return str(session.url or "")
        except stripe.StripeError as exc:
            logger.error(f"Stripe credits checkout session failed: {exc}")
            raise StripeServiceException(
                internal_message=f"Stripe credits checkout session failed: {exc}"
            )

    async def create_payment_intent(
        self,
        *,
        user_id: str,
        amount: int,
        credits_amount: int,
        currency: str = "usd",
    ) -> dict[str, str]:
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency=currency,
                automatic_payment_methods={"enabled": True},
                metadata={
                    "user_id": user_id,
                    "type": "credits",
                    "credits_amount": str(credits_amount),
                },
            )
            return {
                "client_secret": str(intent.client_secret or ""),
                "payment_intent_id": str(intent.id),
            }
        except stripe.StripeError as exc:
            logger.error(f"Failed to create payment intent: {exc}")
            raise StripeServiceException(
                internal_message=f"Stripe payment intent creation failed: {exc}"
            )

    async def _resolve_customer_id(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        email: str | None,
    ) -> str:
        await self._credits_service.ensure_user_initialized(db, user_id)
        user_balance = await self._credits_repository.get_user_balance(db, user_id)
        if not user_balance:
            raise ValidationException(
                user_message="Failed to initialize user balance",
                violations=[
                    {
                        "field": "user_id",
                        "description": f"Failed to initialize user balance for {user_id}",
                    }
                ],
            )

        customer_id = user_balance.stripe_customer_id
        if customer_id:
            return customer_id

        customer_id = self._find_existing_customer_id(email=email)
        if customer_id is None:
            customer_id = self._create_customer(user_id=user_id, email=email)

        user_balance.stripe_customer_id = customer_id
        return customer_id

    def _find_existing_customer_id(
        self,
        *,
        email: str | None,
    ) -> str | None:
        if not email:
            return None

        existing_customers = stripe.Customer.list(email=email, limit=1)
        if existing_customers.data:
            return str(existing_customers.data[0].id)
        return None

    def _create_customer(
        self,
        *,
        user_id: str,
        email: str | None,
    ) -> str:
        if not email:
            raise ValidationException(
                user_message="Email required for first-time payment",
                violations=[
                    {
                        "field": "email",
                        "description": "Email is required to create a billing profile",
                    }
                ],
            )

        customer = stripe.Customer.create(
            email=email,
            metadata={"user_id": str(user_id)},
        )
        return str(customer.id)

    def _configure_stripe_api(self) -> None:
        if not settings.STRIPE_SECRET_KEY:
            raise SystemSettingMissingException(
                internal_message="Stripe API key not configured (STRIPE_SECRET_KEY)"
            )
        stripe.api_key = settings.STRIPE_SECRET_KEY
