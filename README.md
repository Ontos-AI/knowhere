<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/knowhere-banner-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/knowhere-banner-light.jpg">
    <img width="1000" alt="Knowhere — Prepare unstructured data for AI agents" src="docs/assets/knowhere-banner-light.jpg" />
  </picture>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img alt="Python Version" src="https://img.shields.io/badge/Python-%3E%3D%203.11-3776AB.svg?style=for-the-badge&logo=python&logoColor=white&labelColor=000000">
  </a>
  <a href="https://github.com/Ontos-AI/knowhere/stargazers">
    <img alt="GitHub stars" src="https://img.shields.io/github/stars/ontos-ai/knowhere?style=for-the-badge&logo=github&labelColor=000000">
  </a>
  <a href="https://github.com/Ontos-AI/knowhere/actions">
    <img alt="Build Status" src="https://img.shields.io/github/actions/workflow/status/Ontos-AI/knowhere/pr-ci.yml?style=for-the-badge&labelColor=000000">
  </a>
  <br>
  <a href="https://github.com/Ontos-AI/knowhere/discussions">
    <img alt="Join the community on GitHub" src="https://img.shields.io/badge/Join%20the%20community-blueviolet.svg?style=for-the-badge&logo=GitHub&labelColor=000000&logoWidth=20">
  </a>
  <a href="https://ghcr.io/ontos-ai/knowhere">
    <img alt="Container Images" src="https://img.shields.io/badge/CONTAINER%20IMAGES-2496ED.svg?style=for-the-badge&logo=docker&logoColor=white&labelColor=000000">
  </a>
  <a href="https://github.com/Ontos-AI/knowhere/blob/main/LICENSE">
    <img alt="License: Apache 2.0" src="https://img.shields.io/badge/APACHE%202.0-D97706.svg?style=for-the-badge&label=LICENSE&labelColor=000000">
  </a>
</p>

<p align="center">
  🔗 <a href="https://knowhereto.ai">Website</a> |
  📄 <a href="https://docs.knowhereto.ai/">Docs</a> |
  🏠 <a href="https://github.com/Ontos-AI/knowhere-self-hosted">Self-Host</a> |
  🖥️ <a href="https://github.com/Ontos-AI/knowhere-dashboard">Dashboard</a>
</p>

## Overview

**Knowhere is a document intelligence and retrieval infrastructure that turns complex, dirty files into persistent, navigable memory for AI agents.**

It ingests unstructured documents and produces persistent, navigable memory: parsing, hierarchy reconstruction, multi-modal structuring, and graph construction in a single pipeline. Every result stays connected to its document, section, source pages, and related assets, making the output a natural fit for *Agentic RAG*, *vector-based RAG*, or any LLM workflow.

Knowhere parses files along their native hierarchy (headings, if any) and uses frontier vision models to cut the extraction errors of traditional parsers. Documents can be ingested progressively: readable and searchable first, enriched in the background. This progressive feature enables fast response in agent systems.

