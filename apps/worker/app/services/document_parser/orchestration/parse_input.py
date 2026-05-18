from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParseOptions:
    llm_histories: int = 5
    smart_title_parse: bool = True
    summary_image: bool = True
    summary_table: bool = True
    summary_txt: bool = True
    stopwords: list[str] | None = None
    doc_type: str = "auto"
    add_frag_desc: str = ""


@dataclass(frozen=True)
class ParseInput:
    file_full_path: str
    filename: str
    output_dir: str
    internal_output_filename: str
    job_id: str | None = None
    namespace: str = "default"
    options: ParseOptions = field(default_factory=ParseOptions)
    base_url: str = ""
    fragment_content: str = ""
    s3_key: str | None = None
