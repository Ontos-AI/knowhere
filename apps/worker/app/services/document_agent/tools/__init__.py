"""Import tool modules so decorators register handlers."""

from app.services.document_agent.registry import REGISTRY

from . import extract_toc_with_boundaries as extract_toc_with_boundaries  # noqa: F401
from . import find_toc_anchor_pages as find_toc_anchor_pages  # noqa: F401
from . import grep_text as grep_text  # noqa: F401
from . import inspect_pages as inspect_pages  # noqa: F401
from . import judge_toc_source as judge_toc_source  # noqa: F401
from . import ocr_pages as ocr_pages  # noqa: F401
from . import probe_links as probe_links  # noqa: F401
from . import probe_outline as probe_outline  # noqa: F401
from . import propose_shard_plan as propose_shard_plan  # noqa: F401
from . import validate_anatomy_map as validate_anatomy_map  # noqa: F401
from . import verdict as verdict  # noqa: F401

__all__ = ["REGISTRY"]
