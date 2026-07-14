# 0005 Local MinerU Codex Export

## Status

Accepted

## Context

Knowhere needs a standalone, local document-conversion path that accepts PDF
and DOCX and produces a portable, provenance-aware review package for Codex.
The current production PDF ingestion path uses MinerU cloud APIs and the native
DOCX parser has different behavior. This milestone must not reroute either
path.

The implementation baseline is Knowhere
`9c523f5a64a65f0a29baf0d8401e8d6b7e038f4a` on branch
`feat/kiwi-shane/local-mineru-codex-export`. The paired MinerU baseline is
`79d6d8d79fb8f3ddba5cc34c07a16f0ec36f56c7`.

## Decision

### Process boundary

The exporter runs MinerU through an argv-list subprocess with `shell=False`.
MinerU executes in its own project environment and calls `do_parse()` directly.
The repositories exchange only a versioned artifact directory and manifest;
Knowhere does not import MinerU internals or absorb MinerU's model dependency
graph.

### Artifact and evidence boundary

Knowhere validates the `knowhere-mineru-artifacts/1.0` manifest, confines all
artifact paths to the declared root, verifies hashes, and parses the required
JSON before use. It then emits deterministic blocks and a navigation-only
document tree, preserves raw MinerU derivatives, exports table HTML with
explicit best-effort CSV fidelity, and renders only selected pages.

Native source files remain authoritative. Extracted text, OCR, Markdown, JSON,
HTML, CSV, normalized PDFs, rendered PNGs, hierarchy, and descriptions are
derivatives. Decision-relevant table values require native visual verification;
generated summaries are not source evidence.

### Local and offline behavior

The standalone command requires no API, Celery, PostgreSQL, Redis, S3, or
LocalStack service. Offline mode rejects remote MinerU backends and server URLs
and requests local-only model loading. Application-level offline flags are not
equivalent to a firewall, so packages report offline verification separately
from the offline request.

### DOCX page semantics

DOCX blocks retain MinerU Office logical-page locators. If visual pages are
requested, LibreOffice creates a derived normalized PDF. Its page numbers are
`normalized_pdf` page numbers and are not automatically mapped to MinerU
logical pages. Rendering may vary with LibreOffice version, fonts, platform,
and printer settings.

### Non-goals

This milestone does not modify default PDF or DOCX ingestion, add LLM page
selection or section summaries, persist packages to retrieval storage, expose a
web UI, or infer approval, status, compliance, equivalence, disposition, or
Pass/Fail results.

## Licensing and attribution

The repositories retain separate histories, environments, and licenses. No
MinerU source is copied into Knowhere. Knowhere's `LICENSE` and `NOTICE` and
MinerU's license and additional terms remain applicable. An online deployment
must evaluate MinerU attribution and other deployment-specific obligations;
this ADR does not make a legal conclusion.

## Consequences

- Parser crashes, timeouts, model lifecycle, and dependency changes are
  isolated behind the subprocess and artifact contract.
- Packages can be inspected without starting Knowhere infrastructure.
- Artifact validation and copying add local disk and hashing cost.
- Table CSV may be lossy, DOCX physical pages are environment-dependent, and
  large documents may require substantial model memory and runtime.

## Implementation notes and deviations

- Both observed Git baselines match the implementation plan.
- MinerU 3.4.4 provides the planned `do_parse`, `read_fn`, and
  `resolve_parse_dir` interfaces and page-indexed `content_list_v2` output.
- Knowhere already provides reusable PyMuPDF page rendering and a safe
  LibreOffice argv-list conversion helper.
- The Windows baseline contract run produced 107 passes. Twenty-six unrelated
  cases require a local PostgreSQL installation, and one POSIX Java-stub test
  is not executable on Windows. A focused, service-free baseline completed with
  23 passes; these pre-existing platform gaps are tracked as verification
  limitations rather than product changes.

