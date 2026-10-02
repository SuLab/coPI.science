"""One screening verdict, plus a reconstruction of the interview behind it.

The assessments list answers "which verdict deserves attention". This module
answers the next two questions, which nothing in the app could answer before:
*on what basis* was the verdict reached, and *who was asked*.

Two sources, deliberately kept separate:

* ``specialist_consults`` (``src/models/specialist_consult.py``) — the durable,
  forward-only record of a panel consult. It is EMPTY for every assessment that
  predates it: the table is written from the engine's consult success path,
  so rows only appear for runs that happen after it shipped.
* ``llm_call_logs.messages_json`` for the hub's ``thread_reply`` rows — the full
  tool conversation of every hub turn, ``tool_use`` blocks (tool + input) paired
  with ``tool_result`` blocks (including complete specialist opinions). This has
  been captured durably all along, which is what makes the timeline work
  RETROACTIVELY for the 29 assessments already on record. Parsing it at read
  time was chosen over a DB backfill (plan decision 3): no migration of
  inferred rows, and no risk of an inference becoming indistinguishable from a
  recorded fact.

Everything derived from ``llm_call_logs`` is admin-only (``admin_view``), along
with ``raw_opinion``: the LLM drill-down is an admin surface and managers
deliberately do not get one. Managers still see each consult's domain, signal,
confidence, concerns, established and questions_to_ask — the substance of what
the panel said. That split is a recorded policy decision, not an accident.

The redaction is done HERE, by omitting the values from the returned context,
rather than only by not rendering them in the manager template: a template that
never prints a value still ships it to anyone who can read the page source, and
a later template edit would silently widen the audience.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from markupsafe import Markup
from sqlalchemy import JSON, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# `panel_is_owed`/`PANEL_REQUIRED_FOR` are deliberately NOT imported here. This
# module reads what the floor RECORDED (`OpportunityAssessment.panel_owed`); it
# must never re-derive the floor's decision from today's predicate. See
# `panel_state`.
from src.agent.specialists import parse_opinion
from src.models import (
    AgentMessage,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    LlmCallLog,
    OpportunityAssessment,
    SpecialistConsult,
    User,
)
from src.services.assessment_headline import _clip_at_sentence
from src.services.blackbird_rubric import BANDING, RUBRIC_VERSION, load_rubric
from src.services.interview_transcript import load_interview_thread
from src.services.prose_citations import _is_linkable
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    RubricRevisionView,
    resolve_revision,
)

# Hard bounds. This page is a read of unbounded production data: a channel can
# hold hundreds of hub turns and a retrieve_full_text result can be an entire
# paper, so every list and every blob here is capped rather than trusted.
LOG_SCAN_LIMIT = 200
RESULT_EXCERPT_CHARS = 200
RESULT_FULL_CHARS = 4000
INPUT_SUMMARY_CHARS = 200
# Padding on the thread's own time span when selecting log rows. Six of
# production's channels host 29 assessments — several interviews per channel —
# so (run, agent, channel) alone would pull in turns from a DIFFERENT interview
# and file them under this one. The thread's first/last message bracket the
# turns that produced it; the pad absorbs clock skew between the log row's
# server-side created_at and the message's writer-side posted_at (measured at
# ~0.15s, but a pad costs nothing).
LOG_WINDOW_PAD_SECONDS = 300.0
# A turn is matched to the message it produced by normalized-text equality;
# failing that, by a normalized PREFIX this long. Measured over production run
# 88d81cd8 (116 hub thread_reply rows): exact matched 111, the prefix fallback
# recovered 2 more, 3 stayed unplaced. 100 chars is long enough that two
# distinct replies colliding on it is not a realistic outcome, and short enough
# to survive a trailing edit (a stripped mention, a stripped sidecar).
PREFIX_MATCH_CHARS = 100


# ---------------------------------------------------------------------------
# Text helpers
#
# These two regex families are REIMPLEMENTED here rather than imported from
# src/agent/simulation.py (which owns the canonical posting-path versions).
# Importing them would pull the whole engine — src.agent.agent, slack_client,
# tools, services.llm, the Anthropic SDK — into the web tier's import graph for
# two regexes, and would couple a read-only page to the module most likely to
# be mid-edit. If the posting path's tag handling ever changes, this correlator
# degrades to "unplaced turns", which the page renders explicitly; it does not
# break.
# ---------------------------------------------------------------------------

_SIDECAR_RE = re.compile(
    r"<\s*assessment_json\s*>\s*(.*?)\s*<\s*/\s*assessment_json\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SIDECAR_UNCLOSED_RE = re.compile(r"<\s*assessment_json\s*>.*", re.DOTALL | re.IGNORECASE)
_SIDECAR_ORPHAN_TAG_RE = re.compile(r"<\s*/?\s*assessment_json\s*>", re.IGNORECASE)

#: scout_hub >= 1.9.0 (2026-09-28): six groups, ONE main bullet each, in the
#: reviewer's own labels; scout_hub >= 1.10.0 lets any group add one labelled
#: extra (`classify_key_point` below). 1.8.0 (the 2026-09-22 Blackbird review) had the same
#: six with `key_questions` where `path_to_clinic` now sits. The (key, label)
#: order is the render order on both assessment surfaces and in the
#: assessment-chat record, all of which render through `key_point_sections`
#: below (via `_RENDER_GROUPS`, which restores the retired group's slot).
KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("indication_audience", "Indication / Audience"),
    ("lab_background", "Lab Background"),
    ("proposal", "Proposal"),
    ("clinical_actionability", "Clinical Actionability"),
    ("path_to_clinic", "Path to Clinic / Commercialization"),
    ("commercial_opportunity", "Commercial Opportunity"),
)

#: Retired from the WRITE contract by 1.9.0 and still rendered: five production
#: rows were written under 1.8.0 with this group, and nine legacy rows carry the
#: key too. The label here is 1.8.0's, and `_RENDER_GROUPS` below puts it back
#: in 1.8.0's fifth slot — a stored row renders under the labels AND in the
#: order it was written with, which is why this is a tuple of its own rather
#: than a deletion.
RETIRED_KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("key_questions", "Key Questions/Experiment"),
)

#: scout_hub 1.3.0-1.7.1 (1.3.0 carried the first, second and last). Kept for
#: two reasons only: a stored verdict renders under the labels and order it was
#: written with (D2 of
#: docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md), and a sidecar
#: from a stale prompt still stores rather than losing the field to `raw_verdict`.
LEGACY_KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("significance", "Significance"),
    ("innovation", "Innovation"),
    ("clinical_actionability", "Clinical actionability"),
    ("key_questions", "Key questions / experiments"),
    ("commercial_potential", "Commercial potential"),
)

#: Render order for any non-legacy row: the current six with the retired group
#: restored to the slot 1.8.0 gave it (fifth, between clinical actionability
#: and what follows it). Only groups the stored value actually carries render,
#: so a 1.9.0 row simply has nothing there, and a row carrying both
#: `key_questions` and `path_to_clinic` shows each once, in this order.
_RENDER_GROUPS: tuple[tuple[str, str], ...] = (
    KEY_POINT_GROUPS[:4] + RETIRED_KEY_POINT_GROUPS + KEY_POINT_GROUPS[4:]
)

_CURRENT_KEY_POINT_KEYS = frozenset(k for k, _ in KEY_POINT_GROUPS)
_RETIRED_KEY_POINT_KEYS = frozenset(k for k, _ in RETIRED_KEY_POINT_GROUPS)
_LEGACY_KEY_POINT_KEYS = frozenset(k for k, _ in LEGACY_KEY_POINT_GROUPS)
#: A RETIRED key is evidence of neither shape — it was written by 1.8.0 (a
#: "current" prompt at the time) and by the legacy sets alike — so it is
#: subtracted here. Without this a 1.8.0 row classifies "mixed" and takes the
#: stale-prompt warning branch it has no business on. It is also what keeps
#: the legacy-only tail `key_point_sections` appends disjoint from
#: `_RENDER_GROUPS`, so no key renders twice.
_LEGACY_ONLY_KEY_POINT_KEYS = (
    _LEGACY_KEY_POINT_KEYS - _CURRENT_KEY_POINT_KEYS - _RETIRED_KEY_POINT_KEYS
)
_CURRENT_ONLY_KEY_POINT_KEYS = _CURRENT_KEY_POINT_KEYS - _LEGACY_KEY_POINT_KEYS

#: Write-time acceptance: the THREE-way union. Accepting only the current keys
#: would drop `key_points` from a stale 1.8.0 or legacy prompt on this image.
#: The other skew direction, a 1.9.0 prompt on an older image, cannot be fixed
#: here: that image's own accepted set lacks `path_to_clinic`.
KEY_POINT_ACCEPTED_KEYS: frozenset[str] = (
    _CURRENT_KEY_POINT_KEYS | _RETIRED_KEY_POINT_KEYS | _LEGACY_KEY_POINT_KEYS
)

#: The two labels a key-point EXTRA bullet may open with (scout_hub >= 1.10.0,
#: spec §6.1): any group may add one `Risk:` bullet after its main bullet, and Lab
#: Background one `Companies:` bullet. These are the kinds `classify_key_point`
#: returns; the engine's soft checks count the extras with it.
KEY_POINT_RISK = "risk"
KEY_POINT_COMPANIES = "companies"
#: What the page prints, in bold, before an extra's body, and what the chat record
#: quotes: the contract's own spelling, whatever case or markup the hub used.
KEY_POINT_LABELS: dict[str, str] = {
    KEY_POINT_RISK: "Risk:",
    KEY_POINT_COMPANIES: "Companies:",
}

#: A label at the very start of a bullet, tolerant of what models emit (Review
#: Focus #3 of docs/plans/2026-10-02-hub-1-10-summary-risks-gates-plan.md): any
#: case, spaces around the colon, and markdown emphasis around the word or the
#: word and its colon (`**Risk:**`, `**Risk**:`, `_Risk_:`). The word must be
#: exactly `risk` or `companies`: "Risky approach:", "Risks:" and "Company:" are
#: ordinary text.
_KEY_POINT_LABEL_RE = re.compile(
    r"\s*(?P<em>\*\*|__|\*|_)?\s*(?P<label>risk|companies)\s*(?:(?P=em))?"
    r"\s*:\s*(?:(?P=em))?\s*(?P<body>.*)",
    re.IGNORECASE | re.DOTALL,
)


def classify_key_point(text: str) -> tuple[str | None, str]:
    """``(None, text)`` for a main bullet, or ``(kind, body)`` for a labelled
    extra: `kind` is `KEY_POINT_RISK` or `KEY_POINT_COMPANIES`, and `body` is the
    bullet without its label (and without emphasis markers wrapping it), stripped.

    A label followed by nothing is not an extra (there is nothing to show under
    it), and neither is a non-string; both come back unchanged as
    ``(None, text)``. Never raises. This decides only how a bullet is DISPLAYED
    and what the engine's soft checks count; `normalize_key_points` still stores
    the bullet exactly as the hub wrote it.
    """
    if not isinstance(text, str):
        return None, text
    match = _KEY_POINT_LABEL_RE.fullmatch(text)
    if match is None:
        return None, text
    body = match.group("body").strip()
    emphasis = match.group("em")
    if emphasis and body.endswith(emphasis):
        # `**Risk: the whole bullet in bold**`: the closing marker wraps the body.
        body = body[: -len(emphasis)].rstrip()
    if not body:
        return None, text
    return match.group("label").lower(), body


class KeyPointExtra(NamedTuple):
    """One labelled extra bullet of a key-point group, ready to render."""

    kind: str  # KEY_POINT_RISK or KEY_POINT_COMPANIES
    label: str  # KEY_POINT_LABELS[kind]: "Risk:" or "Companies:"
    body: str  # the bullet without its label


#: The current Lab Background group's label: the brief places the PI's
#: staff-confirmed companies directly under the section carrying it (spec §7.4).
_LAB_BACKGROUND_LABEL = dict(KEY_POINT_GROUPS)["lab_background"]


class KeyPointSection(NamedTuple):
    """One renderable key-point group: its display `label` (None for the flat
    <= 1.2.0 list) and its non-blank `points` in stored order.

    A NamedTuple so every existing ``for label, points in key_point_sections(...)``
    (the frozen chat-record copy among them) and every ``== (label, [...])``
    comparison keep working unchanged; label-aware rendering reads `extras`.
    """

    label: str | None
    points: list[str]

    @property
    def extras(self) -> list[KeyPointExtra] | None:
        """The group's extras, labelled, when EVERY bullet after the first
        carries a known label (spec §6.1 Rendering); None otherwise — for a
        single-bullet group, the flat list, and any group with an unlabelled
        extra (1.8.0 and legacy multi-bullet rows), all of which keep today's
        rendering. The first bullet is the main one whatever it says."""
        if self.label is None or len(self.points) < 2:
            return None
        extras: list[KeyPointExtra] = []
        for point in self.points[1:]:
            kind, body = classify_key_point(point)
            if kind is None:
                return None
            extras.append(KeyPointExtra(kind, KEY_POINT_LABELS[kind], body))
        return extras

    @property
    def is_lab_background(self) -> bool:
        """Whether this is the Lab Background group, under which the brief places
        the PI's staff-confirmed companies (spec §7.4)."""
        return self.label == _LAB_BACKGROUND_LABEL


