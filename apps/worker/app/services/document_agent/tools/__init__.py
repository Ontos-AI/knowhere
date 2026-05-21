"""Document Anatomy Agent tools.

Minimal set — only tools with genuine value are exported:

- ``scan_all_page_features``: full-page structural feature extraction (required).
- ``find_h1_boundaries``: locates level-1 headings via text search (required).
- ``classify_special_pages`` / ``heuristic_classify_special_pages``: page
  classification used internally by the agent (not called by LLM as a tool).
- ``propose_shard_plan`` / ``sample_pages`` / ``vlm_inspect_pages``: retained
  for the existing shard-planning path and optional VLM inspection.
"""

from app.services.document_agent.tools.classify_special_pages import (
    classify_special_pages,
    heuristic_classify_special_pages,
)
from app.services.document_agent.tools.find_h1_boundaries import find_h1_boundaries
from app.services.document_agent.tools.probe_sample_pages import sample_pages
from app.services.document_agent.tools.probe_vlm_inspect import vlm_inspect_pages
from app.services.document_agent.tools.propose_shard_plan import propose_shard_plan
from app.services.document_agent.tools.scan_all_page_features import (
    scan_all_page_features,
)

__all__ = [
    "classify_special_pages",
    "find_h1_boundaries",
    "heuristic_classify_special_pages",
    "propose_shard_plan",
    "sample_pages",
    "scan_all_page_features",
    "vlm_inspect_pages",
]
