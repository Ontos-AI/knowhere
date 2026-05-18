from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TypedDict
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.core.database import engine, get_db_context
from shared.models.database.api_key import APIKey
from shared.models.database.user import User
from shared.models.database.user_balance import UserBalance
from shared.services.billing.credits_service import CreditsService
from shared.services.auth.user_table_bootstrap import ensure_better_auth_user_table
from shared.utils.api_keys import generate_api_key, hash_api_key, mask_api_key

_DEFAULT_API_KEY_NAME: str = "standalone-api-key"
_DEFAULT_USER_TIER: str = "tier_5"


class InitializedStandaloneUser(TypedDict):
    user_id: str
    email: str
    api_key_name: str
    api_key: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or fetch an API-only standalone user and generate an API key."
        ),
    )
    parser.add_argument("--email", required=True, help="User email address.")
    parser.add_argument(
        "--user-id",
        default="",
        help="Optional user ID for deterministic local or test bootstrap.",
    )
    parser.add_argument("--name", default="", help="Display name for new users.")
    parser.add_argument(
        "--key-name",
        default=_DEFAULT_API_KEY_NAME,
        help="Display name prefix for the generated API key.",
    )
    parser.add_argument(
        "--tier",
        default=_DEFAULT_USER_TIER,
        help="Compatibility user tier to store in user_balances. Examples: tier_1, tier_2, tier_3, tier_4, tier_5, guest",
    )
    return parser


async def _ensure_user_table() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(ensure_better_auth_user_table)


async def _find_or_create_user(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    requested_user_id: str,
) -> User:
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("email must not be empty")

    normalized_user_id = requested_user_id.strip()
    result = await session.execute(
        select(User).where(User.email == normalized_email).limit(1)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    if normalized_user_id:
        existing_user = await session.get(User, normalized_user_id)
        if existing_user is not None:
            raise ValueError(
                "requested user_id already exists for a different email: "
                f"user_id={normalized_user_id}"
            )

    user = User(
        id=normalized_user_id or f"user_{uuid4().hex[:24]}",
        name=name.strip() or normalized_email,
        email=normalized_email,
    )
    session.add(user)
    await session.flush()
    return user


async def _initialize_user_credits(
    session: AsyncSession,
    *,
    user_id: str,
    tier: str,
) -> None:
    credits_service = CreditsService()
    await credits_service.ensure_user_initialized(session, user_id)
    await session.execute(
        update(UserBalance).where(UserBalance.user_id == user_id).values(user_tier=tier)
    )


async def _resolve_key_name(
    session: AsyncSession,
    *,
    user_id: str,
    requested_name: str,
) -> str:
    key_name = requested_name.strip() or _DEFAULT_API_KEY_NAME
    existing_names = set(
        (
            await session.execute(
                select(APIKey.name).where(APIKey.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    if key_name not in existing_names:
        return key_name

    suffix = 2
    while f"{key_name}-{suffix}" in existing_names:
        suffix += 1
    return f"{key_name}-{suffix}"


async def _create_api_key(
    session: AsyncSession,
    *,
    user_id: str,
    key_name: str,
) -> str:
    api_key = generate_api_key()
    session.add(
        APIKey(
            user_id=user_id,
            key_hash=hash_api_key(api_key),
            key_mask=mask_api_key(api_key),
            name=key_name,
            enabled_modules=["all"],
        )
    )
    return api_key


async def initialize_standalone_user(
    *,
    email: str,
    user_id: str = "",
    name: str = "",
    key_name: str = _DEFAULT_API_KEY_NAME,
    tier: str = _DEFAULT_USER_TIER,
) -> InitializedStandaloneUser:
    await _ensure_user_table()
    async with get_db_context() as session:
        user = await _find_or_create_user(
            session,
            email=email,
            name=name,
            requested_user_id=user_id,
        )
        await _initialize_user_credits(
            session,
            user_id=user.id,
            tier=tier.strip() or _DEFAULT_USER_TIER,
        )
        key_name = await _resolve_key_name(
            session,
            user_id=user.id,
            requested_name=key_name,
        )
        api_key = await _create_api_key(
            session,
            user_id=user.id,
            key_name=key_name,
        )

    return {
        "user_id": str(user.id),
        "email": str(user.email),
        "api_key_name": key_name,
        "api_key": api_key,
    }


async def _run(args: argparse.Namespace) -> int:
    initialized_user = await initialize_standalone_user(
        email=str(args.email),
        user_id=str(args.user_id),
        name=str(args.name),
        key_name=str(args.key_name),
        tier=str(args.tier),
    )

    print(f"user_id={initialized_user['user_id']}")
    print(f"email={initialized_user['email']}")
    print(f"api_key_name={initialized_user['api_key_name']}")
    print(f"api_key={initialized_user['api_key']}")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