def key_point_shape(value: object) -> str | None:
    """``"flat"`` (<= 1.2.0 list), ``"legacy"`` (a legacy-only key and no
    current-only key), ``"current"``, ``"mixed"`` (both), or None for anything
    that is not a list or a non-empty dict.

    Two kinds of key are evidence of NEITHER shape: `clinical_actionability`,
    which the current and legacy sets share, and the retired `key_questions`
    (`RETIRED_KEY_POINT_GROUPS`), which 1.8.0 and the legacy sets both wrote.
    So a 1.8.0 row — the six 1.8.0 keys, `key_questions` included — classifies
    ``"current"``, and a dict holding only those neutral keys classifies
    ``"current"`` too (no stored row has that shape, measured 2026-09-25) and
    renders under the current labels.
    """
    if isinstance(value, list):
        return "flat"
    if not isinstance(value, dict) or not value:
        return None
    keys = set(value)
    legacy = bool(keys & _LEGACY_ONLY_KEY_POINT_KEYS)
    current = bool(keys & _CURRENT_ONLY_KEY_POINT_KEYS)
    if legacy and current:
        return "mixed"
    return "legacy" if legacy else "current"


def _clean_bullets(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def key_point_sections(value: object) -> list[KeyPointSection]:
    """The renderable key-point sections of a stored value, in display order.

    Each section is a `KeyPointSection` — a ``(label, bullets)`` pair to every
    caller that unpacks it, plus `extras` for label-aware rendering (scout_hub
    >= 1.10.0). A grouped value yields one per group: a legacy-shaped row uses
    `LEGACY_KEY_POINT_GROUPS`; every other row uses `_RENDER_GROUPS` — the
    current six with the retired `key_questions` back in its 1.8.0 fifth slot,
    under its 1.8.0 label — followed by any legacy-only group it also carries,
    so nothing stored is hidden and no key renders twice. A flat list yields
    one ``(None, bullets)`` pair. Only non-empty groups are returned — blank
    bullets dropped, unknown keys and non-list values ignored — so "is there
    anything to show" is the truthiness of the result. Both
    assessment templates and the chat record render from this, which is what
    keeps the page and the record identical.
    """
    shape = key_point_shape(value)
    if shape == "flat":
        bullets = _clean_bullets(value)
        return [KeyPointSection(None, bullets)] if bullets else []
    if shape is None:
        return []
    if shape == "legacy":
        groups = LEGACY_KEY_POINT_GROUPS
    else:
        groups = _RENDER_GROUPS + tuple(
            (key, label) for key, label in LEGACY_KEY_POINT_GROUPS
            if key in _LEGACY_ONLY_KEY_POINT_KEYS
        )
    sections: list[KeyPointSection] = []
    for key, label in groups:
        bullets = _clean_bullets(value.get(key))
        if bullets:
            sections.append(KeyPointSection(label, bullets))
    return sections


def normalize_key_points(value: object) -> list | dict | None:
    """Write-time shape check for the sidecar's ``key_points``.

    Accepts the flat list (<= 1.2.0) and any grouped object whose keys are a
    non-empty SUBSET of `KEY_POINT_ACCEPTED_KEYS` — current, retired, legacy
    or any mix — with every value a list of strings. Blank bullets are stripped. Anything
    else is None: a malformed narrative field never costs the verdict (A20),
    and `raw_verdict` keeps the original.

    A subset, not exact set equality: equality meant one omitted group stored
    `key_points = NULL` and lost the WHOLE field, the strictest possible
    reaction to the mildest possible defect. An UNKNOWN key is still rejected
    outright — that is real shape drift, and `_persist_assessment` logs it. An
    empty dict is rejected too; it carries nothing to render.
    """
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return [x.strip() for x in value if x.strip()]
    if isinstance(value, dict) and value and set(value) <= KEY_POINT_ACCEPTED_KEYS and all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in value.values()
    ):
        return {k: [x.strip() for x in v if x.strip()] for k, v in value.items()}
    return None


def normalize_bullets(value: object) -> list[str] | None:
    """The hub's own bullet-list sidecar fields: ``strengths`` / ``risks``
    (scout_hub >= 1.5.0) and ``competitive_landscape`` / ``evidence_maturity``
    (scout_hub >= 1.7.0, migration 0050).

    A non-empty list whose every element is a non-empty ``str`` after
    ``.strip()`` is returned stripped. Anything else — a dict, a string, an
    empty list, a list holding a non-string or a blank string — is ``None``: a
    malformed narrative field never costs the verdict (A20), and
    ``raw_verdict`` keeps the original.
    """
    if not isinstance(value, list) or not value:
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        out.append(item.strip())
    return out


#: Bounds on the sidecar's `dimension_rationales`. A rubric has six dimensions;
#: 20 leaves room for a future revision without letting an unbounded object
#: into a JSONB column, and 50 characters is longer than any dimension key this
#: repo has ever used (`differentiation_unmet_need` is 26).
_MAX_DIMENSION_RATIONALES = 20
_MAX_DIMENSION_KEY_CHARS = 50


def normalize_dimension_rationales(value: object) -> dict[str, str] | None:
    """The hub's one-sentence reason per dimension (sidecar item 2, migration
    0052).

    Accepts a non-empty dict of `str -> str`, at most
    `_MAX_DIMENSION_RATIONALES` entries. Keys are `.strip().lower()`-normalized
    the same way `build_assessment_detail` normalizes the `scores` map, so a
    sidecar emitting `"Scientific_Credibility"` still matches the dimension it
    is about — without this the rationale stores fine and renders nowhere,
    which is indistinguishable from the hub not writing one.

    Keys are deliberately NOT validated against the live rubric's dimension
    keys: a row is rendered against the revision that SCORED it, and a later
    revision renaming a dimension must not make an older row's reasons
    unstorable. An unknown key simply matches no rendered dimension.

    A blank or None VALUE is an absent reason, not a type violation, and is
    skipped rather than rejecting the map: the prompt's skeleton pre-fills every
    key with `""`, so one dimension the hub could not explain must not cost the
    other five their reasons
    (docs/specs/2026-09-28-assessment-chat-entry-and-key-points-design.md
    §5.3: warn, never drop — the write path's "scored dimension(s) with no
    rationale" warning names the gap). A map left entirely blank is None.

    Anything else is None: a non-dict, a non-str key or value, a blank or
    over-long key, or too many entries. A malformed narrative field never costs
    the verdict (A20), and `raw_verdict` keeps the original either way.
    """
    return _normalize_reason_map(value, _MAX_DIMENSION_RATIONALES, _MAX_DIMENSION_KEY_CHARS)


def _dimension_rationale_map(assessment: object) -> dict[str, str]:
    """The STORED `dimension_rationales` as a lookup map, keyed the same way
    the `scores` map is normalized in `build_assessment_detail`.

    `normalize_dimension_rationales` already lower-cases on write, but this is
    a READ of a JSONB column with no CHECK behind it, so it re-normalizes and
    skips anything that is not `str -> non-blank str` rather than trusting the
    writer; a NULL or non-dict value is `{}`. Read with `getattr` so a test
    double lacking the attribute degrades to "no reasons" instead of raising.
    """
    return _stored_reason_map(getattr(assessment, "dimension_rationales", None))


def _rationale_for(rationales: dict[str, str], key: object) -> str | None:
    """The stored reason for dimension or gate `key`, matched on `.strip().lower()`."""
    return rationales.get(key.strip().lower()) if isinstance(key, str) else None


def _normalize_reason_map(
    value: object, max_entries: int, max_key_chars: int
) -> dict[str, str] | None:
    """The write-time rule `normalize_dimension_rationales` documents, shared with
    `normalize_gating_rationales`; only the two bounds differ."""
    if not isinstance(value, dict) or not value:
        return None
    if len(value) > max_entries:
        return None
    out: dict[str, str] = {}
    for key, text in value.items():
        if not isinstance(key, str):
            return None
        slug = key.strip().lower()
        if not slug or len(slug) > max_key_chars:
            return None
        if text is None or (isinstance(text, str) and not text.strip()):
            continue
        if not isinstance(text, str):
            return None
        out[slug] = text.strip()
    return out or None


def _stored_reason_map(stored: object) -> dict[str, str]:
    """A STORED reason map (JSONB, no CHECK behind it) as a lookup keyed
    `.strip().lower()`: anything that is not `str -> non-blank str` is skipped and
    a NULL or non-dict value is `{}` — the read rule `_dimension_rationale_map`
    documents, shared with `gating_rationale_map`."""
    if not isinstance(stored, dict):
        return {}
    return {
        key.strip().lower(): text.strip()
        for key, text in stored.items()
        if isinstance(key, str) and isinstance(text, str) and text.strip()
    }


#: Bounds on the sidecar's `gating_rationales` (0058). The rubric has three
#: gates; 10 leaves room for a future revision without letting an unbounded
#: object into a JSONB column, and 50 is the dimension map's key bound.
_MAX_GATING_RATIONALES = 10
_MAX_GATING_KEY_CHARS = 50


def normalize_gating_rationales(value: object) -> dict[str, str] | None:
    """The hub's one-sentence reason per gate (sidecar item 1, scout_hub >= 1.10.0,
    migration 0058), keyed like `gating`.

    The rule is `normalize_dimension_rationales`' with its own bounds: a non-empty
    dict of `str -> str`, at most `_MAX_GATING_RATIONALES` entries, keys
    `.strip().lower()`-normalized and at most `_MAX_GATING_KEY_CHARS` long. A blank
    or None value is an absent reason and is skipped (the skeleton pre-fills every
    gate with `""`), and a map left entirely blank is None. Keys are not checked
    against any rubric's gating keys, for the reason given there. The 200-character
    sentence bound is a prompt contract the engine only warns about; the text is
    kept whole. Anything else is None: a malformed narrative field never costs the
    verdict (A20), and `raw_verdict` keeps the original.
    """
    return _normalize_reason_map(value, _MAX_GATING_RATIONALES, _MAX_GATING_KEY_CHARS)


