"""Unified summary engine — shared output contracts.

This module defines the two intentional summary output shapes used across both
ingestion tracks (see audit §4.1):

- ``BodySummary`` (Contract A) — text chunks and per-page page tags. Carries
  ``summary`` + ``entities``. It has **no title**: the node/page already owns
  its section title via the hierarchy (``path`` / ``section_title``), and the
  summary layer must never regenerate it.
- ``AssetSummary`` (Contract B) — tables, images, figures, charts. Carries the
  asset's *own* ``title`` (caption/label) and a ``summary`` only. Assets do not
  extract entities; for statistical content the summary naturally incorporates
  key numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entity:
    """A typed entity extracted from content (§4.4).

    ``type`` defaults to the seed set (person / location / organization) but is
    an open string so the ``ENTITY_TYPES`` config can extend it without code
    changes. ``text`` is the surface form as it appears in the content.
    """

    text: str
    type: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "type": self.type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entity":
        return cls(
            text=str(data.get("text", "")).strip(),
            type=str(data.get("type", "")).strip(),
        )


@dataclass
class BodySummary:
    """Contract A — text chunk and per-page tag output.

    No ``title`` field by design: the section title lives on the node ``path``.
    """

    summary: str = ""
    entities: list[Entity] = field(default_factory=list)
    kind: str = "text"  # text | page

    def keywords_str(self, sep: str = ";") -> str:
        """Flattened entity surface forms for the transitional ``keywords`` column.

        During migration (§4.5) the legacy ``keywords`` column is kept populated
        from entity texts so the keyword-overlap graph keeps working until the
        entity-edge upgrade (§4.4) lands.
        """
        return sep.join(e.text for e in self.entities if e.text)


@dataclass
class AssetSummary:
    """Contract B — table / image / figure / chart output.

    ``title`` is the asset's own caption/label (genuinely new information), not
    a section heading. Assets carry summary only — no entity extraction.
    """

    title: str = ""
    summary: str = ""
    entities: list[Entity] = field(default_factory=list)
    """Always empty. Kept for row-schema compatibility; assets do not extract entities."""
    kind: str = "figure"  # table | image | figure

    def keywords_str(self, sep: str = ";") -> str:
        """Assets do not extract entities; always returns empty."""
        return ""
