"""The UTC days a caller searched a namespace, by which its documents age."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import RetrievalActiveDay


def utc_day(moment: datetime) -> date:
    """The UTC day of a moment; a naive moment is already UTC."""
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(timezone.utc).date()


@dataclass(frozen=True)
class ActiveDays:
    """A namespace's active days, today included.

    Today counts because the search being ranked is itself today's activity,
    even before its own record is written.
    """

    days: tuple[date, ...]

    @classmethod
    def of(cls, recorded: Iterable[date], today: date) -> ActiveDays:
        return cls(tuple(sorted({*recorded, today})))

    def age_after(self, moment: datetime) -> int:
        """Active days after the moment's UTC day; the moment's own day is 0."""
        return len(self.days) - bisect_right(self.days, utc_day(moment))


async def load_active_days(
    db: AsyncSession, *, user_id: str, namespace: str
) -> ActiveDays:
    result = await db.execute(
        select(RetrievalActiveDay.day)
        .where(RetrievalActiveDay.user_id == user_id)
        .where(RetrievalActiveDay.namespace == namespace)
    )
    return ActiveDays.of(result.scalars().all(), utc_day(datetime.now(timezone.utc)))


async def record_active_day(
    db: AsyncSession, *, user_id: str, namespace: str, day: date
) -> None:
    """Record that the caller searched the namespace on this day; once per day."""
    await db.execute(
        insert(RetrievalActiveDay)
        .values(user_id=user_id, namespace=namespace, day=day)
        .on_conflict_do_nothing()
    )