def gating_rationale_map(assessment: object) -> dict[str, str]:
    """The STORED `gating_rationales` (0058) as a lookup keyed `.strip().lower()`,
    re-normalized on read exactly as `_dimension_rationale_map` re-normalizes its
    column. Read with `getattr`, so a row or test double without the attribute (the
    chat record's synthetic contexts, every pre-0058 fixture) simply has no reasons.
    The detail page and the assessment-chat record (`_gating_section`) both read
    reasons through this one function."""
    return _stored_reason_map(getattr(assessment, "gating_rationales", None))


def _gating_definitions(
    revision: Any, revision_provenance: str | None
) -> dict[str, dict[str, str]]:
    """The gate definitions (title and description per gate key) a page may show
    for a row with this revision and provenance (spec §5.3).

    `PROVENANCE_LIVE`: the live document's `[gating.*]`, as before.
    `PROVENANCE_ARCHIVED`: the registry's `[revision.gating.*]` tables
    (`RubricRevisionView.gating`) — 3.2.0 and 3.4.0 carry them, 3.3.0 and older do
    not, so those rows keep bare labels. Anything else — an unstamped row (only
    DISPLAYED against the live dimensions) or an unknown one — gets nothing:
    today's definition rendered against a decision of unknown provenance would
    mislabel it the way a hardcoded score threshold would. An entry that is not a
    non-empty `title`/`description` pair is skipped, so this cannot raise.
    `templates/admin/_assessments_body.html` applies the same rule to each row's
    `revision_view.gating`.
    """
    if revision_provenance == PROVENANCE_LIVE:
        source = load_rubric().gating
    elif revision_provenance == PROVENANCE_ARCHIVED:
        source = getattr(revision, "gating", None)
    else:
        return {}
    if not isinstance(source, dict):
        return {}
    definitions: dict[str, dict[str, str]] = {}
    for key, meta in source.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        title, description = meta.get("title"), meta.get("description")
        if isinstance(title, str) and title and isinstance(description, str) and description:
            definitions[key] = {"title": title, "description": description}
    return definitions


def _gating_reasons(assessment: object) -> dict[str, str]:
    """The hub's stored reason per gate, keyed by the row's OWN `gating` keys (so
    a template can look a gate up by the key it is iterating); a gate with no
    reason, and a reason for a key the row has no gate for, are absent."""
    gating = getattr(assessment, "gating", None)
    if not isinstance(gating, dict):
        return {}
    reasons = gating_rationale_map(assessment)
    out: dict[str, str] = {}
    for key in gating:
        reason = _rationale_for(reasons, key)
        if reason:
            out[key] = reason
    return out


def strip_assessment_sidecar(text: str) -> str:
    """Drop the ``<assessment_json>`` sidecar, closed or truncated.

    The posted message never carries it, so a correlator that left it in would
    compare a body that includes the verdict JSON against one that does not and
    never match.
    """
    text = _SIDECAR_RE.sub("", text or "")
    text = _SIDECAR_UNCLOSED_RE.sub("", text)
    return _SIDECAR_ORPHAN_TAG_RE.sub("", text)


def extract_slack_message(text: str) -> str:
    """The ``<slack_message>`` body, or the whole text when there is no block.

    Anchored on the LAST closing tag and the last opening tag before it, the
    same way the posting path is: a model that mentions ``<slack_message>``
    inside its reasoning must not anchor the match and drag the reasoning into
    the body.
    """
    raw = text or ""
    last_close = raw.rfind("</slack_message>")
    if last_close >= 0:
        last_open = raw.rfind("<slack_message>", 0, last_close)
        if last_open >= 0:
            return raw[last_open + len("<slack_message>") : last_close]
    return raw


def normalize_for_match(text: str) -> str:
    """Collapse every whitespace run to one space and trim.

    Not cosmetic: the logged response and the stored message differ by exactly
    this much in production (a leading newline inside the tag), and comparing
    them un-normalized matched 12 of 726 rows instead of 111 of 116.
    """
    return " ".join((text or "").split())


def visible_body(response_text: str) -> str:
    """The normalized text a hub turn actually posted, from its raw response."""
    return normalize_for_match(strip_assessment_sidecar(extract_slack_message(response_text)))


# ---------------------------------------------------------------------------
# Tool-conversation parsing
# ---------------------------------------------------------------------------

# The engine returns a successful consult with a "— signal: <signal>" line
# (src/agent/tools.py::_execute_consult_specialist): since 2026-08-28 it trails
# the raw opinion, before that it led as
# "<Specialist Title> — signal: <signal>\n\n<raw opinion>". Its absence is how a
# FAILED consult is told apart from an opinion: an unknown domain, a missing
# persona file, an API error and an empty reply all return prose with no signal
# line, and none of them may be shown as if a specialist had cleared anything.
# Five values, not three: `gap`/`adequate` are the live vocabulary (2026-08-28)
# and `caution`/`clear` are what the pre-rename corpus this regex exists to read
# actually says. Dropping the historical pair would make every interview that
# predates `specialist_consults` — the only ones this parse is used for — report
# its consults as FAILED, which is precisely the "shown as if a specialist had
# cleared anything" error inverted.
_CONSULT_SIGNAL_RE = re.compile(
    r"signal:\s*(blocking|gap|adequate|caution|clear)\b", re.IGNORECASE
)

CONSULT_TOOL_NAME = "consult_specialist"

# Input keys worth showing first in a chip's one-line summary, in this order.
# Anything else the tool was passed follows, so a new tool still summarizes.
_SUMMARY_KEYS = (
    "domain", "query", "question", "agent_id", "pmid", "doi", "identifier", "title", "url",
)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's content to text.

    The engine writes a plain string; the API's own schema also permits a list
    of blocks, and a JSON column will happily hand back either.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _input_summary(tool_input: object) -> str:
    if isinstance(tool_input, str):
        return _truncate(normalize_for_match(tool_input), INPUT_SUMMARY_CHARS)
    if not isinstance(tool_input, dict):
        return ""
    ordered = [k for k in _SUMMARY_KEYS if k in tool_input]
    ordered += [k for k in tool_input if k not in _SUMMARY_KEYS]
    # Whole key/value pieces, dropped rather than sliced: truncating the joined
    # string mid-key left summaries ending in things like "· conte" (observed
    # against real consult inputs). The most identifying keys come first, so
    # what gets dropped is the least useful part.
    separator = " · "
    parts: list[str] = []
    used = 0
    dropped = False
    for key in ordered:
        value = tool_input.get(key)
        if value is None or value == "":
            continue
        piece = f"{key}: {_truncate(normalize_for_match(str(value)), 120)}"
        if parts and used + len(separator) + len(piece) > INPUT_SUMMARY_CHARS:
            dropped = True
            break
        used += (len(separator) if parts else 0) + len(piece)
        parts.append(piece)
    summary = separator.join(parts)
    return f"{summary} …" if dropped else summary


def consult_opinion_from_result(result: str, *, domain: str) -> dict[str, Any] | None:
    """The specialist opinion a ``consult_specialist`` result carries, or None.

    None means the call did not produce an opinion (refused domain, missing
    persona, API error, empty reply) — a state that must never render as a
    verdict signal.

    ``concern_count`` rides alongside ``concerns`` because the chip renders the
    signal and not the list: after the 2026-08-28 rename ``adequate`` means
    "meets the bar for THIS STAGE", not "no concerns", and a bare label is
    exactly how its predecessor came to read as a clean bill of health.

    A RETRO reader, and one of only two that pass ``allow_historical=True``:
    ``result`` is a stored tool log, so a consult logged before the rename says
    ``caution``/``clear`` and must render as what it said. The live consult path
    shares ``parse_opinion`` and must NOT opt in — see ``_READABLE_SIGNALS`` in
    src/agent/specialists.py.
    """
    text = result or ""
    match = _CONSULT_SIGNAL_RE.search(text)
    if match is None:
        return None
    brace = text.find("{")
    if brace < 0:
        # Prose opinion: the engine's own parse is in the prefix line, and
        # there is no JSON body to read concerns/questions out of.
        return {
            "verdict_signal": match.group(1).lower(),
            "confidence": None,
            "concerns": [],
            "concern_count": 0,
            "questions_to_ask": [],
        }
    opinion = parse_opinion(text[brace:], domain=domain, allow_historical=True)
    return {
        "verdict_signal": opinion.verdict_signal,
        "confidence": opinion.confidence,
        "concerns": list(opinion.concerns),
        "concern_count": len(opinion.concerns),
        "questions_to_ask": list(opinion.questions_to_ask),
    }


def tool_chips_from_conversation(messages_json: object) -> list[dict[str, Any]]:
    """One chip per tool call in a logged hub turn, in call order.

    ``messages_json`` is the conversation ``generate_with_tools`` accumulated:
    a user string, then alternating assistant messages (a block list holding
    ``thinking``/``text``/``tool_use``) and user messages (a block list of
    ``tool_result``). Results are matched to calls by ``tool_use_id``, not by
    position — a round with two calls interleaves them.

    ``thinking`` blocks are skipped entirely: they carry a signature and no
    reader value, and they are the bulk of the payload's bytes.
    """
    if not isinstance(messages_json, list):
        return []
    uses: list[tuple[str | None, str, object]] = []
    results: dict[str | None, str] = {}
    for message in messages_json:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append((
                    block.get("id"),
                    str(block.get("name") or "tool"),
                    block.get("input"),
                ))
            elif block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = _result_text(block.get("content"))

    chips: list[dict[str, Any]] = []
    for use_id, name, tool_input in uses:
        result = results.get(use_id, "")
        domain = None
        if isinstance(tool_input, dict) and tool_input.get("domain"):
            domain = str(tool_input["domain"])
        is_consult = name == CONSULT_TOOL_NAME
        opinion = (
            consult_opinion_from_result(result, domain=domain or "unknown")
            if is_consult
            else None
        )
        chips.append({
            "tool": name,
            "is_consult": is_consult,
            "domain": domain,
            "summary": _input_summary(tool_input),
            "question": (
                str(tool_input.get("question") or "")
                if isinstance(tool_input, dict)
                else ""
            ),
            "opinion": opinion,
            "result_excerpt": _truncate(normalize_for_match(result), RESULT_EXCERPT_CHARS),
            "result_full": _truncate(result, RESULT_FULL_CHARS),
            "result_truncated": len(result) > RESULT_FULL_CHARS,
            "no_result": not result,
        })
    return chips


