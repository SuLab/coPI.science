"""Mondo from its release files (spec §6.3, §7.4 step 4, D34): the SSSOM mappings give
the exact matches between Mondo and MeSH or Orphanet, and the ontology (``mondo-simple.obo``)
gives names, exact synonyms and direct parents.

A release is downloaded and parsed once per process and re-checked weekly against the latest
GitHub release tag. `install` puts a parsed release in place directly, so tests (and a caller
holding local files) never download. The synchronous lookups read the installed release and
raise `MondoNotLoaded` before one is installed: await `ensure_loaded` (or any async lookup)
first.
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from src.landscaping import evidence
from src.services.http_pacing import Pacer

LATEST_RELEASE_URL = "https://api.github.com/repos/monarch-initiative/mondo/releases/latest"
OBO_URL = "https://github.com/monarch-initiative/mondo/releases/download/{tag}/mondo-simple.obo"
SSSOM_URL = ("https://raw.githubusercontent.com/monarch-initiative/mondo/{tag}"
             "/src/ontology/mappings/mondo.sssom.tsv")
#: Seconds between checks for a newer release.
REFRESH_S = 7 * 24 * 3600
_PACER = Pacer(1.0)

_SYNONYM = re.compile(r'^synonym: "((?:[^"\\]|\\.)*)" EXACT')


class MondoNotLoaded(RuntimeError):
    """A synchronous lookup ran before any Mondo release was installed."""


@dataclass(frozen=True)
class MondoData:
    """One parsed Mondo release. ``mesh`` and ``labels`` map to every Mondo id that claims
    the key, so an ambiguous key maps to a set of more than one; ``mesh_of`` and ``orpha``
    likewise map a Mondo id to every MeSH id or ``ORPHA:`` code it exactly matches.
    ``names`` is each live term's name."""

    release: str
    mesh: Mapping[str, frozenset[str]]
    labels: Mapping[str, frozenset[str]]
    parents: Mapping[str, frozenset[str]]
    names: Mapping[str, str]
    mesh_of: Mapping[str, frozenset[str]]
    orpha: Mapping[str, frozenset[str]]
    _ancestors: dict[str, frozenset[str]] = field(default_factory=dict, compare=False)


_data: MondoData | None = None
_checked_at = 0.0


def fold(text: str) -> str:
    """Label comparison form: NFKC, case-folded, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _mesh_key(mesh_id: str) -> str:
    """``MESH:D003920``, ``mesh:D003920`` or ``D003920`` → ``D003920``."""
    value = mesh_id.strip()
    if ":" in value:
        prefix, _, value = value.partition(":")
        if prefix.casefold() != "mesh":
            return ""
    return value.upper()


def _orpha_key(curie: str) -> str:
    """``Orphanet:558`` → ``ORPHA:558``; ``""`` for any other prefix or an empty code."""
    prefix, _, code = curie.strip().partition(":")
    return f"ORPHA:{code.strip()}" if prefix.casefold() == "orphanet" and code.strip() else ""


def parse_obo(
    text: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    """``(labels, parents, names)`` from an OBO file: each non-obsolete MONDO term's name and
    EXACT synonyms (folded) → term ids, term id → its direct ``is_a`` MONDO parents, and term
    id → its name as written."""
    labels: dict[str, set[str]] = {}
    parents: dict[str, set[str]] = {}
    term_names: dict[str, str] = {}
    for stanza in text.split("\n\n"):
        lines = stanza.strip().splitlines()
        if not lines or lines[0] != "[Term]":
            continue
        term_id, term_name, names, is_a, obsolete = None, None, [], set(), False
        for line in lines[1:]:
            if line.startswith("id: "):
                term_id = line[4:].strip()
            elif line.startswith("name: "):
                term_name = line[6:].strip()
                names.append(term_name)
            elif line.startswith("is_a: MONDO:"):
                is_a.add(line[6:].split()[0])
            elif line.startswith("is_obsolete: true"):
                obsolete = True
            elif (match := _SYNONYM.match(line)) is not None:
                names.append(match.group(1).replace('\\"', '"'))
        if not term_id or not term_id.startswith("MONDO:") or obsolete:
            continue
        parents[term_id] = is_a
        if term_name:
            term_names[term_id] = term_name
        for name in names:
            labels.setdefault(fold(name), set()).add(term_id)
    return labels, parents, term_names


def parse_sssom(text: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(mesh, orpha)`` from the ``skos:exactMatch`` rows: MeSH id (bare, e.g. ``D003920``)
    → the MONDO ids matched to it, and MONDO id → the Orphanet codes (``ORPHA:558``) it is
    matched to."""
    out: dict[str, set[str]] = {}
    orpha: dict[str, set[str]] = {}
    columns: list[str] | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        cells = line.split("\t")
        if columns is None:
            columns = cells
            continue
        row = dict(zip(columns, cells, strict=False))
        if row.get("predicate_id") != "skos:exactMatch":
            continue
        subject, obj = row.get("subject_id", ""), row.get("object_id", "")
        if not subject.startswith("MONDO:"):
            continue
        mesh = _mesh_key(obj) if obj.casefold().startswith("mesh:") else ""
        if mesh:
            out.setdefault(mesh, set()).add(subject)
        elif code := _orpha_key(obj):
            orpha.setdefault(subject, set()).add(code)
    return out, orpha


