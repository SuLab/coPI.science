"""Structured lines: one line per idea, composed in code from its normalized fields (spec
§7.10, L41, D29).

The line is never written by a model, so it always matches the idea record. Formats:

- biology: ``"{Direction} {SYMBOL} · {disease label} · {modality or 'modality open'}"``;
- chemistry: ``"{agent label} ({agent kind}) · {target or 'no single target'} · {disease or
  'no single disease'}"``.

Several targets are joined by ``/`` and several modality variants by `` / ``. An idea is a
``LandscapeIdea`` row or a dict with the same field names.
"""
from __future__ import annotations

from typing import Any

SEP = " · "
NO_MODALITY = "modality open"
NO_TARGET = "no single target"
NO_DISEASE = "no single disease"


def _field(idea: Any, name: str) -> Any:
    return idea.get(name) if isinstance(idea, dict) else getattr(idea, name, None)


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _first(item: Any, *keys: str) -> str:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        if text := _text(item.get(key)):
            return text
    return ""


def _targets(idea: Any) -> str:
    names: list[str] = []
    for target in _field(idea, "targets") or []:
        name = _first(target, "symbol", "label", "as_written", "hgnc_id", "provisional_key")
        if name and name not in names:
            names.append(name)
    return "/".join(names)


def _disease(idea: Any) -> str:
    return _first(_field(idea, "disease"), "label", "as_written", "mondo_id", "provisional_key")


def _modality(idea: Any) -> str:
    variants: list[str] = []
    for variant in _field(idea, "modality_variants") or []:
        text = _first(variant, "as_written", "label") if isinstance(variant, dict) else _text(variant)
        if text and text not in variants:
            variants.append(text)
    return " / ".join(variants)


def _direction(idea: Any) -> str:
    direction = _text(_field(idea, "direction")).replace("_", " ")
    return direction[:1].upper() + direction[1:] if direction else "Unknown"


def structured_line(idea) -> str:
    """The idea's structured line (D29), in the biology or chemistry form by ``idea_type``.

    A direction left empty reads ``Unknown``; an agent kind left empty reads ``other``.
    """
    disease = _disease(idea)
    if _field(idea, "idea_type") == "chemistry":
        agent = _field(idea, "agent")
        label = _first(agent, "label", "name", "as_written", "chembl_id") or "unnamed agent"
        kind = (_first(agent, "kind") or "other").replace("_", " ")
        return SEP.join((
            f"{label} ({kind})", _targets(idea) or NO_TARGET, disease or NO_DISEASE,
        ))
    head = f"{_direction(idea)} {_targets(idea) or NO_TARGET}"
    return SEP.join((head, disease or NO_DISEASE, _modality(idea) or NO_MODALITY))
