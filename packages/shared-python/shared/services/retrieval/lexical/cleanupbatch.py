"""Progress of one bounded maintenance pass over due rows."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CleanupBatch:
    """How many due rows one pass claimed, and how many of those it finished.

    A sweep keeps calling a pass while it still claims rows; claimed rows move
    their next due time forward, so a pass never claims the same row twice.
    """

    claimed: int
    succeeded: int

    def __add__(self, other: "CleanupBatch") -> "CleanupBatch":
        return CleanupBatch(claimed=self.claimed + other.claimed, succeeded=self.succeeded + other.succeeded)