def build(release: str, obo_text: str, sssom_text: str) -> MondoData:
    labels, parents, names = parse_obo(obo_text)
    mesh, orpha = parse_sssom(sssom_text)
    mesh_of: dict[str, set[str]] = {}
    for mesh_id, mondo_ids in mesh.items():
        for mondo_id in mondo_ids:
            mesh_of.setdefault(mondo_id, set()).add(mesh_id)
    return MondoData(
        release=release,
        mesh={k: frozenset(v) for k, v in mesh.items()},
        labels={k: frozenset(v) for k, v in labels.items()},
        parents={k: frozenset(v) for k, v in parents.items()},
        names=names,
        mesh_of={k: frozenset(v) for k, v in mesh_of.items()},
        orpha={k: frozenset(v) for k, v in orpha.items()},
    )


def install(data: MondoData) -> None:
    """Make ``data`` the current release, checked as of now."""
    global _data, _checked_at
    _data = data
    _checked_at = time.monotonic()


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject an httpx.MockTransport."""
    return evidence.make_client(timeout=300.0)


async def _latest_release() -> str:
    body = await evidence.request_json(
        _make_client, LATEST_RELEASE_URL, pacer=_PACER,
        headers={"Accept": "application/vnd.github+json"})
    return str(body["tag_name"])


async def _download(tag: str) -> MondoData:
    async with _make_client() as client:
        texts = []
        for url in (OBO_URL.format(tag=tag), SSSOM_URL.format(tag=tag)):
            await _PACER.wait()
            resp = await client.get(url)
            resp.raise_for_status()
            texts.append(resp.text)
    return build(tag, texts[0], texts[1])


async def ensure_loaded() -> MondoData:
    """The current release, downloading it the first time and when a weekly check finds a
    newer release tag. A failed check keeps serving the installed release."""
    global _checked_at
    if _data is not None and time.monotonic() - _checked_at < REFRESH_S:
        return _data
    try:
        tag = await _latest_release()
    except (httpx.HTTPError, KeyError, ValueError):
        if _data is None:
            raise
        _checked_at = time.monotonic()
        return _data
    if _data is None or tag != _data.release:
        install(await _download(tag))
    else:
        _checked_at = time.monotonic()
    return _loaded()


def _loaded() -> MondoData:
    if _data is None:
        raise MondoNotLoaded("no Mondo release installed; await mondo.ensure_loaded() first")
    return _data


async def mondo_release() -> str:
    return (await ensure_loaded()).release


async def mondo_from_mesh(mesh_id: str) -> str | None:
    """The one Mondo id with an SSSOM exact match to the MeSH id; None when there is none or
    more than one (leave unmatched rather than guess, §7.4 step 4)."""
    data = await ensure_loaded()
    ids = data.mesh.get(_mesh_key(mesh_id)) or frozenset()
    return next(iter(ids)) if len(ids) == 1 else None


def mondo_label_match(text: str) -> str | None:
    """The one Mondo id whose label or exact synonym equals ``text`` after folding; None
    when there is none or more than one."""
    ids = _loaded().labels.get(fold(text)) or frozenset()
    return next(iter(ids)) if len(ids) == 1 else None


def mondo_label(mondo_id: str) -> str | None:
    """The term's name; None for an unknown or obsolete id."""
    return _loaded().names.get(mondo_id)


def mondo_orpha(mondo_id: str) -> str | None:
    """The one Orphanet code (``ORPHA:558``) the term exactly matches; None when there is
    none or more than one."""
    codes = _loaded().orpha.get(mondo_id) or frozenset()
    return next(iter(codes)) if len(codes) == 1 else None


def mondo_mesh(mondo_id: str) -> str | None:
    """The one MeSH id (``MESH:D003920``) the term exactly matches; None when there is none
    or more than one."""
    ids = _loaded().mesh_of.get(mondo_id) or frozenset()
    return f"MESH:{next(iter(ids))}" if len(ids) == 1 else None


def mondo_parents(mondo_id: str) -> frozenset[str]:
    """The term's direct ``is_a`` parents (empty for an unknown or obsolete id)."""
    return _loaded().parents.get(mondo_id, frozenset())


def _ancestors_of(data: MondoData, mondo_id: str) -> frozenset[str]:
    cached = data._ancestors.get(mondo_id)
    if cached is not None:
        return cached
    seen: set[str] = set()
    stack = list(data.parents.get(mondo_id, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(data.parents.get(node, ()))
    result = frozenset(seen)
    data._ancestors[mondo_id] = result
    return result


def is_ancestor(a: str, b: str) -> bool:
    """True when ``a`` is a proper ancestor of ``b`` through ``is_a`` (so ``b`` is a Mondo
    descendant of ``a``); a term is not its own ancestor."""
    return a != b and a in _ancestors_of(_loaded(), b)
