"""
Shared Credits Service - Ledger Pattern Implementation
Used by both API and Worker modules

This service implements the ledger pattern where:
- Transactions (CreditsTransaction) are the source of truth
- UserBalance is a materialized view (cached aggregate)
- All operations are atomic within a transaction
"""

from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.billing import MicroDollar
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    InsufficientCreditsException,
)
from shared.core.logging import logger
from shared.models.database.credits_transaction import CreditsTransaction
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.user_balance import UserBalance
from shared.repositories.credits_repository import CreditsRepository
from shared.core.time import utc_now_naive

NON_EXPIRING_CREDIT_TRANSACTION_TYPES: tuple[str, ...] = ("refund",)


class CreditsService:
    """
    Credits management service implementing ledger pattern.

    Core Principle:
    ---------------
    1. Insert transaction record (source of truth)
    2. Recalculate balance from ALL transactions
    3. Update user_balance (materialized view)
    4. All in ONE database transaction

    User Initialization:
    --------------------
    The `ensure_user_initialized()` method is called in TWO contexts:

    1. **Credit modification operations** (add_credits, deduct_credits):
       Called as a safety check before modifying credits. This ensures
       the user balance exists before any write operation. And we can
       not ensure the /billing/credits endpoint is called before any
       credit modification operation.

    2. **First-use user flows**:
       Called before reading balance data, either from an API route or from
       the tier lookup path used by authenticated route guards. `get_balance()`
       is intentionally kept fast (no initialization check) for performance.

    Usage:
    ------
    Both API and Worker modules can use this:

    ```python
    credits_service = CreditsService()

    async with get_db_context() as session:
        new_balance = await credits_service.deduct_credits(
            session=session,
            user_id="user123",
            amount=1000000,  # 1 microdollar = $1
            reason="Job processing"
        )
        await session.commit()  # Atomic: transaction + balance update
    ```
    """

    def __init__(self):
        self.repository = CreditsRepository()

    async def ensure_user_initialized(
        self, session: AsyncSession, user_id: str
    ) -> None:
        """
        Initialize new user with default credits if not exists.
        Creates both UserBalance and initial transaction records.

        Args:
            session: Database session
            user_id: User ID
        """
        existing_balance = await self.repository.get_user_balance(session, user_id)
        if existing_balance:
            return

        # Get initial credits amount
        initial_dollars = getattr(settings, "FREE_PLAN_INITIAL_CREDITS", 5)
        initial_amount = MicroDollar.from_dollars(initial_dollars).amount

        insert_balance_stmt = (
            pg_insert(UserBalance)
            .values(user_id=user_id, credits_balance=initial_amount)
            .on_conflict_do_nothing(index_elements=[UserBalance.user_id])
            .returning(UserBalance.user_id)
        )
        insert_result = await session.execute(insert_balance_stmt)
        inserted_user_id: str | None = insert_result.scalar_one_or_none()
        if inserted_user_id is None:
            return

        # Create initial transaction record only for the request that won creation.
        transaction: CreditsTransaction = CreditsTransaction(
            user_id=user_id,
            credits_amount=initial_amount,
            description="New user registration bonus",
            transaction_type="initial_grant",
        )
        session.add(transaction)

        # Create payment record for tracking only for the request that won creation.
        payment: PaymentRecord = PaymentRecord(
            user_id=user_id,
            payment_type="system_grant",
            amount_cents=0,
            currency="USD",
            status="succeeded",
            credits_amount=initial_amount,
            extra_metadata={"reason": "initial_grant"},
            processed_at=utc_now_naive(),
        )
        session.add(payment)
        await session.flush()

        logger.info(f"User initialized: user_id={user_id}, credits={initial_amount}")

    async def _apply_transaction_and_update_balance(
        self,
        session: AsyncSession,
        user_id: str,
        amount: int,  # Positive or negative
        transaction_type: str,
        description: str,
        stripe_payment_id: Optional[str] = None,
        transaction_metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Core ledger pattern implementation:
        1. Insert transaction record (source of truth)
        2. Aggregate all transactions to calculate true balance
        3. Update user_balance (materialized view)

        All operations in caller's transaction context.

        Args:
            session: Database session
            user_id: User ID
            amount: Credit amount (positive for add, negative for deduct)
            transaction_type: Type of transaction
            description: Human-readable description
            stripe_payment_id: Optional Stripe payment ID
            transaction_metadata: Optional metadata dict

        Returns:
            New balance after operation
        """
        # Step 1: Insert transaction record (ledger entry)
        transaction = CreditsTransaction(
            user_id=user_id,
            credits_amount=amount,
            transaction_type=transaction_type,
            description=description,
            stripe_payment_id=stripe_payment_id,
            transaction_metadata=transaction_metadata,
        )
        await self.repository.create_transaction(session, transaction)

        # Step 2: Recalculate balance from ledger (source of truth)
        new_balance = await self.repository.recalculate_balance_from_ledger(
            session, user_id
        )

        # Step 3: Update materialized view
        await self.repository.update_balance(session, user_id, new_balance)

        logger.debug(
            f"Credits updated: user_id={user_id}, amount={amount}, "
            f"new_balance={new_balance}, type={transaction_type}"
        )

        return new_balance

    async def get_balance(self, session: AsyncSession, user_id: str) -> int:
        """
        Get current balance (fast read from materialized view).

        Args:
            session: Database session
            user_id: User ID

        Returns:
            Current credits balance
        """
        return await self.repository.get_balance(session, user_id)

    async def _check_and_expire_credits(
        self, session: AsyncSession, user_id: str
    ) -> int:
        """
        Check and expire old credits before balance operations.
        This is called during credit modifications, not on reads.

        Logic:
        - Get current balance from materialized view
        - Get total credits from payments within valid period
        - If balance exceeds valid credits, expire the difference

        Args:
            session: Database session
            user_id: User ID

        Returns:
            Amount expired (0 if nothing expired)
        """
        valid_days = getattr(settings, "CREDITS_VALID_DAYS", 365)
        current_balance = await self.repository.get_balance(session, user_id)
        recent_credits = await self.repository.get_recent_payment_credits(
            session, user_id, valid_days
        )
        non_expiring_credits = (
            await self.repository.get_positive_credit_total_by_transaction_types(
                session,
                user_id,
                NON_EXPIRING_CREDIT_TRANSACTION_TYPES,
            )
        )
        expirable_balance = max(current_balance - non_expiring_credits, 0)

        if recent_credits < expirable_balance:
            expired_amount = expirable_balance - recent_credits

            # Use ledger pattern to record expiration
            await self._apply_transaction_and_update_balance(
                session=session,
                user_id=user_id,
                amount=-expired_amount,
                transaction_type="expiration",
                description=f"Credits expired (>{valid_days} days old)",
            )

            logger.info(
                f"Credits expired: user_id={user_id}, amount={expired_amount}, "
                f"remaining={current_balance - expired_amount}"
            )

            return expired_amount

        return 0

    async def add_credits(
        self,
        session: AsyncSession,
        user_id: str,
        amount: int,
        reason: str,
        stripe_payment_id: Optional[str] = None,
        transaction_type: str = "purchase",
        transaction_metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Add credits using ledger pattern.

        Args:
            session: Database session
            user_id: User ID
            amount: Credits to add (positive)
            reason: Description of why credits added
            stripe_payment_id: Optional Stripe payment ID
            transaction_type: Type of transaction (purchase, bonus, refund, etc.)
            transaction_metadata: Optional metadata

        Returns:
            New balance after addition
        """
        await self.ensure_user_initialized(session, user_id)

        # Check and expire old credits before adding new ones
        await self._check_and_expire_credits(session, user_id)

        new_balance = await self._apply_transaction_and_update_balance(
            session=session,
            user_id=user_id,
            amount=amount,  # Positive
            transaction_type=transaction_type,
            description=reason,
            stripe_payment_id=stripe_payment_id,
            transaction_metadata=transaction_metadata,
        )

        logger.info(
            f"Credits added: user_id={user_id}, amount={amount}, "
            f"new_balance={new_balance}, type={transaction_type}"
        )

        return new_balance

    async def deduct_credits(
        self,
        session: AsyncSession,
        user_id: str,
        amount: int,
        reason: str,
        api_key_id: Optional[str] = None,
    ) -> int:
        """
        Deduct credits using ledger pattern.
        Raises InsufficientCreditsException if balance too low.

        Args:
            session: Database session
            user_id: User ID
            amount: Credits to deduct (positive number)
            reason: Description of why credits deducted
            api_key_id: Optional API key ID for tracking

        Returns:
            New balance after deduction

        Raises:
            InsufficientCreditsException: If insufficient credits
        """
        await self.ensure_user_initialized(session, user_id)

        # Check and expire old credits before deducting
        await self._check_and_expire_credits(session, user_id)

        # Check current balance (read from materialized view, after expiration)
        current_balance = await self.get_balance(session, user_id)
        if current_balance < amount:
            raise InsufficientCreditsException(
                user_message=f"Insufficient credits. Required: {amount}, Available: {current_balance}",
                required_credits=amount,
                internal_message=f"User {user_id} has insufficient credits: {current_balance} < {amount}",
            )

        # Apply ledger entry (negative amount)
        new_balance = await self._apply_transaction_and_update_balance(
            session=session,
            user_id=user_id,
            amount=-amount,  # Negative!
            transaction_type="usage",
            description=reason,
            transaction_metadata={"api_key_id": api_key_id} if api_key_id else None,
        )

        logger.info(
            f"Credits deducted: user_id={user_id}, amount={amount}, "
            f"new_balance={new_balance}"
        )

        return new_balance

    async def refund_job_credits(
        self,
        session: AsyncSession,
        user_id: str,
        amount: int,
        job_id: str,
        reason: str = "Job execution failed",
    ) -> int:
        """
        Refund credits for a job (idempotent: same job_id only refunded once).

        Args:
            session: Database session
            user_id: User ID
            amount: Credits to refund
            job_id: Job ID (used for idempotency)
            reason: Refund reason

        Returns:
            New balance after refund. If the job was already refunded,
            returns the current balance without creating a duplicate ledger entry.
        """
        from sqlalchemy import func, select

        # Check if already refunded
        stmt = select(CreditsTransaction).where(
            CreditsTransaction.user_id == user_id,
            CreditsTransaction.transaction_type == "refund",
            func.json_extract_path_text(
                CreditsTransaction.transaction_metadata, "job_id"
            )
            == job_id,
        )

        result = await session.execute(stmt)
        existing = result.first()

        if existing:
            logger.info(f"Job {job_id} already refunded, returning current balance")
            return await self.repository.get_balance(session, user_id)

        # Execute refund
        new_balance = await self.add_credits(
            session=session,
            user_id=user_id,
            amount=amount,
            reason=reason,
            transaction_type="refund",
            transaction_metadata={"job_id": job_id},
        )

        logger.info(
            f"Job refunded: job_id={job_id}, amount={amount}, new_balance={new_balance}"
        )
        return new_balance

    async def get_usage_stats(
        self, session: AsyncSession, user_id: str, period: str = "month"
    ) -> Dict[str, Any]:
        """
        Get usage statistics for a user.

        Args:
            session: Database session
            user_id: User ID
            period: Time period ("month", "week", "all_time")

        Returns:
            Dict with usage stats
        """

        from sqlalchemy import func, select

        # Calculate time range
        if period == "month":
            start_time = utc_now_naive() - timedelta(days=30)
        elif period == "week":
            start_time = utc_now_naive() - timedelta(days=7)
        else:  # all_time
            start_time = None

        # Build query
        query = select(
            func.coalesce(func.sum(CreditsTransaction.credits_amount), 0).label(
                "total_used"
            ),
            func.count(CreditsTransaction.id).label("transaction_count"),
        ).where(CreditsTransaction.user_id == user_id)

        # Only count usage transactions (negative amounts)
        query = query.where(CreditsTransaction.transaction_type == "usage")

        if start_time:
            query = query.where(CreditsTransaction.created_at >= start_time)

        result = await session.execute(query)
        stats = result.first()
        if stats is None:
            return {
                "period": period,
                "total_used": 0,
                "transaction_count": 0,
                "avg_response_time": 0.0,
            }

        return {
            "period": period,
            "total_used": int(abs(stats.total_used or 0)),
            "transaction_count": int(stats.transaction_count or 0),
            "avg_response_time": 0.0,  # TODO: Calculate from usage logs if needed
        }

    async def get_transaction_history(
        self, session: AsyncSession, user_id: str, limit: int = 50
    ) -> list:
        """
        Get transaction history for a user.

        Args:
            session: Database session
            user_id: User ID
            limit: Maximum number of transactions to return

        Returns:
            List of CreditsTransaction records
        """
        from sqlalchemy import select

        result = await session.execute(
            select(CreditsTransaction)
            .where(CreditsTransaction.user_id == user_id)
            .order_by(CreditsTransaction.created_at.desc())
            .limit(limit)
        )

        return list(result.scalars().all())
