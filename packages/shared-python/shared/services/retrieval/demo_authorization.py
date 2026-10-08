"""Internal transaction authorization for shared-corpus writes and caller stats."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import PermissionDeniedException


def require_demo_maintainer(user_id: str) -> None:
    if user_id not in settings.get_demo_maintainer_ids():
        raise PermissionDeniedException(
            user_message="Only demo maintainers may modify the shared demo corpus.",
            required_permission="demo_maintainer",
        )


def authorize_demo_transaction(db: Session, *, user_id: str) -> None:
    require_demo_maintainer(user_id)
    db.execute(
        text("SELECT set_config('knowhere.demo_maintainer', :identity, true)"),
        {"identity": user_id},
    )


def authorize_demo_caller(db: Session, *, user_id: str) -> None:
    db.execute(
        text("SELECT set_config('knowhere.demo_caller', :identity, true)"),
        {"identity": user_id},
    )


def verify_runtime_database_role(db: Session) -> None:
    hasBypass: bool = bool(
        db.execute(
            text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).scalar_one()
    )
    if hasBypass:
        raise RuntimeError(
            "API and Worker require a non-superuser database role without BYPASSRLS; use a separate migration role."
        )
