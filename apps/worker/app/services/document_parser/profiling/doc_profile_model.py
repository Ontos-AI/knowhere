from __future__ import annotations

import gc
import json
import os
from dataclasses import asdict, dataclass, field
from typing import List, Literal, Optional

from loguru import logger


@dataclass
class DocProfile:
    """Document profile data contract used by parser routing."""

    file_type: str = ""
    route: Literal["fast", "standard"] = "standard"
    decision_band: Literal["safe_fast", "gray_zone", "safe_standard"] = "safe_standard"
    scan_type: Optional[Literal["electronic", "scanned", "mixed"]] = None
    doc_category: Literal["generic", "atlas", "ppt_converted"] = "generic"
    page_count: int = 0
    avg_text_density: float = 0.0
    avg_image_coverage: float = 0.0
    has_tables: bool = False
    has_embedded_fonts: bool = False
    is_multi_column: bool = False
    is_degraded_electronic: bool = False
    sample_text: str = ""
    has_significant_images: bool = False
    significant_image_count: int = 0
    max_image_coverage_on_page: float = 0.0
    pages_with_significant_images: int = 0
    large_image_page_ratio: float = 0.0
    table_signal_pages: int = 0
    table_signal_strength: float = 0.0
    complex_pages: int = 0
    complex_page_ratio: float = 0.0
    max_drawing_count: int = 0
    min_text_density_page: float = 0.0
    text_density_std: float = 0.0
    estimated_fast_benefit: float = 0.0
    estimated_risk_score: float = 0.0
    atlas_candidate: bool = False
    page_details: List[dict] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("page_details", None)
        data.pop("sample_text", None)
        return data

    def summary(self) -> str:
        parts = (
            f"[{self.file_type.upper()}] route={self.route}, band={self.decision_band}, "
            f"scan={self.scan_type}, category={self.doc_category}, "
            f"pages={self.page_count}, text_density={self.avg_text_density:.0f}, "
            f"img_coverage={self.avg_image_coverage:.1%}, "
            f"risk={self.estimated_risk_score:.2f}, gain={self.estimated_fast_benefit:.2f}"
        )
        if self.is_degraded_electronic:
            parts += ", degraded=True"
        return parts


def publish_profile_result(queue, profile: DocProfile) -> None:
    gc.collect()
    queue.put({"ok": True, "profile": asdict(profile)})


def save_profile_metadata(profile: DocProfile, output_dir: str) -> None:
    profile_path = os.path.join(output_dir, "profile.json")
    with open(profile_path, "w", encoding="utf-8") as file_obj:
        json.dump(profile.to_dict(), file_obj, ensure_ascii=False, indent=2)
    logger.debug(f"Profile metadata saved to {profile_path}")
