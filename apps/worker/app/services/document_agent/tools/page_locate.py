"""Page-memory residual title-location tools.

The implementations live in ``structure/page_locate_tools.py`` so the
page-memory sub-agent can register/dispatch them without importing this heavy
``tools`` package. Importing this module (e.g. via ``tools/__init__``) ensures
the tools are registered for the profile executor as well.
"""

from __future__ import annotations

from app.services.document_agent.structure.page_locate_tools import (  # noqa: F401
    grep_title_pages,
    verify_section_page,
)

__all__ = ["grep_title_pages", "verify_section_page"]
