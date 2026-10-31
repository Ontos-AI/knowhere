"""Generate a small synthetic native PDF with facts for reading and Ask checks."""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

PAGE_CONTENT: tuple[tuple[str, str], ...] = (
    (
        "1. Launch operations",
        "The synthetic project is named Amber Otter. Its launch date is "
        "14 November 2026. The launch owner is Morgan Lee. The launch checklist "
        "requires a signed operations review, a documented rollback plan, and "
        "a final support rota. The project is an invented testing example. "
        "Its facts are deliberately repeated in the native PDF text so the "
        "initial reading snapshot can answer the launch-date question before "
        "background enrichment completes.",
    ),
    (
        "2. Support arrangements",
        "Amber Otter support hours are 09:00 to 17:00 UTC from Monday through "
        "Friday. The support coordinator is Casey Tran. Urgent synthetic "
        "incidents go to the duty coordinator. Standard requests receive an "
        "initial response within two business days. The runbook requires "
        "each incident to record its time, impact, owner, and next action. "
        "This page provides a separate section whose Fine state can be "
        "compared with an unread section during the integration test.",
    ),
    (
        "3. Reserve budget",
        "The Amber Otter reserve budget is 42000 USD. Of this amount, "
        "12000 USD is assigned to launch support and 30000 USD remains "
        "unallocated. The finance reviewer is Avery Chen. Reserve spending "
        "requires the launch owner's approval and an entry in the project "
        "ledger. These are invented values for deterministic evidence checks. "
        "Reading and Ask must continue to expose this baseline text if "
        "optional Fine enrichment is delayed or fails.",
    ),
)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments: argparse.Namespace = parser.parse_args()
    output: Path = arguments.output
    if output.exists():
        raise FileExistsError(f"Refusing to replace an existing fixture: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for page_number, (title, content) in enumerate(PAGE_CONTENT, start=1):
            page: pymupdf.Page = document.new_page(width=612, height=792)
            page.insert_text((48, 54), title, fontsize=18)
            remaining_space: float = page.insert_textbox(
                pymupdf.Rect(48, 90, 564, 680), content, fontsize=12
            )
            if remaining_space < 0:
                raise RuntimeError(f"Fixture text did not fit page {page_number}")
            page.insert_text(
                (48, 748), f"Synthetic acceptance fixture - Page {page_number}"
            )
        document.set_toc(
            [
                [1, title, page_number]
                for page_number, (title, _) in enumerate(PAGE_CONTENT, start=1)
            ]
        )
        document.set_metadata({"title": "Amber Otter progressive acceptance fixture"})
        document.save(str(output))
    with pymupdf.open(output) as generated:
        assert len(generated) == len(PAGE_CONTENT)
        assert "14 November 2026" in generated[0].get_text()
        assert "42000 USD" in generated[2].get_text()
    print(f"Created synthetic native PDF with {len(PAGE_CONTENT)} pages at {output}")


if __name__ == "__main__":
    main()
