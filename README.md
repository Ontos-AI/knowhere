<h1 align="center">Prepare unstructured data for AI Agents</h1>

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

Knowhere is the open-source infrastructure for unstructured data processing. It automates the complex pipeline of extracting, parsing, and transforming messy documents into structured, high-quality data optimized for *AI Agents*, *Agentic RAG*, and *traditional vector-based RAG workflows*.

> [!NOTE]
> **Get started in seconds with Knowhere Cloud.**
> Avoid the complexity of self-deployment. Use our managed API at [knowhereto.ai](https://knowhereto.ai) and enjoy **$5 in free credits** upon registration.

## 📢 News

- **May 7, 2026**: 🚀 **Knowhere is now Open Source!** We have open-sourced our entire stack for document ingestion, parsing, and agentic RAG. You can now self-host the full platform using [knowhere-self-hosted](https://github.com/Ontos-AI/knowhere-self-hosted). Check out our [Contribution Guide](CONTRIBUTING.md) to get involved!
- **Apr 30, 2026**: 📦 **Version [2026.04.30.1](https://github.com/Ontos-AI/knowhere/releases/tag/2026.04.30.1) has been released.** This update includes several stability improvements and initial support for the agentic RAG layer. See the [full changelog](https://github.com/Ontos-AI/knowhere/commits/2026.04.30.1) for details.

## How it Works

> [!TIP]
> **TL;DR**: Knowhere builds navigable memory from messy documents, then lets agents retrieve and cite evidence from that memory.

Knowhere turns raw documents into a structured memory store that AI agents can navigate and cite. The process follows two steps:

### Step 1: Parse and Build Memory

<p align="center">
  <img alt="Step 1: Parse and Build Memory" src="docs/assets/step-1-parse-build-memory.png" width="900">
</p>

Parsing, chunking, hierarchy extraction, and graph construction are unified into one outcome: a navigable memory layer for AI agents.

- **Parse**: Route PDFs, Office files, images, tables, Markdown, and text to specialized parsers.
- **Structure**: Preserve headings, section paths, multi-modal assets, and chunk relationships.
- **Build Memory**: Store chunks, navigation trees, summaries, and graph links as agent-ready context.

### Step 2: Agentic Retrieval

<p align="center">
  <img alt="Step 2: Agentic Retrieval" src="docs/assets/step-2-agentic-retrieval.png" width="900">
</p>

Agents retrieve by navigating memory instead of depending on a single flat vector lookup.

- **Discover**: Fuse keyword, path, content, and semantic signals for broad first-pass coverage.
- **Navigate**: Walk section trees and graph links to drill into the most relevant document regions.
- **Cite Evidence**: Return traceable results with source document, section, chunk, and linked image or table assets.

## Ecosystem

| Repository | Description |
|---|---|
| [knowhere](https://github.com/Ontos-AI/knowhere) | **This repo.** Backend API and worker — document ingestion, parsing, graph construction, and retrieval. |
| 🖥️ [knowhere-dashboard](https://github.com/Ontos-AI/knowhere-dashboard) | The web UI. Connects to the API for the full product experience. |
| 🐳 [knowhere-self-hosted](https://github.com/Ontos-AI/knowhere-self-hosted) | Docker Compose stack for self-hosted deployments. Packages the API, worker, and dashboard together. |
| 🐍 [knowhere-python-sdk](https://github.com/Ontos-AI/knowhere-python-sdk) | Official Python SDK for the Knowhere Cloud API. |
| 🦕 [knowhere-node-sdk](https://github.com/Ontos-AI/knowhere-node-sdk) | Official Node.js SDK for the Knowhere Cloud API. |

## Features

- **Multi-modal Parsing**: High-fidelity extraction from PDF, Office, and images, preserving headings, tables, and hierarchical paths.
- **Lightweight Memory Graph**: Context-aware organization that links documents and chunks for better relationship understanding.
- **Agentic RAG**: A hybrid retrieval engine combining traditional search (RRF) with autonomous agent navigation.
- **Evidence-based Citations**: Every result is backed by traceable source paths, ensuring reliability for AI Agent decision-making.

## Supported Formats

**✅ Supported**

- [x] `.pdf` `.docx` `.pptx` `.xlsx` `.csv`
- [x] `.jpg` `.png`
- [x] `.md` `.txt` `.json`

**⏳ Coming Soon**

- [ ] `.epub` `.html` `.xml`
- [ ] `.mp4` `.mp3`
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
- `MINERU_API_KEYS` if you need PDF parsing
- a vision-capable model provider if you need image summaries, OCR, atlas classification, or image-aware retrieval
- any optional billing or webhook providers you want to enable

Most parser and retrieval tuning values have code defaults. Start with the
required external services first, then override model names, provider URLs,
budgets, or concurrency limits only when your deployment needs different
behavior. See [docs/external-services.md](docs/external-services.md) for the
full dependency matrix.

4. Start the local infrastructure stack:

```bash
./deploy/local-dev/start-dev.sh
```

5. Start the API and worker in separate terminals:

```bash
cd apps/api && uv run main.py
cd apps/worker && uv run worker.py
```

The API runs migrations during startup.

For API-only development without the dashboard, create an API-only user/key
after the API service starts:

```bash
cd apps/api
uv run scripts/init_user.py --email you@example.com
```

If you plan to use the dashboard, register through the dashboard instead of
using `scripts/init_user.py`.

The API is now running at `http://localhost:5005`. If you want the full product experience with a UI, run the [knowhere-dashboard](https://github.com/Ontos-AI/knowhere-dashboard) alongside it — it connects to this API out of the box.

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

- External dependency guide:
  [docs/external-services.md](docs/external-services.md)

## Citation

If you use Knowhere in your research, please cite it as:

```bibtex
@software{knowhere2026,
  author       = {Ontos AI},
  title        = {Knowhere: Prepare Unstructured Data for AI Agents},
  year         = {2026},
  publisher    = {GitHub},
  url          = {https://github.com/Ontos-AI/knowhere},
  version      = {2026.04.30.1},
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

- [SECURITY.md](SECURITY.md) — how to report vulnerabilities responsibly.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community behavior expectations.
- [LICENSE](LICENSE) and [NOTICE](NOTICE) — Apache 2.0.

## 👋 We're Hiring!

We're building the knowledge layer for the Agent era. If that sounds like work you want to do, reach out — decode the address below and drop us a line:

```bash
echo 'dGVhbUBrbm93aGVyZXRvLmFp' | base64 --decode
```
