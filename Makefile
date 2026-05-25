.PHONY: lint lint-fix typecheck check test-doc-agent

UV := uv
REPO_UV_CACHE_DIR := $(CURDIR)/.uv-cache
USE_REPO_UV_CACHE := $(shell cache_root="$${XDG_CACHE_HOME:-$$HOME/.cache}"; if [ -d "$$cache_root/uv" ]; then [ -w "$$cache_root/uv" ] && printf no || printf yes; elif [ -d "$$cache_root" ]; then [ -w "$$cache_root" ] && printf no || printf yes; elif [ -n "$$HOME" ] && [ -d "$$HOME" ]; then [ -w "$$HOME" ] && printf no || printf yes; else printf yes; fi)

ifeq ($(strip $(UV_CACHE_DIR)),)
ifeq ($(USE_REPO_UV_CACHE),yes)
UV_RUN_ENV := UV_CACHE_DIR=$(REPO_UV_CACHE_DIR)
endif
else
UV_RUN_ENV := UV_CACHE_DIR=$(UV_CACHE_DIR)
endif

UV_RUN := $(UV_RUN_ENV) $(UV) run --all-packages
RUN_LINT := $(UV_RUN) --group lint
RUN_TYPECHECK := $(UV_RUN) --group typecheck
RUFF := $(RUN_LINT) ruff
PYRIGHT := $(RUN_TYPECHECK) pyright
PYRIGHT_PATHS := apps/api/app apps/api/main.py apps/worker/app apps/worker/worker.py packages/shared-python/shared

lint:
	$(RUFF) check apps packages

lint-fix:
	$(RUFF) check --fix apps packages

typecheck:
	$(PYRIGHT) --project pyproject.toml $(PYRIGHT_PATHS)

check: lint typecheck

test-doc-agent:
	cd apps/worker && $(UV_RUN_ENV) $(UV) run pytest tests/document_agent
