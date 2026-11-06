"""Namespace generation locking for serving-state lifecycle updates."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from shared.models.database.document import RetrievalNamespaceGeneration
from shared.services.retrieval.corpus_storage import CorpusStorage


def lock_namespace_generation(
    db: Session,
    *,
    user_id: str,
    namespace: str,
    is_shared: bool = False,
) -> RetrievalNamespaceGeneration:
    """Create if needed, then lock and return one namespace generation row."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    user_id = corpusStorage.resolve_owner(user_id)
    if corpusStorage.is_demo:
        # Public demo readers cannot INSERT or take UPDATE row locks under RLS.
        # Every demo writer takes the matching transaction advisory lock first.
        generation_id: str = f"rng_{sha256(f'{user_id}:{namespace}'.encode()).hexdigest()}"
        identity: int = int.from_bytes(
            sha256(generation_id.encode()).digest()[:8], "big", signed=True
        )
        db.execute(select(
            func.pg_advisory_xact_lock_shared(identity)
            if is_shared
            else func.pg_advisory_xact_lock(identity)
        ))
        if is_shared:
            existing = db.execute(select(corpusStorage.RetrievalNamespaceGeneration).where(
                corpusStorage.RetrievalNamespaceGeneration.user_id == user_id,
                corpusStorage.RetrievalNamespaceGeneration.namespace == namespace)).scalar_one_or_none()
            # An uninitialized demo corpus has generation zero. This object is
            # never added to the session; public readers create no database rows.
            return existing if existing is not None else corpusStorage.RetrievalNamespaceGeneration(
                id=generation_id, user_id=user_id, namespace=namespace, generation=0)
    elif is_shared:
        existing = db.execute(select(corpusStorage.RetrievalNamespaceGeneration).where(
            corpusStorage.RetrievalNamespaceGeneration.user_id == user_id,
            corpusStorage.RetrievalNamespaceGeneration.namespace == namespace,
        ).with_for_update(read=True)).scalar_one_or_none()
        if existing is not None:
            return existing
    db.execute(
        insert(corpusStorage.RetrievalNamespaceGeneration)
        .values(
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
        .with_for_update(read=is_shared)
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