def correlate_turns_to_messages(
    turns: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Attach each logged turn to the message it posted.

    ``turns`` need a ``body`` (normalized posted text); ``messages`` need a
    ``key`` and a ``content_normalized``. Returns ``({key: [turn, ...]},
    unplaced)``. An unmatched turn is RETURNED, never dropped: a turn whose
    reply was edited, mention-stripped or truncated is still evidence of what
    the hub did, and silently discarding it would make the page look complete
    when it is not.
    """
    exact: dict[str, str] = {}
    by_prefix: dict[str, str] = {}
    for message in messages:
        norm = message.get("content_normalized") or ""
        if not norm:
            continue
        exact.setdefault(norm, message["key"])
        if len(norm) >= PREFIX_MATCH_CHARS:
            by_prefix.setdefault(norm[:PREFIX_MATCH_CHARS], message["key"])

    matched: dict[str, list[dict[str, Any]]] = {}
    unplaced: list[dict[str, Any]] = []
    for turn in turns:
        body = turn.get("body") or ""
        key = exact.get(body)
        if key is None and len(body) >= PREFIX_MATCH_CHARS:
            key = by_prefix.get(body[:PREFIX_MATCH_CHARS])
        if key is None:
            unplaced.append(turn)
        else:
            matched.setdefault(key, []).append(turn)
    return matched, unplaced


# ---------------------------------------------------------------------------
# The detail view
#
# `panel_summary_by_thread` used to live above this line and served the
# discussions pages' per-thread indicator. It is gone: both callers now use
# `src/services/thread_panel.py::panel_cards_by_thread`, which answers the same
# question with the full cards those pages expanded to need, keyed on the
# threads a render is actually showing rather than on the whole run — so the
# indicator and the cards are one query and cannot disagree. This module's own
# `panel_summary` (below) is built from the assessment's consults, and never
# called that function.
# ---------------------------------------------------------------------------


def _epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _message_at(message: AgentMessage) -> float:
    """When a message was posted, as an epoch float.

    ``posted_at`` is the writer's clock and is the ordering key everywhere else
    in the app; it carries a server_default of 0, so rows written before that
    column was populated fall back to the DB clock.
    """
    return float(message.posted_at or 0.0) or _epoch(message.created_at)


#: Every value ``panel_state`` can return, most alarming first. Public because
#: two templates and one aggregate query now branch on these strings, and a
#: typo in any of them would land silently in the terminal ``{% else %}``.
PANEL_STATES: tuple[str, ...] = (
    "gap", "unverified", "unrecorded", "not_owed", "verified",
)

#: The states that are NOT a verified panel and NOT an exemption the floor
#: itself recorded — i.e. every row a reader should not treat as vetted.
#:
#: The Python half of a rule that unavoidably exists twice: a run-level COUNT
#: cannot join to a Python function, so ``unvetted_panel_filter`` below is its
#: SQL twin. The two are bound by
#: ``tests/unit/test_directory_assessments.py::
#: test_the_sql_unvetted_filter_matches_panel_state_row_for_row``, which walks
#: every combination of the three columns ``panel_state`` reads and asserts they
#: agree ROW FOR ROW. Without that alarm the coupling is only a comment: the
#: first version of this constant claimed ``src/services/directory.py`` used it
#: while ``directory.py`` carried its own hand-written predicate and never
#: referenced it, so adding a sixth state here would have looked like it updated
#: the banner and changed nothing.
PANEL_STATES_UNVETTED: frozenset[str] = frozenset({"gap", "unverified", "unrecorded"})


def unvetted_panel_filter():
    """The SQL twin of ``panel_state(row) in PANEL_STATES_UNVETTED``.

    Lives HERE, beside the state machine and the constant it mirrors, rather
    than inline in ``src/services/directory.py`` where the banner is built: a
    second copy of this rule in another module is exactly the drift this whole
    change exists to end, and the three pieces have to be reviewable together.

    Reads as the negation of the two states that HAVE an answer — ``verified``
    (``panel_owed`` recorded True, no gap, checkable) and ``not_owed``
    (``panel_owed`` recorded False, no gap) — so a row is unvetted when it
    carries a demonstrated gap, or the ``[]`` sentinel meaning the floor could
    not be checked, or no record of whether a panel was owed at all.

    Returns a bare column expression with NO run scoping: the caller adds that,
    because the warning deliberately follows the run and not the lab filter.
    """
    return or_(
        OpportunityAssessment.panel_incomplete.is_(True),
        OpportunityAssessment.missing_domains.is_not(None),
        OpportunityAssessment.panel_owed.is_(None),
    )


def has_review_filter():
    """The SQL for "a human has written feedback on this assessment".

    Lives beside ``unvetted_panel_filter`` for the same reason that one does:
    one rule, one place. An EXISTS rather than a JOIN because a row with three
    reviews must appear ONCE — a join would multiply it by its feedback count
    and silently inflate both the page and ``total_count``.

    Deliberately narrower than ``review_columns_for``'s "Reviewed by", which
    unions feedback authors with status-event actors. The two can disagree for
    a row that carries a status event and no feedback: it renders a "Reviewed
    by" name and a chip while sitting in the Unreviewed tab. Zero such rows
    exist in production and nothing in the app can create one since the
    approve/disapprove controls were removed, but a restore could — which is
    why the tab strip says in words that a tab counts written feedback.

    Returns a bare correlated predicate with NO run scoping: the caller adds
    that, exactly as it does for ``unvetted_panel_filter``.
    """
    return (
        select(AssessmentReview.id)
        .where(AssessmentReview.assessment_id == OpportunityAssessment.id)
        .exists()
    )


def panel_state(assessment: OpportunityAssessment) -> str:
    """The FIVE findings the specialist floor can leave behind.

    Three come straight from ``OpportunityAssessment.missing_domains``: names = a
    demonstrated gap, ``[]`` = the floor could not be checked at all, NULL = no
    gap recorded.

    The other two split that NULL, because it was answering two questions with
    one word: "the floor evaluated this verdict and found nothing owed and
    unconsulted" and "this verdict never faced a floor at all". Reporting the
    second as "verified" claimed an audit that never ran — production run
    60c53424's pearce ``route-to-incubation`` row rendered the green box while
    ``required_domains_for`` named ``clinical`` and no clinical consult existed
    on that thread.

    **The split is READ FROM THE ROW, never re-derived here.** An earlier fix
    asked ``panel_is_owed(recommendation, band)`` at render time, and that is a
    different question — "would a panel be owed under TODAY's rules" — so every
    time the predicate widens, every older row is silently relabelled. It widened
    twice in 2026-08 alone, and 12 production rows written by the
    recommendation-only floor (which stored "no panel was owed" as
    ``panel_incomplete=False, missing_domains=NULL``) were re-read by the
    band-aware page as completed audits; at least five had a demonstrable gap.
    ``opportunity_assessments.panel_owed`` records what the floor decided AT
    WRITE TIME, and this replays it rather than re-deriving it from today's
    rule.

    Putting ``panel_is_owed`` back ahead of the column test re-arms that bug
    exactly; ``tests/unit/test_panel_state.py``'s
    ``test_the_read_path_never_re_derives_the_floor_s_decision`` fails if anyone
    does.

    Order of authority, strongest evidence first:

    * ``gap`` — the floor looked and found domains owed and never consulted.
    * ``unverified`` — the floor could not check at all (``[]``).
    * ``verified`` — ``panel_owed is True``: a panel WAS owed, so the floor
      evaluated this verdict, and the two states above say it found nothing.
      This is the only state that has earned the green box.
    * ``not_owed`` — ``panel_owed is False``: the floor determined no panel was
      owed and recorded that. The weakest claim of the five, which is why it
      sits below the two evidence states: a stored gap or a stored ``[]`` is
      evidence about THIS row, and evidence outranks an exemption.
    * ``unrecorded`` — ``panel_owed is None``: the row predates 0036, or was
      backfilled, or was hand-built by a test. We do not know whether any floor
      ran, so no claim is available. Never green.
    """
    if assessment.panel_incomplete:
        return "gap"
    if assessment.missing_domains is not None:
        return "unverified"
    if assessment.panel_owed is True:
        return "verified"
    if assessment.panel_owed is False:
        return "not_owed"
    return "unrecorded"


# ---------------------------------------------------------------------------
# Strengths / risks / not-established (request 3, decision D2)
# ---------------------------------------------------------------------------

#: Thresholds are FRACTIONS of the row's own `revision.scale_max`, never
#: absolute scores. On the live 1-5 scale that is 4 and 2.
STRENGTH_THRESHOLD_FRACTION = 0.8
RISK_THRESHOLD_FRACTION = 0.4

#: What a specialist signal counts as. Both sets name the historical labels
#: (`clear`, `caution`) as well as the live ones (`adequate`, `blocking`,
#: `gap`) because ~1,192 stored consults still carry the retired pair — the
#: same read-wider-than-you-write asymmetry `_READABLE_SIGNALS` exists for in
#: `src/agent/specialists.py`. Anything outside BOTH sets is unrecognised and
#: lands in the not-established bucket rather than falling off the end of the
#: branch.
_STRENGTH_SIGNALS = frozenset({"adequate", "clear"})
_RISK_SIGNALS = frozenset({"blocking", "gap", "caution"})

_NOT_SCORED_DETAIL = "not scored — counted as zero in the weighted score"
#: A verdict that carried NO dimension scores at all: `weighted_score` and
#: `band` are NULL for it (see `_persist_assessment`), so nothing was
#: "counted as zero" in a score that does not exist.
_NO_SCORES_AT_ALL_DETAIL = "no dimension scores were recorded for this verdict"
#: `read_state == "defaulted"`: the reply arrived complete but no signal
#: could be parsed out of it, so the stored one is `specialists.py`'s
#: substitute rather than anything a specialist said.
_DEFAULTED_CONSULT_DETAIL = "no signal could be read from this reply"
_TRUNCATED_CONSULT_DETAIL = "reply cut off — no signal"
_UNRECOGNISED_SIGNAL_DETAIL = "signal not recognised — nothing can be said about this consult"
_UNRECOGNISED_GATING_DETAIL = "unrecognised gating value"


def _usable_score(raw: object) -> float | None:
    """A score we can compare against a threshold, or None.

    `bool` subclasses `int`, so `True` would otherwise arrive as 1.0 and be
    reported as a real score of one.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _format_score(value: float, scale_max: object) -> str:
    def _plain(number: float) -> str:
        return str(int(number)) if float(number).is_integer() else f"{number:g}"

    ceiling = _usable_score(scale_max)
    if ceiling is None:
        return f"scored {_plain(value)}"
    return f"scored {_plain(value)} of {_plain(ceiling)}"


def _capped_body(items: object) -> list[str]:
    """The first 3 of a non-empty list of strings, plus an "and N more" tail.

    Blank strings are skipped before capping. Anything else — not a list,
    empty, or containing a non-string — quotes nothing: a `body` empty list means "no stored text to show", never a
    fabricated summary of items that were not all strings.
    """
    if not isinstance(items, list) or not items or not all(isinstance(x, str) for x in items):
        return []
    items = [x for x in items if x.strip()]
    body = list(items[:3])
    remaining = len(items) - len(body)
    if remaining > 0:
        body.append(f"and {remaining} more")
    return body


#: One line of quoted text in a COLLAPSED entry: the first sentence, clipped
#: at a sentence boundary. Stored concerns average ~380 characters and their
#: first sentences ~150 (measured 2026-09-14), so this is what keeps a card
#: with 30 consults readable without hiding that there is text to expand.
PREVIEW_CHARS = 160


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _preview(text: object) -> str | None:
    """The FIRST sentence of ``text`` (cut at the first `.`/`!`/`?` followed
    by whitespace), then clipped to ``PREVIEW_CHARS`` at a sentence or word
    boundary by ``_clip_at_sentence`` if that one sentence is itself too long.
    None for a non-string or blank. `_clip_at_sentence` alone is not enough:
    it keeps the LAST boundary that fits, so two short sentences would both
    survive, and the preview is meant to be one line."""
    if not isinstance(text, str) or not text.strip():
        return None
    first = _SENTENCE_END_RE.split(text.strip(), maxsplit=1)[0]
    return _clip_at_sentence(first, PREVIEW_CHARS)


def _latest_consult_per_domain(consults: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Collapse a thread's consults to ONE per domain — the most recent — in
    first-seen domain order, each paired with how many consults that domain
    had. ``_load_consults`` orders by ``created_at``, so the last dict seen for
    a domain is the latest opinion, which is the one the hub concluded on.
    An interview routinely holds 25-40 consults across 8 domains (measured
    2026-09-14: 37 consults, 8 domains, 42,600 characters of quoted concerns
    when every consult was its own bullet), so this is the single largest
    reduction the card makes; the panel card further down still lists every
    consult in full."""
    order: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for consult in consults:
        if not isinstance(consult, dict):
            continue
        domain = str(consult.get("domain") or "consult")
        if domain not in latest:
            order.append(domain)
        latest[domain] = consult
        counts[domain] = counts.get(domain, 0) + 1
    return [(latest[d], counts[d]) for d in order]


def _add_item(
    bucket: list[dict[str, Any]],
    source: str,
    label: object,
    detail: object,
    body: list[str] | None = None,
    note: str | None = None,
    preview: str | None = None,
    rationale: str | None = None,
) -> None:
    """Append one entry, in the shape ``derive_strengths_and_risks`` documents."""
    bucket.append({
        "source": source,
        "label": str(label) if label else source.replace("_", " "),
        "detail": str(detail),
        "body": list(body) if body else [],
        # One collapsed line of the quoted text (consults only); None
        # when the entry has nothing to preview or the body is static
        # rubric metadata that belongs behind the click.
        "preview": preview,
        # A non-quote annotation rendered as a badge, never as a bullet:
        # "latest of N consults" — so it is not mistaken for text a
        # specialist wrote.
        "note": note,
        # The hub's own one-sentence reason: per dimension (0052) or per
        # gate (0058); None for red flags and consults.
        "rationale": rationale,
    })


def _classify_dimensions(
    dimensions: list[dict[str, Any]] | None,
    revision: Any,
    rationales: dict[str, str],
    strengths: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    unestablished: list[dict[str, Any]],
    mid_scale: list[dict[str, Any]],
) -> tuple[dict[str, float | None], int, int]:
    """The dimension block of ``derive_strengths_and_risks``: files each scored
    dimension into ``strengths`` / ``risks`` / ``mid_scale`` by the ROW's revision
    scale, and each unscored one into ``unestablished``. Returns
    ``(thresholds, mid_scale_count, scored_dimension_count)``; with no revision
    nothing is filed and the thresholds stay None."""
    thresholds: dict[str, float | None] = {
        "strength": None, "risk": None, "scale_max": None,
    }
    mid_scale_count = 0
    scored_dimension_count = 0
    # Dimensions. Skipped wholesale when the row's revision is unknown: with no
    # scale there is no threshold, and guessing one is the re-derivation point 2
    # rules out.
    # "not scored, counted as zero in the weighted score" is only true when a
    # weighted score EXISTS. `_persist_assessment` writes `scores or None`
    # alongside a NULL `weighted_score`/`band` for a verdict that carried no
    # dimension scores at all, so for that row the claim would be made six
    # times about a number that was never computed. The detail page still
    # renders all six of the revision's dimensions for such a row, which is
    # why this has to be decided here rather than by the absence of rows.
    any_scored = any(
        isinstance(dim, dict) and _usable_score(dim.get("score")) is not None
        for dim in dimensions or ()
    )
    not_scored_detail = (
        _NOT_SCORED_DETAIL if any_scored else _NO_SCORES_AT_ALL_DETAIL
    )
    if revision is None:
        return thresholds, mid_scale_count, scored_dimension_count
    scale_max = _usable_score(getattr(revision, "scale_max", None))
    strength_threshold = risk_threshold = None
    if scale_max is not None:
        strength_threshold = STRENGTH_THRESHOLD_FRACTION * scale_max
        risk_threshold = RISK_THRESHOLD_FRACTION * scale_max
        thresholds = {
            "strength": strength_threshold,
            "risk": risk_threshold,
            "scale_max": scale_max,
        }
    for dim in dimensions or ():
        if not isinstance(dim, dict):
            continue
        label = dim.get("title") or dim.get("key")
        rationale = _rationale_for(rationales, dim.get("key"))
        weight_body = (
            [f"weight: {dim.get('weight_note')}"] if dim.get("weight") is not None else []
        )
        score = _usable_score(dim.get("score"))
        if score is None:
            _add_item(unestablished, "dimension", label, not_scored_detail,
                      rationale=rationale)
            continue
        scored_dimension_count += 1
        if scale_max is None:
            continue
        detail = _format_score(score, scale_max)
        if score >= strength_threshold:
            _add_item(strengths, "dimension", label, detail, body=weight_body,
                      rationale=rationale)
        elif score <= risk_threshold:
            _add_item(risks, "dimension", label, detail, body=weight_body,
                      rationale=rationale)
        else:
            # A mid-scale score is a real, neutral answer: listed in its
            # own bucket so every scored dimension appears on the page,
            # but never as a strength or a risk.
            mid_scale_count += 1
            _add_item(mid_scale, "dimension", label, detail, body=weight_body,
                      rationale=rationale)

    return thresholds, mid_scale_count, scored_dimension_count


def _gating_items(
    assessment: OpportunityAssessment,
    revision: Any,
    revision_provenance: str | None,
    strengths: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    unestablished: list[dict[str, Any]],
) -> None:
    """The gating block of ``derive_strengths_and_risks``: one entry per stored gate.

    Every state (met, not met, unconfirmed and an unrecognised value alike) gets
    the rubric's title as its `label` and the rubric's description as its `body`
    whenever `_gating_definitions` resolves the key for this row's revision, and
    the hub's stored reason (`gating_rationales`, 0058) as its `rationale`. The
    template shows the reason when there is one and the definition, prefixed
    "Rubric definition:", otherwise (spec §5.2).
    """
    # The tri-state strings, plus a fourth branch for anything else — `gating`
    # is JSONB with no CHECK constraint behind it.
    gating = getattr(assessment, "gating", None)
    if not isinstance(gating, dict):
        return
    definitions = _gating_definitions(revision, revision_provenance)
    reasons = gating_rationale_map(assessment)
    for key, value in gating.items():
        meta = definitions.get(key) if isinstance(key, str) else None
        label = meta["title"] if meta is not None else str(key).replace("_", " ")
        body = [meta["description"]] if meta is not None else []
        rationale = _rationale_for(reasons, key)
        if value == "met":
            bucket, detail = strengths, "met"
        elif value == "not_met":
            bucket, detail = risks, "not met"
        elif value == "unconfirmed":
            bucket, detail = unestablished, "never asked"
        else:
            bucket, detail = unestablished, _UNRECOGNISED_GATING_DETAIL
        _add_item(bucket, "gating", label, detail, body=body, rationale=rationale)


def _red_flag_items(assessment: OpportunityAssessment, risks: list[dict[str, Any]]) -> None:
    """The red-flag block of ``derive_strengths_and_risks``."""
    # Red flags, full text. A non-string entry is skipped rather than coerced:
    # a rendered `None` or `{}` would read as a flag the hub never wrote.
    # Stored flags average ~535 characters (2026-09-14), so the summary line is
    # the first sentence and the full text sits behind the click — but ONLY
    # when the clip actually shortened it: a short flag renders as a plain,
    # uncollapsed bullet, which is what the red-flag card's own
    # never-collapsed rule expects of a disqualifier a reviewer must see.
    red_flags = getattr(assessment, "red_flags", None)
    if isinstance(red_flags, list):
        for flag in red_flags:
            if isinstance(flag, str) and flag.strip():
                full = flag.strip()
                short = _preview(full) or full
                _add_item(risks, "red_flag", "Red flag", short, body=[full] if short != full else [])


def _consult_items(
    consults: list[dict[str, Any]] | None,
    strengths: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    unestablished: list[dict[str, Any]],
) -> None:
    """The consult block of ``derive_strengths_and_risks``: one entry per domain."""
    for consult, n_in_domain in _latest_consult_per_domain(list(consults or ())):
        label = consult.get("domain") or "consult"
        note = f"latest of {n_in_domain} consults" if n_in_domain > 1 else None
        if consult.get("reply_truncated"):
            _add_item(unestablished, "consult", label, _TRUNCATED_CONSULT_DETAIL, note=note)
            continue
        # `read_state` (migration 0038) has THREE values, and two of them mean
        # the stored `verdict_signal` is not something a specialist said:
        # `truncated` (the reply was cut off) and `defaulted` (the reply
        # arrived complete but `parse_opinion` could not read a signal out of
        # it, so `_DEFAULT_SIGNAL` — `gap` — was substituted). The truncated
        # case is caught above by `reply_truncated`; the DEFAULTED case has
        # `truncated=False` and would otherwise land in `_RISK_SIGNALS` and
        # render under a red glyph as a specialist finding nobody made, which
        # is exactly what point 1 of this function's contract forbids and what
        # `parse_opinion`'s own docstring calls "the laundering that branch
        # exists to prevent". `read_state is None` is a pre-0038 row: the
        # question was never recorded, so it is NOT treated as defaulted and
        # stays on the signal path below, which is the only answer available
        # for it.
        if consult.get("read_state") == "defaulted":
            _add_item(unestablished, "consult", label, _DEFAULTED_CONSULT_DETAIL, note=note)
            continue
        signal = consult.get("verdict_signal")
        if isinstance(signal, str) and signal in _STRENGTH_SIGNALS:
            body = _capped_body(consult.get("established"))
            _add_item(strengths, "consult", label, signal, body=body, note=note,
                 preview=_preview(body[0]) if body else None)
        elif isinstance(signal, str) and signal in _RISK_SIGNALS:
            body = _capped_body(consult.get("concerns"))
            _add_item(risks, "consult", label, signal, body=body, note=note,
                 preview=_preview(body[0]) if body else None)
        else:
            _add_item(unestablished, "consult", label, _UNRECOGNISED_SIGNAL_DETAIL, note=note)


def derive_strengths_and_risks(
    assessment: OpportunityAssessment,
    *,
    dimensions: list[dict[str, Any]] | None,
    consults: list[dict[str, Any]] | None,
    revision: Any,
    revision_provenance: str | None = None,
) -> dict[str, Any]:
    """Four buckets, from STORED values only — never a new judgement.

    Classification, exactly:

    ==========================  ==========================================
    input                       bucket
    ==========================  ==========================================
    dimension score             strength at `>= 0.8 * scale_max` (4 on a
                                1-5 scale); risk at `<= 0.4 * scale_max`
                                (2 on a 1-5 scale)
    dimension score between     `mid_scale`: a real, neutral answer (a 3
                                of 5), listed but bucketed as neither a
                                strength nor a risk — and not an unknown
    dimension score is None     not established: "not scored — counted as
                                zero in the weighted score"
    `gating` value "met"        strength
    `gating` value "not_met"    risk
    `gating` "unconfirmed"      not established: "never asked"
    any other gating value      not established: "unrecognised gating value"
    `red_flags` entry           risk, carrying the flag's own full text
    consult signal `adequate`   strength (and the historical `clear`)
    or `blocking`/`gap`         risk (and the historical `caution`)
    consult `reply_truncated`   not established, REGARDLESS of signal
    consult signal NULL or
    unrecognised                not established: "signal not recognised —
                                nothing can be said about this consult"
    `revision is None`          dimensions contribute NOTHING to any
                                bucket, and `scale_known` is False
    ==========================  ==========================================

    Four things this function is built around, each a defect this repo has
    already paid for once:

    1. **The not-established bucket is not decoration.** `unconfirmed` means
       "never asked", an unscored dimension is not a scored zero, and a truncated
       consult's `verdict_signal` is `src/agent/specialists.py`'s PARSE
       DEFAULT (`gap`) rather than anything a specialist said. Filing any of
       the three as a strength or a risk manufactures a claim nobody made —
       the same error `panel_state`'s five states and
       `OpportunityAssessment.missing_domains`' three states exist to prevent.
    2. **Thresholds come from the ROW's own revision**, via the `revision`
       argument, never from a literal 4 and 2. A hardcoded threshold silently
       relabels every row scored on another scale, which is precisely the
       render-time re-derivation `panel_owed` was added to end.
    3. **Nothing here is stored.** This is presentation of stored values; it
       writes no column and must never be mistaken for a write-time finding.
    4. **It cannot raise.** A malformed `gating` value, a non-string red flag,
       a `scores` dict with a bool in it, a NULL `gating`, a consult dict
       missing a key, a malformed `dimension_rationales` or `gating_rationales`,
       a revision with no (or malformed) gate tables — each degrades into the
       not-established bucket, loses its rationale or definition, or is
       skipped. A brief card must never 500 a page.

    Returns `{"strengths": [...], "risks": [...], "unestablished": [...],
    "mid_scale": [...], "scale_known": bool, "thresholds": {...},
    "mid_scale_count": int, "scored_dimension_count": int}`, each entry being
    `{"source": str, "label": str, "detail": str, "body": list[str],
    "preview": str|None, "note": str|None, "rationale": str|None}` with
    `source` one of `dimension` / `gating` / `red_flag` / `consult`.
    `rationale` is ALWAYS present: the hub's stored one-sentence reason for a
    `dimension` entry (strength, risk, not scored or mid-scale), from
    `dimension_rationales` (migration 0052) matched on the dimension key, or
    for a `gating` entry (every state), from `gating_rationales` (migration
    0058, read through `gating_rationale_map`) matched on the gate key; None
    for red flags and consults, and for a dimension or gate with no stored
    reason — a NULL or malformed column, or a stored key that matches nothing,
    attaches to nothing. `body` is ALWAYS present — an empty list when
    there is nothing stored to quote, so the template can test truthiness
    without `.get`. The template collapses only a consult or red-flag entry
    with a non-empty `body` into a `<details>` whose summary is `label —
    detail`, the `note` badge and the one-line `preview`. A dimension's body
    (its weight) renders inline after its detail; a gate's body (its rubric
    definition) renders as a visible "Rubric definition: …" second line, and
    only when the gate has no `rationale`, which takes that line instead; an
    entry with an empty body is a plain bullet. Consults are ONE ENTRY PER
    DOMAIN, from the domain's latest
    consult (`_latest_consult_per_domain`), with `note` = "latest of N
    consults" when N > 1:

    ==========================  ==========================================
    entry                       `body`
    ==========================  ==========================================
    dimension strength/risk/    `["weight: " + weight_note]`, verbatim
    mid-scale                   (dual-scale notes are not parsed), only when
                                the dimension's `weight` is not None; else `[]`
    dimension not scored        `[]`, unchanged
    gating, every state         `[definition["description"]]`, and `label`
    (met / not_met /            becomes `definition["title"]`, whenever
    unconfirmed / other)        `_gating_definitions` resolves the key: the
                                live document's `[gating.*]` for
                                `PROVENANCE_LIVE`, the registry's
                                `[revision.gating.*]` (3.2.0 and 3.4.0) for
                                `PROVENANCE_ARCHIVED`; otherwise `[]` and the
                                label stays `key.replace("_", " ")`
    red flag                    `detail` is the flag's first sentence
                                (`_preview`, 160 chars); `body` = `[full
                                text]` ONLY when that clip shortened it
    consult adequate/clear      the consult's `established` items (non-empty
                                list of strings only), capped at 3 with an
                                "and N more" tail; else `[]`
    consult blocking/gap/       the consult's `concerns` items, same cap
    caution                     rule; else `[]`
    consult not-established     `[]`, unchanged
    ==========================  ==========================================

    `thresholds` is `{"strength": float|None, "risk": float|None,
    "scale_max": float|None}` — all None whenever `scale_known` is False or
    the revision's `scale_max` is not a usable number. `mid_scale_count` is
    the number of dimensions with a usable score strictly between the risk
    and strength thresholds (0 when the scale is unknown), and
    `scored_dimension_count` the number of dimensions with a usable score at
    all — the template words the mid-scale line as "N of M" and says "All"
    only when the two are equal. `mid_scale_count == len(mid_scale)` always.
    """
    strengths: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    unestablished: list[dict[str, Any]] = []
    mid_scale: list[dict[str, Any]] = []
    scale_known = revision is not None
    thresholds, mid_scale_count, scored_dimension_count = _classify_dimensions(
        dimensions, revision, _dimension_rationale_map(assessment),
        strengths, risks, unestablished, mid_scale,
    )
    _gating_items(assessment, revision, revision_provenance, strengths, risks, unestablished)
    _red_flag_items(assessment, risks)
    _consult_items(consults, strengths, risks, unestablished)

    return {
        "strengths": strengths,
        "risks": risks,
        "unestablished": unestablished,
        "mid_scale": mid_scale,
        "scale_known": scale_known,
        "thresholds": thresholds,
        "mid_scale_count": mid_scale_count,
        "scored_dimension_count": scored_dimension_count,
    }


#: Worst-first ordering for a per-domain chip's colour: a single `blocking`
#: must colour the whole chip, whatever else the domain said.
_SIGNAL_SEVERITY = ("blocking", "gap", "caution", "adequate", "clear")


def summarize_panel_domains(consults: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per domain, in first-seen order:
    ``{"domain", "signals": [(signal, count), ...] in first-seen order,
    "worst": signal | None, "total": int, "cut_off": int}``.

    Truncated consults are counted in ``cut_off`` and NEVER in ``signals`` —
    their stored `verdict_signal` is the parser's default, not an opinion
    (see `_load_consults`' `reply_truncated`). A domain whose every consult
    was cut off therefore has ``signals == []`` and ``worst is None``.
    """
    order: list[str] = []
    per: dict[str, dict[str, Any]] = {}
    for c in consults or ():
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "consult")
        if domain not in per:
            order.append(domain)
            per[domain] = {"domain": domain, "_signals": {}, "_order": [], "total": 0, "cut_off": 0}
        entry = per[domain]
        entry["total"] += 1
        if c.get("reply_truncated"):
            entry["cut_off"] += 1
            continue
        signal = c.get("verdict_signal")
        signal = signal if isinstance(signal, str) and signal else "unknown"
        if signal not in entry["_signals"]:
            entry["_order"].append(signal)
        entry["_signals"][signal] = entry["_signals"].get(signal, 0) + 1
    out: list[dict[str, Any]] = []
    for domain in order:
        entry = per[domain]
        signals = [(sig, entry["_signals"][sig]) for sig in entry["_order"]]
        worst = None
        for candidate in _SIGNAL_SEVERITY:
            if candidate in entry["_signals"]:
                worst = candidate
                break
        if worst is None and signals:
            worst = signals[0][0]
        out.append({
            "domain": domain,
            "signals": signals,
            "worst": worst,
            "total": entry["total"],
            "cut_off": entry["cut_off"],
        })
    return out


