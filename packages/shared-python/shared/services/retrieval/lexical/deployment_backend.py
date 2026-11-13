"""Report which lexical backend an ordinary release must render.

Ordinary releases follow the committed control row, so a deployment can never
select a different backend than the one the database serves. Changing the
backend is the operator transition's job: it gates the control row and deploys
its own reviewed task definitions. A release that meets a gated control row
stops instead of racing that transition.

Run inside the release image with the migration database URL:
``python -m shared.services.retrieval.lexical.deployment_backend``.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from shared.core.database_sync import get_sync_session_factory
from shared.models.database.lexicalcontrol import LexicalBackendControl

# The application bootstraps a missing control row from its own default.
DEFAULT_BACKEND: str = "postgresql"
OUTPUT_PREFIX: str = "KNOWHERE_LEXICAL_BACKEND="


class DeploymentBackendUnavailable(RuntimeError):
    """The control row does not permit an ordinary release right now."""


def readDeploymentBackend(sessions: sessionmaker[Session]) -> str:
    with sessions.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
        if control is None:
            return DEFAULT_BACKEND
        if control.mode != "serving":
            raise DeploymentBackendUnavailable(
                f"Lexical backend {control.backend} is {control.mode} at epoch {control.epoch}; "
                "finish or abort the backend transition before releasing"
            )
        return control.backend


def main() -> None:
    print(OUTPUT_PREFIX + readDeploymentBackend(get_sync_session_factory()), flush=True)


if __name__ == "__main__":
    main()
