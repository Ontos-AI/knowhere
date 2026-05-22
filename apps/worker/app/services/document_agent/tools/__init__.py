"""Import tool modules so decorators register handlers."""

from app.services.document_agent.registry import REGISTRY

from . import classify_page_kinds as classify_page_kinds  # noqa: F401
from . import find_h1_boundaries as find_h1_boundaries  # noqa: F401
from . import find_toc_pages as find_toc_pages  # noqa: F401
from . import persist_anatomy_map as persist_anatomy_map  # noqa: F401
from . import probe_page_features as probe_page_features  # noqa: F401
from . import propose_hierarchy_assist as propose_hierarchy_assist  # noqa: F401
from . import propose_shard_plan as propose_shard_plan  # noqa: F401
from . import validate_anatomy_map as validate_anatomy_map  # noqa: F401

__all__ = ["REGISTRY"]
