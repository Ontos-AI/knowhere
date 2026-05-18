"""
Shared Credits Repository - Ledger Pattern Implementation
Used by both API and Worker modules
"""

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.credits_transaction import CreditsTransaction
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.user_balance import UserBalance
from shared.core.time import utc_now_naive


class CreditsRepository:
    """
    Credits repository implementing ledger pattern.

    Key Principle: Transactions are source of truth, balance is materialized view.
    """

    async def recalculate_balance_from_ledger(
        self, session: AsyncSession, user_id: str
    ) -> int:
        """
        Aggregate all transactions to calculate true balance.
        This is the authoritative source of truth.

        Args:
            session: Database session
            user_id: User ID

        Returns:
            Calculated balance (sum of all transaction amounts)
        """
        result = await session.execute(
            select(func.coalesce(func.sum(CreditsTransaction.credits_amount), 0)).where(
                CreditsTransaction.user_id == user_id
            )
        )
        return int(result.scalar() or 0)

    async def get_balance(self, session: AsyncSession, user_id: str) -> int:
        """
        Get cached balance from materialized view (user_balance table).
        Fast read operation - no aggregation needed.

        Args:
            session: Database session
            user_id: User ID

        Returns:
            Current balance from user_balance table
        """
        result = await session.execute(
            select(UserBalance.credits_balance).where(UserBalance.user_id == user_id)
        )
        return result.scalar() or 0

    async def get_user_balance(
        self, session: AsyncSession, user_id: str
    ) -> Optional[UserBalance]:
        """
        Get UserBalance record.

        Args:
            session: Database session
            user_id: User ID

        Returns:
            UserBalance record or None
        """
        result = await session.execute(
            select(UserBalance).where(UserBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_balance(
        self, session: AsyncSession, user_id: str, new_balance: int
    ) -> None:
        """
        Update materialized view (user_balance) with calculated balance.

        Args:
            session: Database session
            user_id: User ID
            new_balance: New balance value calculated from ledger
        """
        await session.execute(
            update(UserBalance)
            .where(UserBalance.user_id == user_id)
            .values(credits_balance=new_balance)
        )

    async def get_recent_payment_credits(
        self, session: AsyncSession, user_id: str, days: int
    ) -> int:
        """
        Get total credits from succeeded payments in last N days.
        Used for expiration logic.

        Args:
            session: Database session
            user_id: User ID
            days: Number of days to look back

        Returns:
            Total credits from recent payments
        """
        cutoff = utc_now_naive() - timedelta(days=days)
        result = await session.execute(
            select(func.coalesce(func.sum(PaymentRecord.credits_amount), 0))
            .where(PaymentRecord.user_id == user_id)
            .where(PaymentRecord.status == "succeeded")
            .where(PaymentRecord.credits_amount.isnot(None))
            .where(PaymentRecord.created_at >= cutoff)
        )
        return int(result.scalar() or 0)

    async def get_positive_credit_total_by_transaction_types(
        self,
        session: AsyncSession,
        user_id: str,
        transaction_types: tuple[str, ...],
    ) -> int:
        """
        Get total positive credits from specific transaction types.

        Args:
            session: Database session
            user_id: User ID
            transaction_types: Transaction types to include

        Returns:
            Total positive credits for the requested transaction types
        """
        if not transaction_types:
            return 0

        result = await session.execute(
            select(func.coalesce(func.sum(CreditsTransaction.credits_amount), 0))
            .where(CreditsTransaction.user_id == user_id)
            .where(CreditsTransaction.credits_amount > 0)
            .where(CreditsTransaction.transaction_type.in_(transaction_types))
        )
        return int(result.scalar() or 0)

    async def create_transaction(
        self, session: AsyncSession, transaction: CreditsTransaction
    ) -> CreditsTransaction:
        """
        Create a new transaction record.

        Args:
            session: Database session
            transaction: CreditsTransaction instance

        Returns:
            Created transaction
        """
        session.add(transaction)
        await session.flush()
        return transaction
