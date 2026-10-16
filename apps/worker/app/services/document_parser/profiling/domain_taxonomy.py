"""Document domain taxonomy based on UN ISIC Rev.5.

Leaves are ISIC divisions (2-digit); finance, professional/scientific and
health are expanded to groups/classes because documents there are common and
dense. The model picks leaf numbers from the rendered menu; only the stable ISIC
code of each pick is persisted, so renumbering the menu never changes stored data.
"""

from __future__ import annotations

from dataclasses import dataclass

OTHER_CODE = "OTHER"


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


DOMAIN_SECTIONS: tuple[DomainSection, ...] = (
    _section(
        "Agriculture, forestry and fishing",
        ("01", "Crop and animal production"),
        ("02", "Forestry and logging"),
        ("03", "Fishing and aquaculture"),
    ),
    _section(
        "Mining and quarrying",
        ("05", "Coal and lignite"),
        ("06", "Crude petroleum and natural gas"),
        ("07", "Metal ores"),
        ("08", "Other mining and quarrying"),
        ("09", "Mining support services"),
    ),
    _section(
        "Manufacturing",
        ("10", "Food products"),
        ("11", "Beverages"),
        ("12", "Tobacco products"),
        ("13", "Textiles"),
        ("14", "Wearing apparel"),
        ("15", "Leather and related products"),
        ("16", "Wood and cork products"),
        ("17", "Paper and paper products"),
        ("18", "Printing and recorded media"),
        ("19", "Coke and refined petroleum products"),
        ("20", "Chemicals and chemical products"),
        ("21", "Pharmaceutical products"),
        ("22", "Rubber and plastic products"),
        ("23", "Non-metallic mineral products"),
        ("24", "Basic metals"),
        ("25", "Fabricated metal products"),
        ("26", "Computer, electronic and optical products"),
        ("27", "Electrical equipment"),
        ("28", "Machinery and equipment"),
        ("29", "Motor vehicles, trailers and semi-trailers"),
        ("30", "Other transport equipment"),
        ("31", "Furniture"),
        ("32", "Other manufacturing"),
        ("33", "Repair and installation of machinery and equipment"),
    ),
    _section(
        "Electricity, gas, steam and air conditioning supply",
        ("35", "Electricity, gas, steam and air conditioning supply"),
    ),
    _section(
        "Water supply, sewerage, waste management and remediation",
        ("36", "Water collection, treatment and supply"),
        ("37", "Sewerage"),
        ("38", "Waste collection, treatment, disposal and recovery"),
        ("39", "Remediation and other waste management services"),
    ),
    _section(
        "Construction",
        ("41", "Construction of buildings"),
        ("42", "Civil engineering"),
        ("43", "Specialized construction activities"),
    ),
    _section(
        "Wholesale and retail trade",
        ("46", "Wholesale trade"),
        ("47", "Retail trade"),
    ),
    _section(
        "Transportation and storage",
        ("49", "Land transport and pipelines"),
        ("50", "Water transport"),
        ("51", "Air transport"),
        ("52", "Warehousing and transport support activities"),
        ("53", "Postal and courier activities"),
    ),
    _section(
        "Accommodation and food service",
        ("55", "Accommodation"),
        ("56", "Food and beverage service"),
    ),
    _section(
        "Publishing, broadcasting and content production",
        ("58", "Publishing"),
        ("59", "Film, video, television and music production"),
        ("60", "Broadcasting, news agencies and content distribution"),
    ),
    _section(
        "Telecommunications, computer programming and information services",
        ("61", "Telecommunications"),
        ("62", "Computer programming and consultancy"),
        ("63", "Computing infrastructure, data processing and information services"),
    ),
    _section(
        "Financial and insurance activities",
        ("641", "Monetary intermediation and banking"),
        ("642", "Holding companies and financing conduits"),
        ("643", "Trusts, funds and similar financial entities"),
        ("649", "Other financial services (credit, leasing, factoring)"),
        ("651", "Insurance"),
        ("652", "Reinsurance"),
        ("653", "Pension funding"),
        ("6611", "Administration of financial markets"),
        ("6612", "Securities and commodity contracts brokerage"),
        ("6619", "Other activities auxiliary to financial services"),
        ("662", "Activities auxiliary to insurance and pension funding"),
        ("663", "Fund management"),
    ),
    _section(
        "Real estate activities",
        ("68", "Real estate activities"),
    ),
    _section(
        "Professional, scientific and technical activities",
        ("691", "Legal activities"),
        ("692", "Accounting, auditing and tax consultancy"),
        ("701", "Head offices"),
        ("702", "Management consultancy"),
        ("711", "Architecture, engineering and technical consultancy"),
        ("712", "Technical testing and analysis"),
        ("721", "Research and development in natural sciences and engineering"),
        ("722", "Research and development in social sciences and humanities"),
        ("731", "Advertising"),
        ("732", "Market research and public opinion polling"),
        ("741", "Specialized design"),
        ("742", "Photography"),
        ("743", "Translation and interpretation"),
        ("749", "Other professional, scientific and technical activities"),
        ("75", "Veterinary activities"),
    ),
    _section(
        "Administrative and support services",
        ("77", "Rental and leasing"),
        ("78", "Employment activities"),
        ("79", "Travel agencies and tour operators"),
        ("80", "Investigation and security"),
        ("81", "Services to buildings and landscape"),
        ("82", "Office administration and business support"),
    ),
    _section(
        "Public administration and defence",
        ("84", "Public administration, defence and compulsory social security"),
    ),
    _section(
        "Education",
        ("85", "Education"),
    ),
    _section(
        "Human health and social work",
        ("861", "Hospitals"),
        ("862", "Medical and dental practice"),
        ("869", "Other human health activities"),
        ("87", "Residential care"),
        ("88", "Social work without accommodation"),
    ),
    _section(
        "Arts, sports and recreation",
        ("90", "Arts creation and performing arts"),
        ("91", "Libraries, archives, museums and cultural activities"),
        ("92", "Gambling and betting"),
        ("93", "Sports, amusement and recreation"),
    ),
    _section(
        "Other service activities",
        ("94", "Membership organizations"),
        ("95", "Repair of computers, personal and household goods, and vehicles"),
        ("96", "Personal services"),
    ),
    _section(
        "Other",
        (OTHER_CODE, "Other (no listed domain fits)"),
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
    "OTHER_CODE",
    "DomainLeaf",
    "DomainSection",
    "domain_codes_from_ids",
]
