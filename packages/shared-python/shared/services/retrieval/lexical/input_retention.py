"""Fence delayed input consumers and assignments behind durable GC state."""

from hashlib import sha256

from sqlalchemy.orm import Session

from shared.models.database.job_result import JobResult
from shared.models.database.lexicalinputinventory import LexicalInputInventory
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.storage import LexicalInputStorage


class LexicalInputRetention:
    @staticmethod
    def getIdentity(*, bucket: str, key: str) -> str:
        return sha256(f"{bucket}:{key}".encode()).hexdigest()

    @staticmethod
    def assertRetained(
        db: Session, result: JobResult, *, key: str | None = None, lock: bool = True,
    ) -> None:
        """Use the result row lock for loading or assigning live input."""
        if result.indexing_input_gc_state != "retained":
            raise LexicalIndexError("Indexing input is fenced for collection")
        objectKey: str | None = key or result.indexing_input_key
        if objectKey is None:
            return
        identity: str = LexicalInputRetention.getIdentity(bucket=LexicalInputStorage().bucket, key=objectKey)
        inventory: LexicalInputInventory | None = db.get(LexicalInputInventory, identity, with_for_update=lock)
        if inventory is not None and inventory.state in ("deleting", "deleted"):
            raise LexicalIndexError("Indexing input object is fenced for collection")
