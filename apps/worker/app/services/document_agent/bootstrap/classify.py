"""Bootstrap wrapper for deterministic page classification."""

from app.services.document_agent.tools.classify_page_kinds import classify_page_kinds

__all__ = ["classify_page_kinds"]
