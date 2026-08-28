from __future__ import annotations

from pathlib import Path


def test_backfill_script_resolves_shared_package_from_runtime_image_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_indexes import _resolve_shared_root

    api_root = tmp_path / "app"
    shared_root = api_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root


def test_backfill_script_resolves_shared_package_from_source_checkout_layout(
    tmp_path: Path,
) -> None:
    from scripts.backfill_map_unit_indexes import _resolve_shared_root

    repository_root = tmp_path / "repository"
    api_root = repository_root / "apps" / "api"
    shared_root = repository_root / "packages" / "shared-python"
    shared_root.mkdir(parents=True)

    assert _resolve_shared_root(api_root) == shared_root
