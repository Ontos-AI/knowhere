"""
Sync credits service for worker-side billing operations.
"""

from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from shared.core.billing import MicroDollar
from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    InsufficientCreditsException,
)
from shared.core.logging import logger
from shared.models.database.credits_transaction import CreditsTransaction
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.user_balance import UserBalance
from shared.repositories.credits_sync_repository import SyncCreditsRepository
from shared.core.time import utc_now_naive

NON_EXPIRING_CREDIT_TRANSACTION_TYPES: tuple[str, ...] = ("refund",)


class SyncCreditsService:
    """Sync mirror of ``CreditsService`` using SQLAlchemy ``Session``."""

    def __init__(self) -> None:
        self.repository = SyncCreditsRepository()

    def ensure_user_initialized(
        self,
        session: Session,
        user_id: str,
    ) -> None:
        """Initialize first-time users with the default credits grant."""
        existing_balance = self.repository.get_user_balance(session, user_id)
        if existing_balance:
            return

        initial_dollars = getattr(settings, "FREE_PLAN_INITIAL_CREDITS", 5)
        initial_amount = MicroDollar.from_dollars(initial_dollars).amount

        insert_balance_stmt = (
            pg_insert(UserBalance)
            .values(user_id=user_id, credits_balance=initial_amount)
            .on_conflict_do_nothing(index_elements=[UserBalance.user_id])
            .returning(UserBalance.user_id)
        )
        insert_result = session.execute(insert_balance_stmt)
        inserted_user_id: str | None = insert_result.scalar_one_or_none()
        if inserted_user_id is None:
            return

        transaction: CreditsTransaction = CreditsTransaction(
            user_id=user_id,
            credits_amount=initial_amount,
            description="New user registration bonus",
            transaction_type="initial_grant",
        )
        session.add(transaction)

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
        session.flush()

        logger.info(f"User initialized: user_id={user_id}, credits={initial_amount}")

    def _apply_transaction_and_update_balance(
        self,
        session: Session,
        user_id: str,
        amount: int,
        transaction_type: str,
        description: str,
        stripe_payment_id: Optional[str] = None,
        transaction_metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Insert the ledger entry and refresh the materialized balance."""
        transaction = CreditsTransaction(
            user_id=user_id,
            credits_amount=amount,
            transaction_type=transaction_type,
            description=description,
            stripe_payment_id=stripe_payment_id,
            transaction_metadata=transaction_metadata,
        )
        self.repository.create_transaction(session, transaction)

        new_balance = self.repository.recalculate_balance_from_ledger(session, user_id)
        self.repository.update_balance(session, user_id, new_balance)

        logger.debug(
            f"Credits updated: user_id={user_id}, amount={amount}, "
            f"new_balance={new_balance}, type={transaction_type}"
        )
        return new_balance

    def get_balance(self, session: Session, user_id: str) -> int:
        """Read the current balance from the materialized view."""
        return self.repository.get_balance(session, user_id)

    def _check_and_expire_credits(
        self,
        session: Session,
        user_id: str,
    ) -> int:
        """Expire old credits before mutating the balance."""
        valid_days = getattr(settings, "CREDITS_VALID_DAYS", 365)
        current_balance = self.repository.get_balance(session, user_id)
        recent_credits = self.repository.get_recent_payment_credits(
            session, user_id, valid_days
        )
        non_expiring_credits = (
            self.repository.get_positive_credit_total_by_transaction_types(
                session,
                user_id,
                NON_EXPIRING_CREDIT_TRANSACTION_TYPES,
            )
        )
        expirable_balance = max(current_balance - non_expiring_credits, 0)

        if recent_credits >= expirable_balance:
            return 0

        expired_amount = expirable_balance - recent_credits
        self._apply_transaction_and_update_balance(
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

    def add_credits(
        self,
        session: Session,
        user_id: str,
        amount: int,
        reason: str,
        stripe_payment_id: Optional[str] = None,
        transaction_type: str = "purchase",
        transaction_metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add credits using the sync ledger flow."""
        self.ensure_user_initialized(session, user_id)
        self._check_and_expire_credits(session, user_id)

        new_balance = self._apply_transaction_and_update_balance(
            session=session,
            user_id=user_id,
            amount=amount,
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

    def deduct_credits(
        self,
        session: Session,
        user_id: str,
        amount: int,
        reason: str,
        api_key_id: Optional[str] = None,
    ) -> int:
        """Deduct credits or raise ``InsufficientCreditsException``."""
        self.ensure_user_initialized(session, user_id)
        self._check_and_expire_credits(session, user_id)

        current_balance = self.get_balance(session, user_id)
        if current_balance < amount:
            raise InsufficientCreditsException(
                user_message=f"Insufficient credits. Required: {amount}, Available: {current_balance}",
                required_credits=amount,
                current_balance=current_balance,
                internal_message=(
                    f"User {user_id} has insufficient credits: "
                    f"{current_balance} < {amount}"
                ),
            )

        new_balance = self._apply_transaction_and_update_balance(
            session=session,
            user_id=user_id,
            amount=-amount,
            transaction_type="usage",
            description=reason,
            transaction_metadata={"api_key_id": api_key_id} if api_key_id else None,
        )

        logger.info(
            f"Credits deducted: user_id={user_id}, amount={amount}, "
            f"new_balance={new_balance}"
        )
        return new_balance

    def refund_job_credits(
        self,
        session: Session,
        user_id: str,
        amount: int,
        job_id: str,
        reason: str = "Job execution failed",
    ) -> int:
        """Refund a job once; duplicate refunds return the current balance."""
        stmt = select(CreditsTransaction).where(
            CreditsTransaction.user_id == user_id,
            CreditsTransaction.transaction_type == "refund",
            func.json_extract_path_text(
                CreditsTransaction.transaction_metadata, "job_id"
            )
            == job_id,
        )
        result = session.execute(stmt)
        existing = result.first()

        if existing:
            logger.info(f"Job {job_id} already refunded, returning current balance")
            return self.repository.get_balance(session, user_id)

        new_balance = self.add_credits(
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

    def get_usage_stats(
        self,
        session: Session,
        user_id: str,
        period: str = "month",
    ) -> Dict[str, Any]:
        """Return aggregate usage statistics for the user."""

        start_time = None
        if period == "month":
            start_time = utc_now_naive() - timedelta(days=30)
        elif period == "week":
            start_time = utc_now_naive() - timedelta(days=7)

        query = select(
            func.coalesce(func.sum(CreditsTransaction.credits_amount), 0).label(
                "total_used"
            ),
            func.count(CreditsTransaction.id).label("transaction_count"),
        ).where(CreditsTransaction.user_id == user_id)
        query = query.where(CreditsTransaction.transaction_type == "usage")

        if start_time is not None:
            query = query.where(CreditsTransaction.created_at >= start_time)

        result = session.execute(query)
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
            "avg_response_time": 0.0,
        }

    def get_transaction_history(
        self,
        session: Session,
        user_id: str,
        limit: int = 50,
    ) -> list[CreditsTransaction]:
        """Return recent ledger entries for the user."""
        result = session.execute(
            select(CreditsTransaction)
            .where(CreditsTransaction.user_id == user_id)
            .order_by(CreditsTransaction.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