> [!NOTE]
> **Get started in seconds with Knowhere Cloud.**
> Avoid the complexity of self-deployment. Use our managed API at [knowhereto.ai](https://knowhereto.ai) and enjoy **$5 in free credits** upon registration.

## 📢 News

- **November 2026**: ⏩ **Progressive ingestion.** `ingestion_mode=progressive` publishes a readable baseline first, then enriches the rest in the background. MinerU is no longer required by default.
- **November 2026**: 🧰 **Agent retrieval tools exposed through the API.** Corpus navigation tools are exposed to built-in and external agents alike.
- **November 2026**: 🖼️ **More image formats.** `.gif`, `.webp`, `.bmp`, `.tif`/`.tiff`, and `.heic`/`.heif` are now supported.
- **September 8, 2026**: 🧭 **Introducing agent-native Retrieval 2.0.** Agents explore the corpus with hierarchy-aware tools (recall, grep, outline, read) and cite resolvable evidence.
- **June 1, 2026**: 📚 **Knowhere supports ultra-long PDFs and atlases.** It can process long-form PDFs (e.g., over 1000) and route technical atlases or drawing collections through a dedicated layout-aware parser.
- **May 7, 2026**: 🚀 **Knowhere is now Open Source!** We have open-sourced our entire stack for document ingestion, parsing, and agentic RAG. You can now self-host the full platform using [knowhere-self-hosted](https://github.com/Ontos-AI/knowhere-self-hosted). Check out our [Contribution Guide](CONTRIBUTING.md) to get involved!

## Document Parsing: By Format, Progressively

Traditional OCR and Document Intelligence pipelines try to extract every element before a model can understand the document. On dirty PDFs and slide decks, mistakes in reading order, layout, tables, or hidden text layers can accumulate into unreliable model context.

Knowhere does not make perfect element-by-element extraction a prerequisite for retrieval. Native parsing preserves precise text and structure where they are reliable, and vision parsing uses frontier vision models to understand a page or slide as a whole, so visually complex content can still be recalled and understood without first reconstructing every element.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/step-1-parse-and-publish-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/step-1-parse-and-publish.png">
    <img alt="Documents are routed by format, published progressively, and converge into one navigable memory" src="docs/assets/step-1-parse-and-publish.png" width="1000">
  </picture>
</p>

- **Route by format**: Page-based documents (e.g., PDF and PowerPoint) use vision parsing; everything else is parsed natively. Both end up in the same chunk and metadata schema, so nothing downstream depends on the source format.
- **Progressive ingestion**: With `ingestion_mode=progressive`, a document becomes readable and searchable first, then gains its outline and hierarchy, and is finally enriched in the background, with bottom-level nodes enriched on demand. Full ingestion, which publishes once parsing completes, is the default.
- **Recall without brittle reconstruction**: Pages can be indexed and searched through summaries, entities, source text (if any), and hierarchy even when OCR or layout extraction cannot reliably recover every component.
- **One navigable memory**: Text sections and vision pages become compatible hierarchy nodes with source evidence, linked assets, and cross-document relationships.

## How it Works

Knowhere runs in two steps: build memory from documents, then let agents retrieve from it.

### Step 1: Parse and Build Memory

- **Route**: Select vision parsing or native parsing according to the document format.
- **Understand**: Preserve native text structure where it is reliable, or understand complex pages holistically with a vision model.
- **Publish**: Publish a readable baseline first for progressive jobs, or publish once parsing completes for full jobs.
- **Build Memory**: Normalize everything into hierarchy-native chunks and store navigation trees, linked assets, citations, and cross-document relationships as agent-ready context.

### Step 2: Agentic Retrieval

Knowhere provides the document-memory substrate; the agent decides how to explore it.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/step-2-routed-retrieval-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/step-2-routed-retrieval.png">
    <img alt="A query router chooses classic or agent retrieval, and any external agent can call the same corpus tools directly" src="docs/assets/step-2-routed-retrieval.png" width="1000">
  </picture>
</p>

- **Route per query**: Unless a request sets `use_agentic`, a query router chooses classic BM25 or agent retrieval for each query.
- **Provide the substrate**: Knowhere exposes one corpus schema and the `corpus.*` tools for exact search, fuzzy recall, outlines, full reading, table queries, and assets.
- **Let the agent explore**: Instead of forcing every query through a fixed navigation pipeline, the agent chooses which tools to call, in what order, and how deeply to traverse.
- **Stay agent-neutral**: External agents call the same tools directly through MCP or `POST /v2/retrieval/tools/invoke`, without running Knowhere's own retrieval agent. Whichever agent explores the memory, Knowhere resolves its references into traceable documents, sections, pages, and linked assets.

## FAQ

**Q: What is Knowhere's relationship with MinerU?**

A: PDF and PowerPoint use vision parsing: Knowhere renders source pages, combines visual interpretation with document profiling and the native hierarchy, and assembles page-grounded hierarchy nodes. MinerU is an optional fine-stage page-text extractor and is off by default. Knowhere does not treat parser-generated Markdown as the source of truth.

**Q: What LLM / VLM dependencies does Knowhere have?**

A: We recommend [`deepseek-flash`](https://api-docs.deepseek.com/guides/vision/) as a unified model for both Text and Vision workloads. It accepts text and image input, so the same model can handle summarization, hierarchy reasoning, page understanding, and asset descriptions. Knowhere remains model-agnostic: you can use another model—or separate Text and Vision models—from OpenAI, Qwen, GLM, Volcengine, or any compatible provider.

**Q: How is Agentic Retrieval different from traditional RAG?**

A: Traditional RAG does a flat vector lookup and returns isolated snippets. Knowhere's agents navigate the document's section tree and cross-document graph, drilling into the most relevant regions the way a human reader would, returning traceable, well-contextualized evidence.

**Q: Does it handle images and tables?**

A: Yes. Knowhere extracts images and tables, runs them through VLM-assisted summarization and feature extraction, and links them back to their source section nodes. Vision parsing also retains rendered page citations, so agents can return both structured context and the visual source evidence.

## Performance Benchmark

Agents using Knowhere outperform those working from raw documents, Markitdown, Unstructured, or MinerU output on real-world tasks: searching, modifying, and answering questions.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/benchmark-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/benchmark-light.png">
    <img alt="Benchmark Performance: Agent + Knowhere vs Others" src="docs/assets/benchmark-light.png" width="900">
  </picture>
</p>

> **We're not developing the next MinerU — we're building document memory infrastructure that agents can effectively consume.**

### Key Advantages

- **Accuracy**: +36% first-try accuracy and +11% recall over raw documents.
- **Reliability**: 79% accuracy with feedback, vs. a ~53% ceiling on raw docs.
- **Efficiency**: Fewer loops, fewer tokens, less time. Agents navigate a structured graph instead of reading monolithic text.

*(Internal evaluation across identical agentic RAG tasks. Baselines: raw documents and parser output fed directly to agents.)*

> [!NOTE]
> **📊 Benchmarks are actively expanding.** More parsers and retrieval baselines coming soon.

## Ecosystem

| Repository | Description |
|---|---|
| [knowhere](https://github.com/Ontos-AI/knowhere) | **This repo.** Backend API and worker: document ingestion, parsing, graph construction, and retrieval. |
| 🖥️ [knowhere-dashboard](https://github.com/Ontos-AI/knowhere-dashboard) | The web UI. Connects to the API for the full product experience. |
| 🐳 [knowhere-self-hosted](https://github.com/Ontos-AI/knowhere-self-hosted) | Docker Compose stack for self-hosted deployments. Packages the API, worker, and dashboard together. |
| 🐍 [knowhere-python-sdk](https://github.com/Ontos-AI/knowhere-python-sdk) | Official Python SDK for the Knowhere Cloud API. |
| 🦕 [knowhere-node-sdk](https://github.com/Ontos-AI/knowhere-node-sdk) | Official Node.js SDK for the Knowhere Cloud API. |

## Features

- **Progressive Ingestion**: Jobs with `ingestion_mode=progressive` publish a usable baseline before background enrichment completes.
- **Format-aware Parsing**: Vision and native parsing handle different document conditions while producing the same downstream schema.
- **Vision Parsing**: Frontier vision models make complex PDF and PowerPoint content recallable without requiring perfect element-by-element OCR or layout reconstruction.
- **Hierarchy-native Memory**: Section nodes preserve document paths, page ranges, summaries, entities, and linked assets instead of returning disconnected chunks.
- **Cross-document Memory Graph**: Page-derived typed entities and keywords connect related documents across a namespace.
- **Agent-native Retrieval**: Built-in and MCP-connected agents explore the same corpus schema and hierarchy-aware tools; classic retrieval remains available for deterministic top-K search.
- **Page-grounded Citations**: Results retain source documents, section paths, page numbers, and rendered visual evidence.

## Supported Formats

**✅ Supported**

- [x] `.pdf` `.pptx` — vision parsing
- [x] `.doc` `.docx` `.xls` `.xlsx` `.csv` `.tsv`
- [x] `.jpg` `.jpeg` `.png` `.gif` `.webp` `.bmp` `.tif` `.tiff` `.heic` `.heif`
- [x] `.md` `.txt` `.html` `.htm` `.json`
- [x] `.mp3` `.wav` `.m4a` `.aac` `.flac` `.ogg` `.opus` `.mp4` `.mov` `.webm` `.mkv` — speech transcription with speakers, chapters, and time-range citations (video: audio track only)

**⏳ Coming Soon**

- [ ] `.epub` `.xml`
- [ ] `.srt` `.vtt`
- [ ] `.skills.md`

Want to see a new format supported? Adding a parser is a great first contribution. Check out [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

## Prerequisites

- Python 3.11+
- `uv`
- Docker with `docker compose`

## Quick Start

1. Sync the workspace dependencies:

```bash
uv sync --all-packages
```

2. Copy the environment examples:

```bash
cp apps/api/.env.example apps/api/.env
cp apps/worker/.env.example apps/worker/.env
```

3. Update the copied `.env` files with the values you need for local work:

- database and Redis connection settings
- S3-compatible storage credentials
- at least one LLM provider key: `DS_KEY`, `ALI_API_KEYS`, `GPT_API_KEY`, or `GLM_API_KEY`
- `VERCEL_KEY` for page-image and query embeddings
- a vision-capable model provider for PDF/PowerPoint parsing, page understanding, image summaries, OCR, atlas classification, or image-aware retrieval
- `MINERU_API_KEYS` only if you turn on the optional MinerU fine-stage page-text extractor
- any optional billing or webhook providers you want to enable

Most parser and retrieval tuning values have code defaults. Start with the
required external services first, then override model names, provider URLs,
budgets, or concurrency limits only when your deployment needs different
behavior.

4. Start the local infrastructure stack:

```bash
./deploy/local-dev/start-dev.sh
```

5. Run migrations with the local management role. The infrastructure script
provisions `knowhere_runtime`; the API/Worker environment examples use that
non-superuser role so shared demo row-level security is enforced.

```bash
cd apps/api
MIGRATION_DATABASE_URL=postgresql+asyncpg://root:root123@localhost:5432/Knowhere \
  uv run alembic upgrade heads
```

6. Start the API and worker in separate terminals:

```bash
cd apps/api && uv run main.py
cd apps/worker && uv run worker.py
```

For API-only development without the dashboard, create an API-only user/key
after the API service starts:

```bash
cd apps/api
uv run scripts/init_user.py --email you@example.com
```

If you plan to use the dashboard, register through the dashboard instead of
using `scripts/init_user.py`.

The API is now running at `http://localhost:5005`. If you want the full product experience with a UI, run the [knowhere-dashboard](https://github.com/Ontos-AI/knowhere-dashboard) alongside it; it connects to this API out of the box.

## Quality Checks

Run lint checks from the repository root:

```bash
make lint
```

Apply safe Ruff fixes:

```bash
make lint-fix
```

Run type checks across the API, worker, and shared source code:

```bash
make typecheck
```

Run both lint and type checks:

```bash
make check
```

## Local Endpoints

- API: `http://localhost:5005`
- OpenAPI docs: `http://localhost:5005/docs`
- LocalStack: `http://localhost:4566`
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

## Additional Guides

- Retrieval document scope:
  [docs/retrieval-document-scope.md](docs/retrieval-document-scope.md)
- Architecture decisions:
  [docs/adr/README.md](docs/adr/README.md)

## Telemetry

Self-hosted Knowhere emits **anonymous** product telemetry to PostHog so Ontos
operators can understand OSS adoption (install liveness, usage aggregates,
client/document mix). Events never include filenames, prompts, emails, IPs, or
geo. Schema and allowlists are locked in
[ADR-0004](docs/adr/0004-anonymous-self-hosted-telemetry.md).

Telemetry is **default-on**. To opt out, set:

```bash
TELEMETRY_ENABLED=false
```

Related settings live in `apps/api/.env.example` under `TELEMETRY_*`.

## Citation

If you use Knowhere in your research, please cite it as:

```bibtex
@software{knowhere2026,
  author       = {Ontos AI},
  title        = {Knowhere: Prepare Unstructured Data for AI Agents},
  year         = {2026},
  publisher    = {GitHub},
  url          = {https://github.com/Ontos-AI/knowhere},
  version      = {1.2.51},
  license      = {Apache-2.0}
}
```

## Communication

- [GitHub Discussions](https://github.com/Ontos-AI/knowhere/discussions) for questions, ideas, and general conversation.
- [GitHub Issues](https://github.com/Ontos-AI/knowhere/issues) for bug reports and feature requests.

## Contribution

Any contributions to Knowhere are more than welcome!

If you are new to the project, check out the [good first issues](https://github.com/Ontos-AI/knowhere/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22). They are well-defined, relatively simple, and a great way to get familiar with the codebase and the contribution workflow.

For general guidelines on branching, commit conventions, and the review process, take a look at [CONTRIBUTING.md](CONTRIBUTING.md).

Other useful references:

- [SECURITY.md](SECURITY.md): how to report vulnerabilities responsibly.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): community behavior expectations.
- [LICENSE](LICENSE) and [NOTICE](NOTICE): Apache 2.0.

## 👋 We're Hiring!

We're building the knowledge layer for the Agent era. If that sounds like work you want to do, reach out. Decode the address below and drop us a line:

```bash
echo 'dGVhbUBrbm93aGVyZXRvLmFp' | base64 --decode
```
