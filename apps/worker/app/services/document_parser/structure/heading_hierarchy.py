from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from app.services.document_parser.structure.layout_parser import pred_titles


@dataclass(frozen=True)
class HeadingHierarchyInput:
    infos: Any
    doc_type: Literal["pptx", "md", "docx"]
    toc_hierarchies: Any | None = None
    prompt_limit: int = 4000
    enable_regex: bool = True
    smart_parse: bool = False
    model_name: str | None = None
    output_dir: str | None = None
    layout_json_path: str | None = None
    first_toc_ele_num: int | None = None


def predict_heading_hierarchy(heading_input: HeadingHierarchyInput) -> pd.DataFrame:
    return pred_titles(
        heading_input.infos,
        doc_type=heading_input.doc_type,
        toc_hierarchies=heading_input.toc_hierarchies,
        prompt_limt=heading_input.prompt_limit,
        enable_regx=heading_input.enable_regex,
        smart_parse=heading_input.smart_parse,
        model_name=heading_input.model_name,
        output_dir=heading_input.output_dir,
        layout_json_path=heading_input.layout_json_path,
        first_toc_ele_num=heading_input.first_toc_ele_num,
    )
