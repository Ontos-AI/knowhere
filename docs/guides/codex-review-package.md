# Build and inspect a local Codex review package

This standalone workflow converts one local PDF or DOCX into a portable review
directory. It runs MinerU as a local child process and does not require the
Knowhere API, worker, Celery, PostgreSQL, Redis, S3, or LocalStack services. It
does not replace Knowhere's production PDF or DOCX ingestion paths.

## Prerequisites

1. Check out and install the paired MinerU project, including the
   `mineru-knowhere-export` adapter.
2. Pre-download the model files required by the selected backend before a
   strict offline run. The supported MVP PDF backend is `pipeline`; DOCX uses
   MinerU's effective `office` backend. Other non-HTTP backends are not
   qualified here.
3. Install LibreOffice only if DOCX pages must be rendered. Structured DOCX
   export can complete without it when no pages are requested.
4. Ensure sufficient local disk, RAM, and, if used, GPU memory. Requirements
   vary with the selected models, page count, resolution, and document
   complexity. CPU execution can be slower; GPU execution needs compatible
   drivers and runtime. No performance or capacity guarantee is implied.

Application offline flags ask known model libraries not to download and reject
MinerU HTTP-client backends and server URLs. They are not equivalent to a host
firewall or an independently verified network-denial control. Use host or
container network controls when that assurance is required; the manifest keeps
`offline.requested` and `offline.verified` separate.

## Run without Knowhere services

From `knowhere/apps/worker`, run:

```bash
uv run python scripts/export_codex_review_package.py \
  --input /absolute/path/to/source.pdf \
  --output ../../.codex-review/source-pdf \
  --mineru-project /absolute/path/to/MinerU \
  --backend pipeline \
  --method auto \
  --lang en \
  --pages 1,2 \
  --include-table-pages \
  --dpi 200 \
  --offline
```

Paths are passed as an argument list, not a shell command. The output must not
already exist unless `--force` is supplied. On failure, the unfinished package
is removed unless `--keep-work-dir` was requested. Do not place confidential
fixtures or completed review packages in the repository; `.codex-review/` and
`review-package/` are ignored.

For DOCX, `--pages` means pages in the LibreOffice-normalized PDF. MinerU Office
logical pages remain distinct and unmapped. Pagination can change with fonts,
LibreOffice version, operating system, and printer settings.

## Package map

- `native/source.pdf` or `native/source.docx`: authoritative source copy.
- `metadata/manifest.json`: source hash, tool provenance, options, counts,
  limitations, and a hash inventory of package artifacts.
- `structured/blocks.jsonl`: deterministic parser-derived blocks and locators.
- `structured/document_tree.json`: deterministic, navigation-only hierarchy.
- `structured/extraction_findings.jsonl`: extraction and fidelity warnings.
- `derivatives/document.md`: MinerU Markdown derivative.
- `raw/mineru/`: validated MinerU artifacts and original manifest.
- `tables/`: preserved HTML, metadata, source image when available, and
  best-effort CSV derivatives.
- `pages/`: selected lossless PNG page renders.
- `CODEX_REVIEW_INSTRUCTIONS.md`: evidence boundaries for the reviewer.

Table HTML is the preserved structural derivative. CSV is explicitly
best-effort: merged cells, row/column spans, nesting, multiple tables, or parser
failures can make it lossy or unavailable. Verify decision-relevant values on
the native source page.

## Provenance and evidence classes

- `parser_extracted` / `source_derivative`: MinerU-extracted text and structure;
  useful for search and navigation, but not a substitute for the source.
- `native_verification_required`: table derivatives and other content whose
  decision-relevant values must be checked against the native document.
- `machine_generated_visual_description` / `navigation_only`: image or chart
  descriptions used to locate content, never as source evidence.
- Normalized PDFs and page PNGs: rendering derivatives. PDF pages retain native
  page numbers; DOCX PNGs use normalized-PDF page numbers.
- The document tree is `navigation_only`; it does not imply approval, status,
  compliance, equivalence, disposition, or a Pass/Fail result.

## Inspect with Codex

Open the completed package directory in Codex and ask it to read
`CODEX_REVIEW_INSTRUCTIONS.md` first. Then inspect
`metadata/manifest.json` for identity and limitations,
`structured/document_tree.json` for navigation, and
`structured/blocks.jsonl` for extracted content. Use table metadata and page
renders to locate evidence, then verify material claims against `native/`.
Check `structured/extraction_findings.jsonl` before relying on any derivative.

## Security and licensing notes

The adapter validates relative artifact paths, resolves symlinks, verifies
SHA-256 hashes, uses `shell=False`, bounds logs, and redacts common secret
forms. It does not require `MINERU_API_KEYS` or start a MinerU HTTP server.

Knowhere's `LICENSE` and `NOTICE` remain in force. MinerU remains under its own
license and additional terms; no MinerU source is copied into Knowhere. If the
combined result is offered as an online service, evaluate MinerU attribution
and other deployment-specific obligations. This guide is not a legal
conclusion, and deployment-specific legal review may be required.
