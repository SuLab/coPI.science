"""The methods section of a PMC full-text article (R2.12 of the 2026-10-15
publication-handling design; main SPEC PM-8 and PM-9). Pure: ``re`` and
``xml.etree.ElementTree`` only; tags are compared by local name, so JATS with or without a
namespace reads the same.

Locator: only the article's direct ``body``, then its direct ``back``, are searched (never
``front``, where a structured abstract has its own "Methods", nor ``sub-article``). A
``sec`` is a candidate by ``sec-type`` (tier 0), by a normalised title in
``METHOD_TITLES`` (tier 1), or, at the top level only, by ``_TITLE_PATTERN`` (tier 2). The
first candidate, by (body before back, strong tier before weak, depth, tier, document
order), with text beyond its title wins.

Serialiser: block elements start new lines, inline markup joins with no separator (H2O2),
whitespace inside a block collapses, and figures, tables, supplements, footnotes and
references are dropped with their tails kept.

A parse failure returns None, as before (the module's ``ET.ParseError`` is swallowed on
purpose): ``fetch_full_text`` then reports ``methods: None``, which
``src/agent/tools.py::_format_full_text`` renders by omitting the Methods block, so nothing
false is asserted."""

import re
import xml.etree.ElementTree as ET

#: Normalised section titles that name a methods section (main SPEC §4's title list).
METHOD_TITLES = frozenset({
    "methods", "method", "materials and methods", "material and methods",
    "methods and materials", "materials and method", "experimental procedures",
    "experimental procedure", "experimental methods", "experimental section", "experimental",
    "star methods", "method details", "online methods", "patients and methods",
    "subjects and methods", "participants and methods", "methodology",
    "computational methods", "materials", "study design and methods",
    "research design and methods", "general methods", "general materials and methods",
    "step-by-step method details",
})
_METHOD_SEC_TYPES = frozenset({"methods", "method", "materials"})
_TITLE_PATTERN = re.compile(
    r"^(?:materials?\s+and\s+)?methods?\b|^experimental\s+(?:procedures?|section|methods?)\b"
    r"|^online\s+methods\b|^star\s+methods\b"
)
_NUMBERING = re.compile(r"^(?:\d+(?:\s*\.\s*\d+)*|[ivx]+)\s*[.):]?\s+")
_BLOCKS = frozenset({
    "sec", "title", "p", "list", "list-item", "def-list", "def-item", "disp-quote",
    "boxed-text", "statement", "disp-formula", "caption", "table", "tr",
})
_DROPPED = frozenset({
    "fig", "table-wrap", "supplementary-material", "fn-group", "ref-list", "media", "graphic",
})


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in element if _local(c.tag) == name), None)


def _normalized_title(title: ET.Element) -> str:
    text = "".join(title.itertext()).replace("★", " ").replace("&", " and ")
    text = _NUMBERING.sub("", " ".join(text.split()).lower())
    return text.rstrip(" .:;")


def _dropped(element: ET.Element) -> bool:
    name = _local(element.tag)
    return name in _DROPPED or (name == "sec" and element.get("sec-type") == "supplementary-material")


def _serialize(element: ET.Element) -> str:
    """``element``'s text, one line per block, without the element's own tail."""
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        text = " ".join("".join(current).split())
        if text:
            blocks.append(text)
        current.clear()

    def walk(node: ET.Element) -> None:
        block = _local(node.tag) in _BLOCKS
        if block:
            flush()
        if node.text:
            current.append(node.text)
        for child in node:
            if not _dropped(child):
                walk(child)
            if child.tail:
                current.append(child.tail)
        if block:
            flush()

    walk(element)
    flush()
    return "\n".join(blocks)


def _tier(sec: ET.Element, depth: int) -> int | None:
    tokens = {t.strip().lower() for t in (sec.get("sec-type") or "").split("|")}
    if tokens & _METHOD_SEC_TYPES:
        return 0
    title = _child(sec, "title")
    normalized = _normalized_title(title) if title is not None else ""
    if normalized in METHOD_TITLES:
        return 1
    if depth == 0 and _TITLE_PATTERN.match(normalized):
        return 2
    return None


def _candidates(article: ET.Element) -> list[tuple[tuple[int, bool, int, int, int], ET.Element]]:
    found: list[tuple[tuple[int, bool, int, int, int], ET.Element]] = []
    order = 0

    def visit(node: ET.Element, part: int, depth: int) -> None:
        nonlocal order
        for sec in node:
            if _local(sec.tag) != "sec":
                continue
            order += 1
            tier = _tier(sec, depth)
            if tier is not None:
                found.append(((part, tier == 2, depth, tier, order), sec))
            visit(sec, part, depth + 1)

    for part, name in enumerate(("body", "back")):
        container = _child(article, name)
        if container is not None:
            visit(container, part, 0)
    return sorted(found, key=lambda item: item[0])


def extract_methods_section(xml_text: str) -> str | None:
    """The serialised methods section of a PMC article, or None: unparseable XML, no
    ``article`` element, or no candidate section with text beyond its title."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    article = root if _local(root.tag) == "article" else next(
        (el for el in root.iter() if _local(el.tag) == "article"), None
    )
    if article is None:
        return None
    for _key, sec in _candidates(article):
        text = _serialize(sec)
        title = _child(sec, "title")
        title_text = _serialize(title) if title is not None else ""
        body = text[len(title_text):] if title_text and text.startswith(title_text) else text
        if body.strip():
            return text
    return None
