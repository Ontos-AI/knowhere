# Knowhere corpus

A namespace holds documents. Each document is a tree of sections that follows
its headings. A section is addressed by document_id + section_path;
section_path joins the heading titles from the top level down with " / ".

A section can own one body chunk:
- text: the section's own text (Word, Markdown, Excel, ...)
- page: PDF pages belonging to the section, as text plus a page image
A section with child sections may have no body of its own; its content is in
its children.

Images and tables are separate chunks, addressed by document_id + chunk_id.
Result rows show the section that contains them.

Example:

doc_7f3a  Hypertension_Guideline.pdf
├── 1 Overview                        [text]
├── 2 Treatment                       (no body; see children)
│   ├── 2 Treatment / 2.1 Lifestyle   [text]
│   └── 2 Treatment / 2.2 Drugs       [text]  contains table chunk_id=tbl_12
└── 3 Follow-up                       [page p.14-15]

Addresses from this example:
- {document_id: "doc_7f3a", section_path: "2 Treatment / 2.2 Drugs"}
- {document_id: "doc_7f3a", chunk_id: "tbl_12"}
