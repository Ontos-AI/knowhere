"""Namespace generation locking for serving-state lifecycle updates."""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from shared.models.database.document import RetrievalNamespaceGeneration


def lock_namespace_generation(
    db: Session,
    *,
    user_id: str,
    namespace: str,
) -> RetrievalNamespaceGeneration:
    """Create if needed, then lock and return one namespace generation row."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    user_id = corpusStorage.resolve_owner(user_id)
    generation_id = f"rng_{sha256(f'{user_id}:{namespace}'.encode()).hexdigest()}"
    db.execute(
        insert(corpusStorage.RetrievalNamespaceGeneration)
        .values(
            id=generation_id,
            user_id=user_id,
            namespace=namespace,
            generation=0,
        )
        .on_conflict_do_nothing(
            index_elements=[
                corpusStorage.RetrievalNamespaceGeneration.user_id,
                corpusStorage.RetrievalNamespaceGeneration.namespace,
            ]
        )
    )
    generation = db.execute(
        select(corpusStorage.RetrievalNamespaceGeneration)
        .where(corpusStorage.RetrievalNamespaceGeneration.user_id == corpusStorage.resolve_owner(user_id))
        .where(corpusStorage.RetrievalNamespaceGeneration.namespace == namespace)
        .with_for_update()
    ).scalar_one()
    return generation


def advance_namespace_generation(
    db: Session,
    *,
    user_id: str,
    namespace: str,
    locked_generation: RetrievalNamespaceGeneration | None = None,
) -> int:
    """Increment a locked namespace generation and return its new value."""
    generation = locked_generation or lock_namespace_generation(
        db, user_id=user_id, namespace=namespace
    )
    generation.generation += 1
    db.flush()
    return generation.generation
