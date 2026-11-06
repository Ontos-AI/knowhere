"""One restartable object listing page; sizes are compressed object bytes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectInventoryPage:
    objects: tuple[tuple[str, int], ...]
    nextCursor: str | None
