"""Document domain taxonomy based on IPTC Media Topics (top level only).

Source: IPTC NewsCodes Media Topics (https://cv.iptc.org/newscodes/mediatopic/).
Only the 17 official top-level topics are listed; the model picks menu numbers and
we persist the stable Media Topic codes (e.g. ``04000000``). ``OTHER`` is our
local escape hatch when none of the listed topics fit.
"""

from __future__ import annotations

from dataclasses import dataclass

OTHER_CODE = "OTHER"
ECONOMY_BUSINESS_FINANCE_CODE = "04000000"


@dataclass(frozen=True)
class DomainLeaf:
    code: str
    title: str


@dataclass(frozen=True)
class DomainSection:
    title: str
    leaves: tuple[DomainLeaf, ...]


def _section(title: str, *leaves: tuple[str, str]) -> DomainSection:
    return DomainSection(title, tuple(DomainLeaf(code, name) for code, name in leaves))


# Official IPTC Media Topics top-level codes (medtop:NNNNNNNN → NNNNNNNN).
DOMAIN_SECTIONS: tuple[DomainSection, ...] = (
    _section(
        "IPTC Media Topics",
        ("01000000", "Arts, culture, entertainment and media"),
        ("16000000", "Conflict, war and peace"),
        ("02000000", "Crime, law and justice"),
        ("03000000", "Disaster, accident and emergency incident"),
        (ECONOMY_BUSINESS_FINANCE_CODE, "Economy, business and finance"),
        ("05000000", "Education"),
        ("06000000", "Environment"),
        ("07000000", "Health"),
        ("08000000", "Human interest"),
        ("09000000", "Labor"),
        ("10000000", "Lifestyle and leisure"),
        ("11000000", "Politics and government"),
        ("12000000", "Religion"),
        ("13000000", "Science and technology"),
        ("14000000", "Society"),
        ("15000000", "Sport"),
        ("17000000", "Weather"),
        (OTHER_CODE, "Other (no listed topic fits)"),
    ),
)

_CODE_BY_ID: dict[int, str] = {
    number: leaf.code
    for number, leaf in enumerate(
        (leaf for section in DOMAIN_SECTIONS for leaf in section.leaves),
        start=1,
    )
}


def _render_menu() -> str:
    lines: list[str] = []
    number = 1
    for section in DOMAIN_SECTIONS:
        if section.title:
            lines.append(section.title)
        for leaf in section.leaves:
            lines.append(f"{number}. {leaf.title}")
            number += 1
    return "\n".join(lines)


DOMAIN_MENU = _render_menu()


def domain_codes_from_ids(ids: object) -> list[str]:
    """Map model-picked menu numbers to stable domain codes.

    Raises ``ValueError`` for anything that is not a non-empty list of known,
    distinct numbers, or when ``OTHER`` is combined with other picks.
    """
    if not isinstance(ids, list) or not ids:
        raise ValueError("domain_ids must be a non-empty list")
    codes: list[str] = []
    for number in ids:
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"domain id must be an integer: {number!r}")
        if number not in _CODE_BY_ID:
            raise ValueError(f"unknown domain id: {number}")
        codes.append(_CODE_BY_ID[number])
    if len(set(codes)) != len(codes):
        raise ValueError("domain_ids contains duplicates")
    if OTHER_CODE in codes and len(codes) > 1:
        raise ValueError(f"{OTHER_CODE} cannot be combined with other domains")
    return codes


__all__ = [
    "DOMAIN_MENU",
    "DOMAIN_SECTIONS",
    "ECONOMY_BUSINESS_FINANCE_CODE",
    "OTHER_CODE",
    "DomainLeaf",
    "DomainSection",
    "domain_codes_from_ids",
]
