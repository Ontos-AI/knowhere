"""Reset retrieval hit stats in Hipo namespaces.

Until retrieval requests carried a purpose, every automatic per-turn recall
from Hipo recorded hits, so these counts mostly reinforce themselves rather
than reflect use. From this release only explicit searches and reported uses
record hits, and importance restarts from each document's upload time. Other
callers' stats stay as they are.

The deleted counts cannot be restored, so downgrade does nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "809ee432a5f6"
down_revision: str | None = "e108c52f6bf8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM retrieval_hit_stats WHERE namespace LIKE 'hipo-%'")


def downgrade() -> None:
    pass