def _normalized_scores(assessment: object) -> dict[str, Any]:
    """The stored ``scores`` map with its keys stripped and lower-cased; {} for a
    non-dict. Dimension rows, the review form's ``bot_score`` and the rationale
    lookup all match on these keys."""
    scores = getattr(assessment, "scores", None)
    scores = scores if isinstance(scores, dict) else {}
    return {
        key.strip().lower(): value
        for key, value in scores.items()
        if isinstance(key, str)
    }


def _score_value(raw: object) -> float | None:
    return (
        float(raw)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool)
        else None
    )


def build_dimension_rows(
    assessment: object, revision: RubricRevisionView | None
) -> list[dict[str, Any]]:
    """One row per rubric dimension of ``revision``, then one per stored score key
    the revision does not name (a stored row shows its data, never blanks). Shared
    by the detail page, the chat record (through ``build_assessment_detail``) and
    the directory rows (AP-10)."""
    normalized_scores = _normalized_scores(assessment)

    def _pct(value: float | None) -> float | None:
        # Bar width as a percentage of the revision's scale, clamped — a
        # verdict can carry an out-of-range score and a >100% width would
        # overflow the track. No revision -> no known scale -> no bar.
        if revision is None:
            return None
        if value is None:
            return 0.0
        return min(100.0, max(0.0, value / revision.scale_max * 100.0))

    # The hub's per-dimension reasons (0052), keyed the SAME way the scores map
    # is normalized above — `normalize_dimension_rationales` lower-cases on
    # write, and this lookup must match it or a stored reason renders nowhere.
    rationales = _dimension_rationale_map(assessment)

    dimensions = []
    named_keys: set[str] = set()
    if revision is not None:
        for dim in revision.dimensions:
            value = _score_value(normalized_scores.get(dim.key))
            named_keys.add(dim.key)
            dimensions.append({
                "key": dim.key,
                "title": dim.title,
                "weight": dim.weight,
                "weight_note": dim.weight_note,
                "score": value,
                "pct": _pct(value),
                "rationale": _rationale_for(rationales, dim.key),
            })
    # Score keys the chosen revision does not name still render — a stored row
    # must show its data, never blanks (the pre-registry page dropped a v2
    # row's 13 scores on the floor).
    for key in sorted(normalized_scores):
        if key in named_keys:
            continue
        value = _score_value(normalized_scores[key])
        if value is None:
            continue
        dimensions.append({
            "key": key,
            "title": key.replace("_", " "),
            "weight": None,
            "weight_note": None,
            "score": value,
            "pct": _pct(value),
            "rationale": _rationale_for(rationales, key),
        })
    return dimensions


