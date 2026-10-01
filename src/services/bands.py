"""The score bands, in one place (AP-6). Thresholds come from the rubric's
existing [banding] keys by name; nothing here changes the rubric (B22). The
stored vocabulary ("advance"/"conditional"/"pass") is unchanged; "pass" means
pass ON the deal and displays as BANDING["pass_label"]. Dependency-free:
blackbird_rubric imports this module, not the other way round."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str
    threshold_key: str | None
    css_class: str
    panel_required: bool


#: Highest first; the last band has no threshold (everything below).
BANDS: tuple[Band, ...] = (
    Band("advance", "advance_min", "green", True),
    Band("conditional", "conditional_min", "amber", True),
    Band("pass", None, "gray", False),
)
_BY_NAME = {b.name: b for b in BANDS}


def band_for(score: float, thresholds: Mapping[str, float]) -> str:
    """The name of the highest band whose threshold `score` reaches."""
    for b in BANDS:
        if b.threshold_key is None or score >= thresholds[b.threshold_key]:
            return b.name
    return BANDS[-1].name


def band_class(band: str | None, strong: int, muted: int) -> str:
    """Tailwind text colour for a band: its colour at `strong`, or gray at `muted`
    for the no-panel band and anything unknown (the templates' old else branch)."""
    b = _BY_NAME.get(band or "")
    if b is None or not b.panel_required:
        return f"text-gray-{muted}"
    return f"text-{b.css_class}-{strong}"


def band_label(band: str | None, pass_label: str | None) -> str | None:
    """The display text of a stored band: the lowest band shows as `pass_label`."""
    return pass_label if band == BANDS[-1].name else band
