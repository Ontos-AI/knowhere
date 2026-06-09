# Contributing

Thanks for contributing to Knowhere. The project is split across several repositories — make sure you're working in the right one.

## Ecosystem

| Repository | Description |
|---|---|
| [knowhere](https://github.com/Ontos-AI/knowhere) | **This repo.** Backend API and worker — document ingestion, parsing, graph construction, and retrieval. |
| 🖥️ [knowhere-dashboard](https://github.com/Ontos-AI/knowhere-dashboard) | The web UI. Connects to the API for the full product experience. |
| 🐳 [knowhere-self-hosted](https://github.com/Ontos-AI/knowhere-self-hosted) | Docker Compose stack for self-hosted deployments. Packages the API, worker, and dashboard together. |
| 🐍 [knowhere-python-sdk](https://github.com/Ontos-AI/knowhere-python-sdk) | Official Python SDK for the Knowhere Cloud API. |
| 🦕 [knowhere-node-sdk](https://github.com/Ontos-AI/knowhere-node-sdk) | Official Node.js SDK for the Knowhere Cloud API. |

## Before You Start

- Open or confirm an issue before starting significant changes.
- Keep changes scoped and reviewable.
- Do not commit real secrets, deployment credentials, or environment-specific
  private data.

## Branching

- Do not push directly to protected branches.
- Use `main` as the default source branch and pull request target.
- Use a dedicated feature or fix branch for each change.
- Name branches as `<type>/<user>/<description>`.
- Use a lowercase `type`, preferably one of `feat`, `fix`, `refactor`,
  `chore`, `docs`, `test`, `perf`, `ci`, `build`, or `revert`.
- Use the human owner or contributor name for `user`; do not use a generic
  tool name such as `codex`.
- Keep `description` short, lowercase, and kebab-case, for example
  `refactor/alice/extract-chunk-converter`.
- If you are working on publication cleanup, keep migration-only changes on the
  dedicated migration branch instead of flowing them back into normal private
  development by default.

## Development Flow

`main` is the public development trunk. Contributors and maintainers develop
against `main`.

### Contributor workflow

1. Fork the repository or create a branch from the latest `main`.
2. Use a dedicated branch such as `docs/alice/add-faq` or
   `fix/alice/retrieval-timeout`.
3. Open the pull request against `main`.
4. Wait for review and required checks.
5. Keep the pull request focused; split unrelated changes into separate pull
   requests.

Pull requests to `main` run CI, secret scanning, and CodeQL.

### Maintainer development workflow

Internal changes follow the same trunk workflow:

1. Create a branch from `main`.
2. Open a pull request to `main`.
3. Merge after review and green checks.

Do not ask external contributors to target internal environment branches for
normal changes. `main` is the public contribution trunk.

## Development Setup

Sync the Python services and shared package:

```bash
cd packages/shared-python && uv sync
cd ../../apps/api && uv sync
cd ../worker && uv sync
```

Start the local services:

```bash
cd deploy/local-dev && ./start-dev.sh
```

## Pull Requests

- Write a clear title and summary.
- Explain API, schema, or behavior changes explicitly.
- Call out migration, workflow, and documentation impacts.
- Add or update tests when behavior changes.
- Keep documentation aligned with the implementation.
