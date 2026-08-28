"""Read-side registry of ARCHIVED rubric revisions.

Stored assessments are stamped (rubric_version, rubric_content_hash) at write
time. The live document churns; this registry lets the read paths render each
row against the revision that scored it — dimension names, weights, scale and
band lines are DISPLAY metadata only, never re-fed into any arithmetic
(stored weighted_score/band are write-time facts and stay untouched).

The live document is never duplicated here: its view is derived from
load_rubric() so the two cannot drift. Loaded once at import, fail-fast on an
invalid document, exactly like blackbird_rubric.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.services.blackbird_rubric import load_rubric

REVISIONS_PATH = Path("prompts/rubric/revisions.toml")

PROVENANCE_LIVE = "live"
PROVENANCE_ARCHIVED = "archived"
PROVENANCE_UNSTAMPED = "unstamped"
PROVENANCE_UNKNOWN = "unknown"


class RevisionRegistryError(RuntimeError):
    """The registry document is malformed. Raised at import — a wrong registry
    must fail the deploy, not mislabel archived verdicts at 2am."""


@dataclass(frozen=True)
class RevisionDimension:
    key: str
    title: str
    weight: int | None  # numeric single-scale weight (bar shading); None for row-extras
    weight_note: str    # display string: "25%" or "6%/4% (investment/incubation)"


@dataclass(frozen=True)
class RubricRevisionView:
    version: str
    content_hash: str
    scale_min: int
    scale_max: int
    advance_min: float | None
    conditional_min: float | None
    pass_label: str | None
    banding_note: str | None
    dimensions: tuple[RevisionDimension, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RevisionRegistryError(message)


def _parse_registry(path: Path) -> tuple[RubricRevisionView, ...]:
    _require(path.is_file(), f"revision registry missing: {path}")
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    entries = doc.get("revision")
    _require(isinstance(entries, list) and entries, "[[revision]] entries required")

    views: list[RubricRevisionView] = []
    for i, raw in enumerate(entries):
        where = f"revision[{i}]"
        version = raw.get("version")
        content_hash = raw.get("content_hash")
        _require(isinstance(version, str) and version, f"{where}.version")
        _require(
            isinstance(content_hash, str) and len(content_hash) == 12,
            f"{where}.content_hash must be the 12-hex sha256 prefix",
        )
        dims_raw = raw.get("dimension")
        _require(isinstance(dims_raw, list) and dims_raw, f"{where}.dimension table required")
        dims: list[RevisionDimension] = []
        for j, d in enumerate(dims_raw):
            dwhere = f"{where}.dimension[{j}]"
            key, title, weight = d.get("key"), d.get("title"), d.get("weight")
            _require(isinstance(key, str) and key, f"{dwhere}.key")
            _require(isinstance(title, str) and title, f"{dwhere}.title")
            _require(isinstance(weight, int), f"{dwhere}.weight must be an int")
            weight_incubation = d.get("weight_incubation")
            if weight_incubation is not None:
                _require(isinstance(weight_incubation, int), f"{dwhere}.weight_incubation")
                note = f"{weight}%/{weight_incubation}% (investment/incubation)"
            else:
                note = f"{weight}%"
            dims.append(RevisionDimension(key=key, title=title, weight=weight, weight_note=note))
        _require(
            len({d.key for d in dims}) == len(dims), f"{where}: duplicate dimension keys"
        )
        views.append(RubricRevisionView(
            version=version,
            content_hash=content_hash,
            scale_min=int(raw.get("scale_min", 1)),
            scale_max=int(raw["scale_max"]) if "scale_max" in raw else 5,
            advance_min=float(raw["advance_min"]) if "advance_min" in raw else None,
            conditional_min=float(raw["conditional_min"]) if "conditional_min" in raw else None,
            pass_label=raw.get("pass_label"),
            banding_note=raw.get("banding_note"),
            dimensions=tuple(dims),
        ))
    hashes = [v.content_hash for v in views]
    _require(len(hashes) == len(set(hashes)), "duplicate content_hash entries")
    return tuple(views)


_ARCHIVED: tuple[RubricRevisionView, ...] = _parse_registry(REVISIONS_PATH)
_BY_HASH: dict[str, RubricRevisionView] = {v.content_hash: v for v in _ARCHIVED}


def live_revision_view() -> RubricRevisionView:
    r = load_rubric()
    return RubricRevisionView(
        version=r.version,
        content_hash=r.content_hash,
        scale_min=r.scale_min,
        scale_max=r.scale_max,
        advance_min=r.advance_min,
        conditional_min=r.conditional_min,
        pass_label=r.pass_label,
        banding_note=None,
        dimensions=tuple(
            RevisionDimension(key=d.key, title=d.title, weight=d.weight,
                              weight_note=f"{d.weight}%")
            for d in r.dimensions
        ),
    )


def resolve_revision(
    version: str | None, content_hash: str | None
) -> tuple[RubricRevisionView | None, str]:
    """Which revision scored a row, and how sure we are.

    Hash is authoritative: a known version with a WRONG hash is a different
    document and resolves UNKNOWN rather than to the version's registry entry.
    Version-only matching (rows written when only the version was stamped, or
    hand-built fixtures) is honoured only when exactly one candidate exists.
    """
    live = live_revision_view()
    if not version and not content_hash:
        return live, PROVENANCE_UNSTAMPED
    if content_hash:
        if content_hash == live.content_hash:
            return live, PROVENANCE_LIVE
        hit = _BY_HASH.get(content_hash)
        if hit is not None:
            return hit, PROVENANCE_ARCHIVED
        return None, PROVENANCE_UNKNOWN
    # version set, hash absent
    candidates = [v for v in _ARCHIVED if v.version == version]
    if version == live.version:
        candidates.append(live)
    if len(candidates) == 1:
        view = candidates[0]
        provenance = PROVENANCE_LIVE if view.content_hash == live.content_hash else PROVENANCE_ARCHIVED
        return view, provenance
    return None, PROVENANCE_UNKNOWN
