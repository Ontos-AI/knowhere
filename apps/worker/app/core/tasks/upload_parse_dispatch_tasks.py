from loguru import logger

from shared.core.celery_app import get_celery_app
from shared.core.database_sync import get_sync_db_context
from shared.services.jobs.upload_parse_dispatch import UploadParseOutbox
from shared.services.jobs.upload_parse_publisher import publish_upload_parse

_MAX_DISPATCHES_PER_SWEEP = 32
_TASK_NAME = (
    "app.core.tasks.upload_parse_dispatch_tasks.recover_upload_parse_dispatches"
)


@get_celery_app().task(name=_TASK_NAME)
def recover_upload_parse_dispatches() -> int:
    """Re-publish confirmed uploads whose initial Celery send was unacknowledged."""
    published_count = 0
    for _ in range(_MAX_DISPATCHES_PER_SWEEP):
        with get_sync_db_context() as db:
            claim = UploadParseOutbox.claim_due(db)
        if claim is None:
            break

        published = False
        try:
            publish_upload_parse(job_id=claim.job_id, user_id=claim.user_id)
            published = True
        except Exception as exc:
            logger.warning(
                "Upload parse recovery could not publish: "
                f"job_id={claim.job_id}, attempt={claim.attempt}, error={exc}"
            )

        with get_sync_db_context() as db:
            UploadParseOutbox.finish(db, claim, published=published)
        if not published:
            break
        published_count += 1

    return published_count
