"""Word equations (Office Math Markup, ``<m:oMath>``) as LaTeX.

Each structure in ECMA-376 Part 1, section 22.1 maps to its LaTeX form. A
character the tables below do not list is written as it is, and an element
this module does not know keeps the text of what it holds.
"""

from __future__ import annotations

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_VAL = f"{{{_MATH_NS}}}val"

# Characters LaTeX reads as markup, written so they print as themselves.
_ESCAPES = {
    "\\": r"\backslash{}",
    "{": r"\{",
    "}": r"\}",
    "_": r"\_",
    "^": r"\^{}",
    "%": r"\%",
    "#": r"\#",
    "&": r"\&",
    "$": r"\$",
}

# n-ary operators; ECMA-376 makes the integral the default.
_NARY_DEFAULT = "∫"
_NARY_OPERATORS = {
    "∑": r"\sum",
    "∏": r"\prod",
    "∐": r"\coprod",
    "∫": r"\int",
    "∬": r"\iint",
    "∭": r"\iiint",
    "∮": r"\oint",
    "⋃": r"\bigcup",
    "⋂": r"\bigcap",
    "⋁": r"\bigvee",
    "⋀": r"\bigwedge",
    "⨁": r"\bigoplus",
    "⨂": r"\bigotimes",
    "⨀": r"\bigodot",
}

# Accents; ECMA-376 makes the circumflex the default.
_ACCENT_DEFAULT = "̂"
_ACCENTS = {
    "̂": r"\hat",
    "̃": r"\tilde",
    "̄": r"\bar",
    "̅": r"\overline",
    "‾": r"\overline",
    "̇": r"\dot",
    "̈": r"\ddot",
    "⃛": r"\dddot",
    "⃗": r"\vec",
    "́": r"\acute",
    "̀": r"\grave",
    "̆": r"\breve",
    "̌": r"\check",
}

# Delimiters that ``\left`` and ``\right`` accept; ECMA-376 defaults are
# parentheses around and a vertical bar between.
_BEGIN_DEFAULT = "("
_END_DEFAULT = ")"
_SEPARATOR_DEFAULT = "|"
_DELIMITERS = {
    "(": "(",
    ")": ")",
    "[": "[",
    "]": "]",
    "{": r"\{",
    "}": r"\}",
    "|": "|",
    "‖": r"\|",
    "⟨": r"\langle",
    "⟩": r"\rangle",
    "〈": r"\langle",
    "〉": r"\rangle",
    "⌊": r"\lfloor",
    "⌋": r"\rfloor",
    "⌈": r"\lceil",
    "⌉": r"\rceil",
}

# Group characters; ECMA-376 makes the bottom curly bracket the default.
_GROUP_DEFAULT = "⏟"
_GROUP_BRACES = {"⏟": r"\underbrace", "⏞": r"\overbrace"}


def omml_to_latex(o_math) -> str:
    """LaTeX for one ``<m:oMath>`` element, without math delimiters."""
    return _children(o_math)


def _local(element) -> str:
    return etree.QName(element).localname


def _is_math(element) -> bool:
    return element.tag.startswith(f"{{{_MATH_NS}}}")


def _child(element, name: str):
    return element.find(f"{{{_MATH_NS}}}{name}")


def _property(element, prop: str, name: str) -> str | None:
    """The ``m:val`` of ``<m:{prop}><m:{name}>``, or None when it is absent."""
    props = _child(element, prop)
    if props is None:
        return None
    value = _child(props, name)
    return None if value is None else value.get(_VAL)


def _is_on(element, prop: str, name: str) -> bool:
    """An ECMA-376 on/off property: present means on unless its value says off."""
    props = _child(element, prop)
    if props is None:
        return False
    value = _child(props, name)
    if value is None:
        return False
    return value.get(_VAL) not in ("0", "off", "false")


def _children(element) -> str:
    parts: list[str] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if _is_math(child):
            if not _local(child).endswith("Pr"):
                parts.append(_convert(child))
        else:
            # A Word element inside an equation, such as a content control,
            # holds equation parts of its own.
            parts.append(_children(child))
    return "".join(parts)


def _arg(element, name: str) -> str:
    part = _child(element, name)
    return "" if part is None else _children(part)


def _literal(text: str) -> str:
    return "".join(_ESCAPES.get(char, char) for char in text)


def _text(run) -> str:
    return "".join(_literal(text.text or "") for text in run.iter(f"{{{_MATH_NS}}}t"))


def _convert(element) -> str:
    name = _local(element)
    converter = _CONVERTERS.get(name)
    if converter is not None:
        return converter(element)
    if name == "r":
        return _text(element)
    return _children(element)


def _sub(element) -> str:
    return f"{{{_arg(element, 'e')}}}_{{{_arg(element, 'sub')}}}"


def _sup(element) -> str:
    return f"{{{_arg(element, 'e')}}}^{{{_arg(element, 'sup')}}}"


