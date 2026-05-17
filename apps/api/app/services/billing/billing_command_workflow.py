from __future__ import annotations

from app.services.billing.stripe_purchase_service import StripePurchaseService
from app.services.billing.stripe_webhook_service import StripeWebhookService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.billing import MicroDollar
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import StripeServiceException
from shared.models.database.user import User
from shared.models.schemas.billing import (
    BuyCreditsPackageRequest,
    BuyCreditsRequest,
    CheckoutSessionResponse,
    PaymentIntentResponse,
)


class BillingCommandWorkflow:
    async def buy_credits(
        self,
        *,
        request: BuyCreditsRequest,
        user_id: str,
    ) -> PaymentIntentResponse:
        stripe_purchase_service = StripePurchaseService()
        try:
            amount_cny = request.credits_amount * 0.02
            amount_cents = int(amount_cny * 100)
            payment_intent = await stripe_purchase_service.create_payment_intent(
                user_id=user_id,
                amount=amount_cents,
                credits_amount=MicroDollar.from_dollars(request.credits_amount).amount,
                currency="cny",
            )

            return PaymentIntentResponse(
                client_secret=payment_intent["client_secret"],
                payment_intent_id=payment_intent["payment_intent_id"],
            )
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to buy credits: {str(exc)}"
            )

    async def buy_credits_package(
        self,
        db: AsyncSession,
        *,
        request: BuyCreditsPackageRequest,
        user_id: str,
    ) -> CheckoutSessionResponse:
        stripe_purchase_service = StripePurchaseService()
        try:
            result = await db.execute(select(User.email).where(User.id == user_id))
            user_email = result.scalar_one_or_none()

            frontend_url = settings.FRONTEND_URL
            success_url = f"{frontend_url}/billing?success=true&type=credits_package"
            cancel_url = f"{frontend_url}/billing?canceled=true"

            checkout_url = await stripe_purchase_service.create_credits_package_checkout_session(
                db=db,
                user_id=user_id,
                price_id=request.price_id,
                success_url=success_url,
                cancel_url=cancel_url,
                quantity=request.quantity,
                email=user_email,
            )

            return CheckoutSessionResponse(checkout_url=checkout_url, session_id="")
        except Exception as exc:
            raise StripeServiceException(
                internal_message=(
                    "Failed to create credits package purchase: "
                    f"{str(exc)}"
                )
            )

    async def handle_stripe_webhook(
        self,
        db: AsyncSession,
        *,
        payload: bytes,
        stripe_signature: str | None,
    ) -> dict[str, object]:
        stripe_webhook_service = StripeWebhookService()
        try:
            if not stripe_signature:
                raise StripeServiceException(
                    internal_message="Missing stripe-signature header"
                )
            return await stripe_webhook_service.handle_webhook(
                db,
                payload=payload,
                sig_header=stripe_signature,
            )
        except Exception as exc:
            raise StripeServiceException(
                internal_message=f"Failed to handle webhook: {str(exc)}"
            )
