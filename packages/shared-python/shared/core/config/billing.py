"""Billing configuration."""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BillingConfig(BaseSettings):
    """Billing configuration."""

    BILLING_ENABLED: bool = Field(
        default=True,
        description=(
            "Enable Stripe/credits billing. Disable for OSS self-hosted and "
            "API-only development flows."
        ),
    )

    # Stripe configuration.
    STRIPE_SECRET_KEY: Optional[str] = Field(
        default=None, description="Stripe secret key"
    )
    STRIPE_WEBHOOK_SECRET: Optional[str] = Field(
        default=None, description="Stripe webhook secret"
    )

    # Credits (Micro-Dollar System: $1.00 = 1,000,000 micro-credits)
    MICRO_DOLLARS_PER_PAGE: int = Field(
        default=15000, description="Micro dollars per page ($0.015 = 15000 micros)"
    )
    CREDITS_VALID_DAYS: int = Field(
        default=365, description="Credit validity period in days"
    )

    # Moesif configuration.
    MOESIF_APPLICATION_ID: str = Field(default="")

    # Subscription defaults.
    FREE_PLAN_INITIAL_CREDITS: int = Field(default=5)

    # S3 result-bucket configuration.
    S3_RESULTS_BUCKET: str = Field(default="")

    # Frontend callback URL used by Stripe Checkout.
    FRONTEND_URL: str = Field(
        default="http://localhost:3000",
        description="Frontend URL for Stripe Checkout success/cancel callbacks",
    )

    def validate_billing_config(self) -> bool:
        """Validate the optional billing configuration."""
        if self.STRIPE_SECRET_KEY:
            required_stripe_fields = [
                self.STRIPE_SECRET_KEY,
                self.STRIPE_WEBHOOK_SECRET,
            ]
            return all(field for field in required_stripe_fields)
        return True  # Stripe is optional in local or limited-feature setups.

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )
