"""Admin utility to add credits and optionally set a user's tier."""
# ruff: noqa: E402

import argparse
import asyncio
import os
import sys
from typing import cast
from uuid import uuid4


def _bootstrap_python_path() -> None:
    """Allow running the script directly without preconfigured PYTHONPATH."""
    current_dir = os.path.dirname(os.path.abspath(__file__))  # apps/api/scripts
    api_root = os.path.dirname(current_dir)  # apps/api
    repo_root = os.path.dirname(os.path.dirname(api_root))  # repo root
    shared_python_path = os.path.join(repo_root, "packages", "shared-python")

    for path in (api_root, shared_python_path):
        if path not in sys.path:
            sys.path.insert(0, path)


_bootstrap_python_path()

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.rate_limit.tier_service import TierService
from shared.core.billing import MicroDollar
from shared.core.database import get_db_context
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.tier_limit import TierLimit
from shared.models.database.user import User
from shared.models.database.user_balance import UserBalance
from shared.services.billing import CreditsService
from shared.core.time import utc_now_naive


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add credits to a user and optionally update the stored tier.",
    )
    parser.add_argument("user_identifier", help="User email or user id")
    parser.add_argument(
        "amount",
        nargs="?",
        type=int,
        help="Credits amount in dollars to add",
    )
    parser.add_argument(
        "--tier",
        dest="tier_name",
        help="Tier name to set explicitly (must exist in tier_limits)",
    )
    return parser


async def _load_user(session: AsyncSession, user_identifier: str) -> User | None:
    result = await session.execute(
        select(User)
        .where(or_(User.email == user_identifier, User.id == user_identifier))
        .order_by(User.id.asc())
    )
    users = result.scalars().all()

    if not users:
        print(
            f"Error: user '{user_identifier}' not found (checked by email or user_id)."
        )
        return None

    if len(users) > 1:
        print(
            f"Warning: found {len(users)} users matching '{user_identifier}', "
            f"using first user id={users[0].id}."
        )

    return users[0]


async def _load_user_balance(session: AsyncSession, user_id: str) -> UserBalance | None:
    result = await session.execute(
        select(UserBalance).where(UserBalance.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def _load_available_tiers(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(TierLimit.tier_name).order_by(
            TierLimit.min_lifetime_amount_micro.asc(),
            TierLimit.tier_name.asc(),
        )
    )
    return list(result.scalars().all())


def _resolve_tier_name(requested_tier: str, available_tiers: list[str]) -> str | None:
    normalized_tiers: dict[str, str] = {
        tier_name.lower(): tier_name for tier_name in available_tiers
    }
    return normalized_tiers.get(requested_tier.lower())


async def _set_user_tier(session: AsyncSession, user_id: str, tier_name: str) -> None:
    await session.execute(
        update(UserBalance)
        .where(UserBalance.user_id == user_id)
        .values(user_tier=tier_name)
    )


def _create_manual_payment_record(user_id: str, micro_amount: int) -> PaymentRecord:
    fake_payment_id = f"manual_grant_{uuid4()}"
    return PaymentRecord(
        user_id=user_id,
        payment_intent_id=fake_payment_id,
        amount_cents=0,
        currency="USD",
        status="succeeded",
        credits_amount=micro_amount,
        payment_type="manual_grant",
        processed_at=utc_now_naive(),
        extra_metadata={"reason": "Manual dev top-up via script"},
    )


async def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    user_identifier: str = args.user_identifier.strip()
    amount: int | None = args.amount
    requested_tier_input: str | None = (
        args.tier_name.strip() if args.tier_name is not None else None
    )

    if amount is None and requested_tier_input is None:
        parser.error("provide an amount, --tier, or both")

    if amount is not None and amount <= 0:
        print("Error: amount must be greater than 0.")
        return 1

    if requested_tier_input == "":
        print("Error: tier must not be empty.")
        return 1

    actions: list[str] = []
    if amount is not None:
        actions.append(f"add {amount} credits")
    if requested_tier_input is not None:
        actions.append(f"set tier to {requested_tier_input}")

    print(f"Connecting to database to {' and '.join(actions)} for {user_identifier}...")

    async with get_db_context() as db_session:
        db = cast(AsyncSession, db_session)
        user = await _load_user(db, user_identifier)
        if user is None:
            return 1

        user_id = str(user.id)
        credits_service = CreditsService()

        requested_tier_name: str | None = None
        if requested_tier_input is not None:
            available_tiers = await _load_available_tiers(db)
            if not available_tiers:
                print("Error: no tiers are configured in the tier_limits table.")
                return 1

            requested_tier_name = _resolve_tier_name(
                requested_tier_input, available_tiers
            )
            if requested_tier_name is None:
                print(
                    f"Error: invalid tier '{requested_tier_input}'. "
                    f"Available tiers: {', '.join(available_tiers)}"
                )
                return 1

        if amount is not None:
            micro_amount = MicroDollar.from_dollars(amount).amount
            fake_payment = _create_manual_payment_record(user_id, micro_amount)
            db.add(fake_payment)
            await db.flush()

            await credits_service.add_credits(
                session=db,
                user_id=user_id,
                amount=micro_amount,
                reason="Manual dev top-up via script",
                transaction_type="manual_grant",
                stripe_payment_id=fake_payment.payment_intent_id,
            )

            if requested_tier_name is None:
                await TierService.refresh_tier(user_id=user_id, session=db)

            print(
                f"Successfully added {amount} credits "
                f"(micro={micro_amount}) to user {user_id}"
            )

        if requested_tier_name is not None:
            await credits_service.ensure_user_initialized(db, user_id)
            await _set_user_tier(db, user_id, requested_tier_name)

        final_balance = await _load_user_balance(db, user_id)
        if final_balance is None:
            print(
                f"Error: user balance row for '{user_id}' was not found after update."
            )
            return 1

        balance_label = "New Balance" if amount is not None else "Current Balance"
        print(
            f"{balance_label}: {MicroDollar(final_balance.credits_balance).to_credit()}"
        )

        tier_label = (
            "Updated Tier" if requested_tier_name is not None else "Current Tier"
        )
        print(f"{tier_label}: {final_balance.user_tier}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