def _sub_sup(element) -> str:
    return (
        f"{{{_arg(element, 'e')}}}_{{{_arg(element, 'sub')}}}"
        f"^{{{_arg(element, 'sup')}}}"
    )


def _pre_script(element) -> str:
    return (
        f"{{}}_{{{_arg(element, 'sub')}}}^{{{_arg(element, 'sup')}}}"
        f"{{{_arg(element, 'e')}}}"
    )


def _fraction(element) -> str:
    return f"\\frac{{{_arg(element, 'num')}}}{{{_arg(element, 'den')}}}"


def _delimiter(element) -> str:
    begin = _property(element, "dPr", "begChr")
    end = _property(element, "dPr", "endChr")
    separator = _property(element, "dPr", "sepChr")
    begin = _BEGIN_DEFAULT if begin is None else begin
    end = _END_DEFAULT if end is None else end
    separator = _SEPARATOR_DEFAULT if separator is None else separator
    inner = _literal(separator).join(
        _children(part) for part in element.findall(f"{{{_MATH_NS}}}e")
    )
    # An empty character means no delimiter on that side.
    if all(char == "" or char in _DELIMITERS for char in (begin, end)):
        left = _DELIMITERS.get(begin, ".")
        right = _DELIMITERS.get(end, ".")
        return f"\\left{left}{inner}\\right{right}"
    return f"{_literal(begin)}{inner}{_literal(end)}"


def _accent(element) -> str:
    char = _property(element, "accPr", "chr")
    char = _ACCENT_DEFAULT if char is None else char
    command = _ACCENTS.get(char)
    base = _arg(element, "e")
    if command is None:
        return f"\\overset{{{_literal(char)}}}{{{base}}}"
    return f"{command}{{{base}}}"


def _nary(element) -> str:
    char = _property(element, "naryPr", "chr")
    char = _NARY_DEFAULT if char is None else char
    operator = _NARY_OPERATORS.get(char) or _literal(char)
    if not _is_on(element, "naryPr", "subHide"):
        operator += f"_{{{_arg(element, 'sub')}}}"
    if not _is_on(element, "naryPr", "supHide"):
        operator += f"^{{{_arg(element, 'sup')}}}"
    return f"{operator}{{{_arg(element, 'e')}}}"


def _equation_array(element) -> str:
    rows = [_children(row) for row in element.findall(f"{{{_MATH_NS}}}e")]
    return "\\begin{aligned}" + r"\\".join(rows) + "\\end{aligned}"


def _function(element) -> str:
    return f"\\operatorname{{{_arg(element, 'fName')}}}{{{_arg(element, 'e')}}}"


def _radical(element) -> str:
    degree = _arg(element, "deg")
    base = _arg(element, "e")
    if _is_on(element, "radPr", "degHide") or not degree:
        return f"\\sqrt{{{base}}}"
    return f"\\sqrt[{degree}]{{{base}}}"


def _border_box(element) -> str:
    return f"\\boxed{{{_arg(element, 'e')}}}"


def _lower_limit(element) -> str:
    return f"\\underset{{{_arg(element, 'lim')}}}{{{_arg(element, 'e')}}}"


def _upper_limit(element) -> str:
    return f"\\overset{{{_arg(element, 'lim')}}}{{{_arg(element, 'e')}}}"


def _matrix(element) -> str:
    rows = [
        " & ".join(_children(cell) for cell in row.findall(f"{{{_MATH_NS}}}e"))
        for row in element.findall(f"{{{_MATH_NS}}}mr")
    ]
    return "\\begin{matrix}" + r"\\".join(rows) + "\\end{matrix}"


def _group_character(element) -> str:
    char = _property(element, "groupChrPr", "chr")
    char = _GROUP_DEFAULT if char is None else char
    position = _property(element, "groupChrPr", "pos")
    base = _arg(element, "e")
    command = _GROUP_BRACES.get(char)
    if command is not None:
        return f"{command}{{{base}}}"
    if position == "top":
        return f"\\overset{{{_literal(char)}}}{{{base}}}"
    return f"\\underset{{{_literal(char)}}}{{{base}}}"


def _bar(element) -> str:
    # ECMA-376 puts a bar below its base unless it says top.
    if _property(element, "barPr", "pos") == "top":
        return f"\\overline{{{_arg(element, 'e')}}}"
    return f"\\underline{{{_arg(element, 'e')}}}"


_CONVERTERS = {
    "sSub": _sub,
    "sSup": _sup,
    "sSubSup": _sub_sup,
    "sPre": _pre_script,
    "f": _fraction,
    "d": _delimiter,
    "acc": _accent,
    "nary": _nary,
    "eqArr": _equation_array,
    "func": _function,
    "rad": _radical,
    "borderBox": _border_box,
    "limLow": _lower_limit,
    "limUpp": _upper_limit,
    "m": _matrix,
    "groupChr": _group_character,
    "bar": _bar,
}
