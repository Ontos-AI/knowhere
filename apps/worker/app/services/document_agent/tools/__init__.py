"""Document Anatomy Agent tools.

- ``scan_all_page_features``: full-page structural feature extraction.
- ``find_h1_boundaries``: locates level-1 headings via text search.
- ``classify_special_pages`` / ``heuristic_classify_special_pages``: page
  classification used internally by the agent (not called by LLM as a tool).
"""

from app.services.document_agent.tools.classify_special_pages import (
    classify_special_pages,
    heuristic_classify_special_pages,
)
from app.services.document_agent.tools.find_h1_boundaries import find_h1_boundaries
from app.services.document_agent.tools.scan_all_page_features import (
    scan_all_page_features,
)

__all__ = [
    "classify_special_pages",
    "find_h1_boundaries",
    "heuristic_classify_special_pages",
    "scan_all_page_features",
]
