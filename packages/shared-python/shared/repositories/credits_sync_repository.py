"""
Sync credits repository for worker-side billing operations.
"""

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from shared.models.database.credits_transaction import CreditsTransaction
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.user_balance import UserBalance
from shared.core.time import utc_now_naive


class SyncCreditsRepository:
    """Sync repository implementing the credits ledger queries."""

    def recalculate_balance_from_ledger(
        self,
        session: Session,
        user_id: str,
    ) -> int:
        """Aggregate all transactions to calculate the authoritative balance."""
        result = session.execute(
            select(func.coalesce(func.sum(CreditsTransaction.credits_amount), 0)).where(
                CreditsTransaction.user_id == user_id
            )
        )
        return int(result.scalar() or 0)

    def get_balance(self, session: Session, user_id: str) -> int:
        """Read the cached balance from the materialized view."""
        result = session.execute(
            select(UserBalance.credits_balance).where(UserBalance.user_id == user_id)
        )
        return int(result.scalar() or 0)

    def get_user_balance(
        self,
        session: Session,
        user_id: str,
    ) -> Optional[UserBalance]:
        """Return the user's balance row if it exists."""
        result = session.execute(
            select(UserBalance).where(UserBalance.user_id == user_id)
        )
        return result.scalar_one_or_none()

    def update_balance(
        self,
        session: Session,
        user_id: str,
        new_balance: int,
    ) -> None:
        """Update the materialized balance view."""
        session.execute(
            update(UserBalance)
            .where(UserBalance.user_id == user_id)
            .values(credits_balance=new_balance)
        )

    def get_recent_payment_credits(
        self,
        session: Session,
        user_id: str,
        days: int,
    ) -> int:
        """Return credits from succeeded payments still inside the validity window."""
        cutoff = utc_now_naive() - timedelta(days=days)
        result = session.execute(
            select(func.coalesce(func.sum(PaymentRecord.credits_amount), 0))
            .where(PaymentRecord.user_id == user_id)
            .where(PaymentRecord.status == "succeeded")
            .where(PaymentRecord.credits_amount.isnot(None))
            .where(PaymentRecord.created_at >= cutoff)
        )
        return int(result.scalar() or 0)

    def get_positive_credit_total_by_transaction_types(
        self,
        session: Session,
        user_id: str,
        transaction_types: tuple[str, ...],
    ) -> int:
        """Return positive credits from the specified transaction types."""
        if not transaction_types:
            return 0

        result = session.execute(
            select(func.coalesce(func.sum(CreditsTransaction.credits_amount), 0))
            .where(CreditsTransaction.user_id == user_id)
            .where(CreditsTransaction.credits_amount > 0)
            .where(CreditsTransaction.transaction_type.in_(transaction_types))
        )
        return int(result.scalar() or 0)

    def create_transaction(
        self,
        session: Session,
        transaction: CreditsTransaction,
    ) -> CreditsTransaction:
        """Insert a transaction row into the ledger."""
        session.add(transaction)
        session.flush()
        return transaction
