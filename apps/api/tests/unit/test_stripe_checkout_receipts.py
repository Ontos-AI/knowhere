"""Checkout sessions should request Stripe invoice/receipt emails."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

configure_import_environment()
ensure_import_paths()

_API_ROOT = str(Path(__file__).resolve().parents[2])


def _prioritize_api_import_root() -> None:
    ensure_import_paths()
    if _API_ROOT in sys.path:
        sys.path.remove(_API_ROOT)
    sys.path.insert(0, _API_ROOT)


def _is_api_app_module(module: ModuleType | None) -> bool:
    if module is None:
        return False
    module_file = getattr(module, "__file__", None)
    if isinstance(module_file, str) and module_file.startswith(_API_ROOT):
        return True
    module_paths = getattr(module, "__path__", ())
    try:
        return any(str(path).startswith(_API_ROOT) for path in module_paths)
    except KeyError:
        return False


def _drop_non_api_app_modules() -> None:
    for module_name in sorted(sys.modules, key=len, reverse=True):
        if module_name != "app" and not module_name.startswith("app."):
            continue
        if not _is_api_app_module(sys.modules.get(module_name)):
            sys.modules.pop(module_name, None)


def _drop_api_app_modules() -> None:
    for module_name in sorted(sys.modules, key=len, reverse=True):
        if module_name != "app" and not module_name.startswith("app."):
            continue
        if _is_api_app_module(sys.modules.get(module_name)):
            sys.modules.pop(module_name, None)


def _load_purchase_service_module() -> ModuleType:
    _prioritize_api_import_root()
    _drop_non_api_app_modules()
    from app.services.billing import stripe_purchase_service

    return stripe_purchase_service


@pytest.fixture(autouse=True)
def _clear_api_app_modules_after_unit_test():
    yield
    _drop_api_app_modules()


@pytest.mark.asyncio
async def test_credits_checkout_requests_invoice_and_receipt_email() -> None:
    stripe_purchase_service = _load_purchase_service_module()
    StripePurchaseService = stripe_purchase_service.StripePurchaseService

    price_config = SimpleNamespace(
        is_credits_package=lambda: True,
        credits_amount=500,
    )
    user_balance = SimpleNamespace(stripe_customer_id="cus_test")
    created_session = SimpleNamespace(url="https://checkout.stripe.test/session")

    with (
        patch.object(StripePurchaseService, "_configure_stripe_api"),
        patch.object(
            stripe_purchase_service.stripe.checkout.Session,
            "create",
            return_value=created_session,
        ) as create_session,
    ):
        service = StripePurchaseService(
            price_config_service=MagicMock(
                get_price_config=AsyncMock(return_value=price_config)
            ),
            credits_repository=MagicMock(
                get_user_balance=AsyncMock(return_value=user_balance)
            ),
            credits_service=MagicMock(ensure_user_initialized=AsyncMock()),
        )
        checkout_url = await service.create_credits_package_checkout_session(
            AsyncMock(),
            user_id="user-1",
            price_id="price_credits",
            success_url="https://app.test/billing?success=true",
            cancel_url="https://app.test/billing?canceled=true",
            quantity=2,
            email="buyer@example.com",
        )

    assert checkout_url == "https://checkout.stripe.test/session"
    create_session.assert_called_once()
    session_params = create_session.call_args.kwargs
    assert session_params["invoice_creation"] == {"enabled": True}
    assert session_params["payment_intent_data"]["receipt_email"] == "buyer@example.com"
    assert session_params["customer"] == "cus_test"
    assert session_params["mode"] == "payment"


@pytest.mark.asyncio
async def test_credits_checkout_omits_receipt_email_when_email_missing() -> None:
    stripe_purchase_service = _load_purchase_service_module()
    StripePurchaseService = stripe_purchase_service.StripePurchaseService

    price_config = SimpleNamespace(
        is_credits_package=lambda: True,
        credits_amount=500,
    )
    user_balance = SimpleNamespace(stripe_customer_id="cus_test")
    created_session = SimpleNamespace(url="https://checkout.stripe.test/session")

    with (
        patch.object(StripePurchaseService, "_configure_stripe_api"),
        patch.object(
            stripe_purchase_service.stripe.checkout.Session,
            "create",
            return_value=created_session,
        ) as create_session,
    ):
        service = StripePurchaseService(
            price_config_service=MagicMock(
                get_price_config=AsyncMock(return_value=price_config)
            ),
            credits_repository=MagicMock(
                get_user_balance=AsyncMock(return_value=user_balance)
            ),
            credits_service=MagicMock(ensure_user_initialized=AsyncMock()),
        )
        await service.create_credits_package_checkout_session(
            AsyncMock(),
            user_id="user-1",
            price_id="price_credits",
            success_url="https://app.test/billing?success=true",
            cancel_url="https://app.test/billing?canceled=true",
            quantity=1,
            email=None,
        )

    session_params = create_session.call_args.kwargs
    assert session_params["invoice_creation"] == {"enabled": True}
    assert "receipt_email" not in session_params["payment_intent_data"]