async def _resolve_pi_user_id(db: AsyncSession, assessment: OpportunityAssessment) -> str | None:
    # Resolves to a live AgentRegistry row's user_id, so the template can
    # link to that PI's profile. Left None for a null subject_agent_id, a
    # stale/decommissioned slug with no AgentRegistry row, or an unlinked
    # agent whose AgentRegistry.user_id is itself NULL — all three are
    # "no link", not an error.
    pi_user_id: str | None = None
    if assessment.subject_agent_id:
        from src.models import AgentRegistry
        row = (await db.execute(
            select(AgentRegistry.user_id)
            .where(AgentRegistry.agent_id == assessment.subject_agent_id)
        )).scalar_one_or_none()
        if row is not None:
            pi_user_id = str(row)
    return pi_user_id


def _company_link(url: object, label: str) -> Markup | None:
    """``url`` as a link built the way ``prose_citations`` builds a citation link
    (class ``citation-link``, the URL in ``title``, ``rel="noreferrer"``, no
    ``target``), or None for anything but an http(s) URL with a host
    (``_is_linkable``). Built here, not in the template, so the template gains no
    ``href`` whose value is a bare expression: tests/unit/test_reachability.py pins
    how many of those exist. ``Markup.format`` escapes both values."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not _is_linkable(url):
        return None
    return Markup(
        '<a class="citation-link" href="{}" title="{}" rel="noreferrer">{}</a>'
    ).format(url, url, label)


def _company_date(value: object) -> date | None:
    """The UTC calendar date of a stored timestamp, or None for a non-datetime."""
    if not isinstance(value, datetime):
        return None
    return (value.astimezone(UTC) if value.tzinfo is not None else value).date()


async def _load_confirmed_companies(
    db: AsyncSession, assessment: OpportunityAssessment
) -> dict[str, Any] | None:
    """The subject PI's staff-confirmed companies for the brief (spec §7.4,
    decision O6), or None when there are none to show.

    `subject_agent_id` -> `AgentRegistry.user_id` -> confirmed `pi_companies` rows,
    through `pi_companies.confirmed_companies_for_agent`, which answers [] for an
    unknown agent id and for a registry row with no user (Review Focus #4): the
    page then simply has no block. Suggested and rejected rows never reach it.
    This is TODAY's list, not what was known when the verdict was written, which
    is why the block carries an as-of date: the newest `reviewed_at` (or
    `created_at`, for a row never reviewed) among the entries. Imported at call
    time, as `_resolve_pi_user_id` imports AgentRegistry: the engine imports this
    module for its normalize_* helpers, and the companies service has no place in
    its import graph. The chat record never reads the key this fills.
    """
    if not assessment.subject_agent_id:
        return None
    from src.services.pi_companies import (
        PI_COMPANY_ROLE_LABELS,
        confirmed_companies_for_agent,
        format_funding,
    )

    companies = await confirmed_companies_for_agent(db, assessment.subject_agent_id)
    if not companies:
        return None
    stamps = [
        stamp
        for stamp in (_company_date(c.reviewed_at or c.created_at) for c in companies)
        if stamp is not None
    ]
    entries = []
    for company in companies:
        role = company.pi_role
        links = [
            link
            for link in (
                _company_link(company.source_url, "source"),
                _company_link(company.funding_source_url, "SEC filings"),
            )
            if link is not None
        ]
        entries.append({
            "name": company.company_name,
            "role": PI_COMPANY_ROLE_LABELS.get(role, str(role).replace("_", " ")),
            "funding": format_funding(
                company.funding_usd, company.funding_as_of, company.funding_source_url
            ),
            "links": links,
        })
    return {"as_of": max(stamps).isoformat() if stamps else None, "entries": entries}


def _message_views(
    messages: list[Any], assessment: OpportunityAssessment
) -> list[dict[str, Any]]:
    """The interview thread's messages as template/timeline dicts."""
    return [
        {
            "key": str(message.id),
            "agent_id": message.agent_id,
            "sender_name": message.sender_name,
            "channel_name": message.channel_name,
            "is_hub": message.agent_id == assessment.agent_id,
            "phase": message.phase,
            "content": message.content,
            "content_normalized": normalize_for_match(message.content),
            "at": _message_at(message),
            "is_verdict_message": bool(
                assessment.slack_ts
                and assessment.slack_ts in (message.slack_ts, message.message_ts)
            ),
        }
        for message in messages
    ]


def _build_timeline(
    message_views: list[dict[str, Any]],
    consults: list[dict[str, Any]],
    matched: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Messages (with their placed tool turns) and consults on one clock."""
    timeline: list[dict[str, Any]] = []
    for view in message_views:
        timeline.append({
            "kind": "message",
            "at": view["at"],
            "message": view,
            "tool_turns": matched.get(view["key"], []),
        })
    for consult in consults:
        timeline.append({
            "kind": "consult",
            "at": _epoch(consult["created_at"]),
            "consult": consult,
        })
    # Stable sort: messages were appended before consults, so a consult and the
    # reply it informed landing on the same timestamp read message-then-consult
    # rather than in an arbitrary order.
    timeline.sort(key=lambda entry: entry["at"])
    return timeline


def _retro_consult_count(matched: dict[str, list[dict[str, Any]]]) -> int:
    # Counted over PLACED turns only, not over every scanned turn.
    # `_load_tool_turns` excludes rows stamped with another thread's
    # `llm_call_logs.thread_ts`, but rows logged before 0042 carry a NULL
    # `thread_ts` and are admitted on the (run, phase, agent, channel, time
    # window) heuristic alone. Several interviews share a channel, so those
    # rows can still be other threads' turns, which `correlate_turns_to_messages`
    # hands back as `unplaced`. Summing over `turns` would attribute other
    # interviews' consults to this one: production run 60c53424's kevrekidis
    # assessment reported 11 against 7 real consults under the heuristic alone,
    # the difference being its 4 unplaced turns exactly. Unplaced turns are
    # still SHOWN, under their own heading — they are evidence of what the hub
    # did — they are just not counted as this interview's panel.
    return sum(
        1
        for placed in matched.values()
        for turn in placed
        for chip in turn["chips"]
        if chip["is_consult"]
    )


async def _load_review_context(
    db: AsyncSession, assessment: OpportunityAssessment, viewer_is_staff: bool
) -> tuple[list[Any], list[Any], list[Any], list[User]]:
    """Feedback, status history, assignments and (staff only) the assignee roster."""
    review_feedback = await _load_review_feedback(db, assessment.id)
    review_status_history = await _load_review_status_history(db, assessment.id)
    review_assignments = await _load_review_assignments(db, assessment.id)
    review_capable_users = (
        await _load_review_capable_users(db) if viewer_is_staff else []
    )
    return review_feedback, review_status_history, review_assignments, review_capable_users


def _review_rubric(normalized_scores: dict[str, Any]) -> dict[str, Any]:
    # The scoring form's own source of truth: the LIVE document, because that
    # is what the reviewer is about to score against and what
    # `submit_feedback` will stamp on the row. Deliberately NOT the revision
    # that scored the assessment — a human reviewing a v3.2.0 verdict today is
    # giving a v3.4.0 opinion, and the stamp on their row must say so.
    # `bot_score` rides along per dimension (A9): disagreement has to be
    # visible where the human is choosing, and it is labelled as the bot's.
    live_rubric = load_rubric()
    return {
        "version": live_rubric.version,
        "scale_min": live_rubric.scale_min,
        "scale_max": live_rubric.scale_max,
        "dimensions": [
            {
                "key": d.key,
                "title": d.title,
                "weight": d.weight,
                "anchors": d.anchors,
                "bot_score": _score_value(normalized_scores.get(d.key)),
            }
            for d in live_rubric.dimensions
        ],
    }


async def build_assessment_detail(
    db: AsyncSession,
    assessment_id: uuid.UUID,
    *,
    admin_view: bool,
    viewer_is_staff: bool = False,
) -> dict[str, Any] | None:
    """One assessment, its dimension breakdown, and its interview timeline.

    Returns None when there is no such assessment (the router owns the 404 —
    this module stays HTTP-free, like src/services/directory.py).

    ``admin_view=False`` omits every admin-only value from the returned
    context: no ``raw_opinion``, no tool activity. See the module docstring.

    ``viewer_is_staff`` gates ``review_capable_users`` (the assignee roster
    for the Human-review card's assign form): it is queried ONLY when True,
    so a reviewer's render never enumerates the staff/reviewer roster — the
    same rule ``src/routers/manager.py``'s own PI-add form applies, and it
    saves a query on every non-staff render.
    """
    assessment = (
        await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == assessment_id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        return None

    pi_user_id = await _resolve_pi_user_id(db, assessment)
    confirmed_companies = await _load_confirmed_companies(db, assessment)

    revision, revision_provenance = resolve_revision(
        assessment.rubric_version, assessment.rubric_content_hash
    )
    normalized_scores = _normalized_scores(assessment)
    dimensions = build_dimension_rows(assessment, revision)

    thread_id, messages = await load_interview_thread(db, assessment)
    consults = await _load_consults(db, assessment, thread_id, admin_view=admin_view)

    message_views = _message_views(messages, assessment)

    turns: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    matched: dict[str, list[dict[str, Any]]] = {}
    logs_scanned = 0
    if admin_view and message_views:
        turns, logs_scanned = await _load_tool_turns(
            db, assessment, message_views, thread_id=thread_id,
        )
        matched, unplaced = correlate_turns_to_messages(turns, message_views)

    timeline = _build_timeline(message_views, consults, matched)
    retro_consult_count = _retro_consult_count(matched)

    (
        review_feedback, review_status_history, review_assignments, review_capable_users,
    ) = await _load_review_context(db, assessment, viewer_is_staff)
    review_rubric = _review_rubric(normalized_scores)

    return {
        "assessment": assessment,
        "pi_user_id": pi_user_id,
        # The subject PI's staff-confirmed companies for the brief (spec §7.4),
        # or None. Page-only: the chat record never reads it.
        "confirmed_companies": confirmed_companies,
        "dimensions": dimensions,
        "revision": revision,
        "revision_provenance": revision_provenance,
        "scale_max": revision.scale_max if revision is not None else None,
        "banding": BANDING,
        "rubric_version": RUBRIC_VERSION,
        "panel_state": panel_state(assessment),
        # The chips under the panel-state box. `reply_truncated` rides along
        # with the signal it qualifies: this row of chips is the compact answer
        # to "was this verdict's panel real", so a chip whose opinion was never
        # finished has to say so here, not only on the card further down.
        "panel_summary": [
            {
                "domain": c["domain"],
                "verdict_signal": c["verdict_signal"],
                "reply_truncated": c["reply_truncated"],
            }
            for c in consults
        ],
        # The same chips GROUPED per domain (2026-09-15 visual audit M6): a
        # real interview has 25-40 consults, and 37 identical 12px chips in
        # four rows were unreadable as a set. One chip per domain carries the
        # signal tally in chronological order; a domain whose replies were
        # cut off gets its own neutral chip, so the "reply cut off" marker is
        # never merged into an opinion tally.
        "panel_domains": summarize_panel_domains(consults),
        # Live gate descriptions, LIVE rows only, read by the assessment chat
        # record (`assessment_chat_record._gating_section`). Deliberately kept to
        # that meaning when archived rows gained definitions (spec §5.3):
        # widening it would change the chat model's input for every existing
        # archived row. The page reads `gating_definitions` below instead.
        "gating_descriptions": (
            {k: v for k, v in load_rubric().gating.items()}
            if revision_provenance == PROVENANCE_LIVE else {}
        ),
        # PAGE-ONLY (hub 1.10.0, spec §5.2-§5.3); the chat record reads neither.
        # `gating_definitions`: the title and description the Evidence summary
        # and the gating card may show for this row's gates — the live
        # document's for a live row, the registry's [revision.gating.*] for an
        # archived row that has them (3.2.0, 3.4.0), {} otherwise
        # (`_gating_definitions`). `gating_reasons`: the hub's stored reason per
        # gate (0058), keyed by this row's own `gating` keys.
        "gating_definitions": _gating_definitions(revision, revision_provenance),
        "gating_reasons": _gating_reasons(assessment),
        # Request 3 / D2: the strengths-risks-not-established brief, DERIVED
        # from the three things already resolved above and stored nowhere.
        "verdict_signals": derive_strengths_and_risks(
            assessment,
            dimensions=dimensions,
            consults=consults,
            revision=revision,
            revision_provenance=revision_provenance,
        ),
        "consult_count": len(consults),
        "retro_consult_count": retro_consult_count,
        "thread_id": thread_id,
        "messages_available": bool(message_views),
        "timeline": timeline,
        "unplaced_turns": unplaced,
        "logs_scanned": logs_scanned,
        "log_scan_limit": LOG_SCAN_LIMIT,
        "admin_view": admin_view,
        # The hub's own `strengths`/`risks` bullets (0049) — and, since
        # scout_hub 1.7.0, `competitive_landscape`/`evidence_maturity` (0050)
        # — are STAFF-only on the page, matching what the prompt promises the
        # model: a reviewer account reaches the manager detail route but must
        # not see model text the hub was told may cite unpublished results or
        # Blackbird's own commercial diligence. All four are gated on this one
        # key in `templates/admin/_assessment_detail_body.html`; a fifth such
        # field belongs in the same place.
        "viewer_is_staff": viewer_is_staff,
        # Human-review card. All three review tables are ordered
        # (created_at, id) — Postgres `now()` is transaction-start, so ties
        # inside one write burst are real and `id` is the tiebreak.
        # ``review_status`` is the LATEST event (last of the ordered history),
        # or None when the assessment has never had one recorded.
        "review_feedback": review_feedback,
        "review_status": review_status_history[-1] if review_status_history else None,
        "review_status_history": review_status_history,
        "review_assignments": review_assignments,
        "review_capable_users": review_capable_users,
        "review_rubric": review_rubric,
        "revision_provenance_unknown": PROVENANCE_UNKNOWN,
    }


def _review_dimension_rows(review: AssessmentReview) -> tuple[list[dict], str]:
    """One reviewer's stored per-dimension scores, titled by the revision THEY
    scored against — never by today's document.

    Rubric v3.0.0 replaced thirteen dual-scale dimensions with six single-scale
    ones, so a dimension key is only meaningful against its own revision. An
    unresolvable stamp renders the keys as stored, untitled: silently remapping
    them onto today's dimensions would manufacture a claim nobody made (A13).
    Returns ``([], "")`` for a review that scored no dimensions.
    """
    scores = (
        review.dimension_scores if isinstance(review.dimension_scores, dict) else {}
    )
    if not scores:
        return [], ""
    # `resolve_revision(None, None)` returns `(live, PROVENANCE_UNSTAMPED)`,
    # not `(None, PROVENANCE_UNKNOWN)` — an unstamped row would title its
    # keys from TODAY's document with no warning, which is exactly what this
    # function exists to avoid. That combination (scores present, both stamp
    # columns NULL) is unreachable in practice, by two independent facts
    # rather than one: `submit_feedback`/`edit_feedback` always
    # stamp both columns whenever a row is written, so any row that HAS
    # `dimension_scores` also has a stamp; and every row written before
    # `dimension_scores` existed (pre-0043) has it NULL, which the `if not
    # scores` guard above already returns on before `resolve_revision` is
    # ever called. Only a hand-built fixture or manual SQL could produce
    # scores with no stamp — not deliberately guarded against here, because
    # code defending an unreachable state would be untestable.
    revision, provenance = resolve_revision(
        review.rubric_version, review.rubric_content_hash
    )
    titles = (
        {d.key: d.title for d in revision.dimensions} if revision is not None else {}
    )
    rows = [
        {"key": key, "title": titles.get(key, key), "score": scores[key]}
        for key in sorted(scores)
    ]
    return rows, provenance


async def _load_review_feedback(
    db: AsyncSession, assessment_id: uuid.UUID
) -> list[AssessmentReview]:
    """Every human review-feedback row for this assessment, oldest first."""
    rows = (
        await db.execute(
            select(AssessmentReview)
            .where(AssessmentReview.assessment_id == assessment_id)
            .order_by(AssessmentReview.created_at, AssessmentReview.id)
        )
    ).scalars().all()
    rows = list(rows)

    # F4: the impersonation note NAMES the admin who entered the review.
    # "an admin" alone is not an attribution anyone can act on — with several
    # admins it identifies nobody. One lookup for the whole page rather than
    # one per row; a missing id (the FK is ON DELETE SET NULL, but a stale
    # read can still miss) falls back to "an admin" in the template.
    recorder_ids = {r.recorded_by_user_id for r in rows if r.recorded_by_user_id}
    names: dict[uuid.UUID, str] = {}
    if recorder_ids:
        names = {
            uid: name
            for uid, name in (
                await db.execute(
                    select(User.id, User.name).where(User.id.in_(recorder_ids))
                )
            ).all()
        }

    for row in rows:
        # Not mapped columns — ordinary instance attributes on read-only rows
        # handed straight to a template. Nothing is persisted.
        row.dimension_rows, row.dimension_provenance = _review_dimension_rows(row)
        row.recorded_by_name = names.get(row.recorded_by_user_id)
    return rows


async def _load_review_status_history(
    db: AsyncSession, assessment_id: uuid.UUID
) -> list[AssessmentReviewEvent]:
    """The full approve/disapprove/clear audit trail, oldest first. The last
    entry is the current status — see ``build_assessment_detail``'s
    ``review_status`` key."""
    rows = (
        await db.execute(
            select(AssessmentReviewEvent)
            .where(AssessmentReviewEvent.assessment_id == assessment_id)
            .order_by(AssessmentReviewEvent.created_at, AssessmentReviewEvent.id)
        )
    ).scalars().all()
    return list(rows)


async def _load_review_assignments(
    db: AsyncSession, assessment_id: uuid.UUID
) -> list[AssessmentReviewAssignment]:
    """Everyone currently assigned to review this assessment, oldest first."""
    rows = (
        await db.execute(
            select(AssessmentReviewAssignment)
            .where(AssessmentReviewAssignment.assessment_id == assessment_id)
            .order_by(AssessmentReviewAssignment.created_at, AssessmentReviewAssignment.id)
        )
    ).scalars().all()
    return list(rows)


async def _load_review_capable_users(db: AsyncSession) -> list[User]:
    """The assignee roster for the assign form: every allowed staff or
    reviewer account. Callers must gate this on ``viewer_is_staff`` — see the
    module docstring's redaction note and ``build_assessment_detail``.
    """
    rows = (
        await db.execute(
            select(User)
            .where(
                or_(User.is_staff, User.is_reviewer),
                User.access_status == "allowed",
            )
            .order_by(User.name, User.id)
        )
    ).scalars().all()
    return list(rows)


async def _load_consults(
    db: AsyncSession,
    assessment: OpportunityAssessment,
    thread_id: str | None,
    *,
    admin_view: bool,
) -> list[dict[str, Any]]:
    """Recorded panel consults for this interview.

    Keyed on (run, thread) — never on the channel alone, which in production
    holds several interviews. Empty for every assessment that predates the
    table, which is what the read-time tool-log parse exists to cover.
    """
    if thread_id is None:
        return []
    rows = (
        await db.execute(
            select(SpecialistConsult)
            .where(
                SpecialistConsult.simulation_run_id == assessment.simulation_run_id,
                SpecialistConsult.thread_id == thread_id,
            )
            .order_by(SpecialistConsult.created_at)
        )
    ).scalars().all()
    return [
        {
            "domain": row.domain,
            "verdict_signal": row.verdict_signal,
            "confidence": row.confidence,
            "question": row.question,
            "concerns": list(row.concerns or []),
            # Alongside the list, because the chip row shows the signal only.
            # `adequate` (2026-08-28) means "meets the bar for THIS STAGE" and
            # NOT "no concerns" — every `clear` opinion ever emitted carried 4-9
            # of them, one of which read "Succession risk is high as described"
            # under a ✅. The count is what keeps the label honest at a glance.
            "concern_count": len(list(row.concerns or [])),
            "questions_to_ask": list(row.questions_to_ask or []),
            # ADMIN ONLY (plan decision 2). Managers get the signal and the
            # structured lists; the specialist's verbatim text is drill-down,
            # so it is dropped HERE rather than merely left unrendered — a
            # value that reaches the context reaches anyone who can read the
            # page source the moment a template edit prints it.
            "raw_opinion": row.raw_opinion if admin_view else None,
            # Was this reply CUT OFF (0036's `specialist_consults.truncated`)?
            # Carried, not dropped, because `verdict_signal` above is a PARSE
            # DEFAULT on a truncated reply — `gap`, from
            # src/agent/specialists.py — and a chip that says `gap` is
            # indistinguishable from one a specialist actually gave. The
            # specialist floor, the Slack panel note and the durable row all
            # already know; this page was the last reader that did not, and it
            # is the one a human reads to decide whether a verdict's panel was
            # real.
            #
            # Named `reply_truncated`, not `truncated`: `thread_panel.py`'s
            # card dicts carry the same key, and there "truncated" already
            # means two other things (the row cap, and `raw_opinion` clipped
            # for display). One name across both card renderers, and none of
            # the three collide.
            #
            # NULL stays falsy on purpose — see the column's comment: NULL is
            # "written before 0036", read as not-truncated, because
            # retroactively invalidating history on no evidence is worse.
            "reply_truncated": bool(row.truncated),
            # 0038's `read_state`, carried verbatim INCLUDING None. None means
            # "written before 0038" — a third state, not "parsed" — so the
            # decision about what to show for it stays in the template rather
            # than being guessed at here. Same key and same rule as
            # `thread_panel.py`'s card dicts, so the two card renderers still
            # read alike.
            "read_state": row.read_state,
            # 0038's positive-evidence field, carried verbatim: NULL means
            # "never asked", `[]` means "asked, nothing came back" (or the key
            # was ignored — the two are indistinguishable), and only a
            # non-empty list is evidence. Coerced to a plain list rather than
            # left as whatever the JSONB driver hands back, so a downstream
            # `isinstance(..., list)` check behaves the same as it does for
            # `concerns`/`questions_to_ask` above.
            "established": list(row.established) if isinstance(row.established, list) else None,
            "created_at": row.created_at,
        }
        for row in rows
    ]


#: B-07: the tool_use / tool_result blocks of a logged hub turn, extracted in SQL, as
#: `[{"content": [block, ...]}, ...]` in conversation order (NULL when there are
#: none). `tool_chips_from_conversation` reads nothing else, so the page no longer
#: transfers the thinking and text blocks, which are the bulk of the column's bytes.
#: `messages_json` is `json` (not `jsonb`); a non-array value or a non-array
#: `content` contributes nothing, as in the Python reader.
TOOL_BLOCKS_SLICE = literal_column(
    """(SELECT json_agg(json_build_object('content', kept.blocks) ORDER BY msg.ord)
        FROM json_array_elements(
               CASE WHEN json_typeof(llm_call_logs.messages_json) = 'array'
                    THEN llm_call_logs.messages_json ELSE '[]'::json END
             ) WITH ORDINALITY AS msg(value, ord)
        CROSS JOIN LATERAL (
          SELECT json_agg(blk.value ORDER BY blk.ord) AS blocks
          FROM json_array_elements(
                 CASE WHEN json_typeof(msg.value -> 'content') = 'array'
                      THEN msg.value -> 'content' ELSE '[]'::json END
               ) WITH ORDINALITY AS blk(value, ord)
          WHERE blk.value ->> 'type' IN ('tool_use', 'tool_result')
        ) AS kept
        WHERE kept.blocks IS NOT NULL)""",
    type_=JSON,
).label("tool_slice")


async def _load_tool_turns(
    db: AsyncSession,
    assessment: OpportunityAssessment,
    message_views: list[dict[str, Any]],
    *,
    thread_id: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The hub's logged tool conversations for this thread's time span.

    Rows are selected by (run, phase, agent, channel, time window). When
    ``thread_id`` is given, a row stamped with a DIFFERENT
    ``llm_call_logs.thread_ts`` is excluded as well, before the
    ``LOG_SCAN_LIMIT`` cap applies, so other interviews in the same channel can
    neither appear as unplaced turns nor crowd this thread's own turns out of
    the scan. Rows with a NULL ``thread_ts`` (logged before 0042, never
    backfilled) stay admitted on the time-window heuristic alone. The returned
    count is the number of rows scanned under that filter.

    ``system_prompt`` is deliberately NOT selected: it is the largest column in
    the table, it is already readable on the LLM-calls page, and nothing here
    renders it — loading it would put tens of kilobytes per row into the
    template context for nothing. Nor is the whole ``messages_json``: only its
    tool blocks, extracted in SQL (``TOOL_BLOCKS_SLICE``, B-07).
    """
    channel = message_views[0].get("channel_name") or assessment.channel_name
    first = min(view["at"] for view in message_views)
    last = max(view["at"] for view in message_views)
    query = (
        select(
            LlmCallLog.id,
            LlmCallLog.created_at,
            LlmCallLog.model,
            TOOL_BLOCKS_SLICE,
            LlmCallLog.response_text,
        )
        .where(
            LlmCallLog.simulation_run_id == assessment.simulation_run_id,
            LlmCallLog.phase == "thread_reply",
            LlmCallLog.agent_id == assessment.agent_id,
            LlmCallLog.channel == channel,
            LlmCallLog.created_at
            >= datetime.fromtimestamp(first - LOG_WINDOW_PAD_SECONDS, UTC),
            LlmCallLog.created_at
            <= datetime.fromtimestamp(last + LOG_WINDOW_PAD_SECONDS, UTC),
        )
    )
    if thread_id is not None:
        query = query.where(
            or_(LlmCallLog.thread_ts == thread_id, LlmCallLog.thread_ts.is_(None))
        )
    query = query.order_by(LlmCallLog.created_at.desc()).limit(LOG_SCAN_LIMIT)
    # Newest LOG_SCAN_LIMIT rows, not earliest: dropping the newest turns would
    # lose the concluding turn's consults, the most load-bearing ones, while the
    # banner tells the admin these are the "most recent" scanned turns. Fetch
    # descending so the LIMIT keeps the newest, then reverse in Python so
    # display stays chronological.
    rows = list(reversed((await db.execute(query)).all()))
    turns = []
    for row in rows:
        chips = tool_chips_from_conversation(row.tool_slice)
        if not chips:
            # A turn that called no tool adds nothing the message itself does
            # not already say.
            continue
        turns.append({
            "log_id": str(row.id),
            "at": _epoch(row.created_at),
            "created_at": row.created_at,
            "model": row.model,
            "body": visible_body(row.response_text),
            "chips": chips,
        })
    return turns, len(rows)
