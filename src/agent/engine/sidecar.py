"""Verdict-sidecar and reply-text parsing helpers. Their regexes and limits live here
too, so the leaves never import each other."""

import json
import logging
import re
from typing import Any

logger = logging.getLogger("src.agent.simulation")


def _extract_slack_message(text: str) -> str:
    """Extract the message from <slack_message> tags if present, else fall back to preamble stripping.

    Uses the LAST opening tag before the LAST closing tag so that a prior
    mention of ``<slack_message>`` inside the LLM's reasoning (e.g.
    "my output is a single `<slack_message>` block") does not anchor the
    match and pull preceding reasoning into the captured body.
    """
    last_close = text.rfind("</slack_message>")
    if last_close >= 0:
        last_open = text.rfind("<slack_message>", 0, last_close)
        if last_open >= 0:
            return text[last_open + len("<slack_message>"):last_close].strip()
    # Fallback: strip preamble heuristically
    return _strip_llm_preamble(text)


def _reply_closes_thread(text: str) -> bool:
    """True if this reply ENDS the interview — the one definition of the ⏸️
    no-viable-collaboration close.

    ``_check_thread_outcome`` acts on it (``_close_thread(..., "no_proposal")``)
    and ``_capture_hub_assessment`` reads it a few lines earlier, via the
    ``closes_thread`` argument ``_reply_to_thread`` hoists. Those two MUST agree:
    the prompts tell the hub to deliver a negative verdict by opening with ⏸️,
    so a sidecar on a closing reply is the interview's real, final verdict and
    there will be no later turn to supply another. When the gate refused those
    (they are not on ordinal 12) the verdict was destroyed — measured in run
    076e80b6, where 4 of 5 ``premature_sidecar`` refusals were the thread's
    terminal message.

    Deliberately permissive about WHERE the marker appears, matching what this
    check has always done: the thread really is closed by any ⏸️ in the reply,
    so the capture gate has to treat any ⏸️ as terminal too. The stricter
    front-of-string variant is ``_reply_opens_with_pause``.
    """
    body = text or ""
    return "⏸️" in body or ":pause_button:" in body


def _reply_opens_with_pause(text: str) -> bool:
    """True if ``text`` follows the ⏸️ no-viable-collaboration convention
    thread_guidance.py's DECIDE/CONCLUDE instructions ask for verbatim
    ("start your reply with ⏸️") — checked at the front of the (stripped)
    string, not merely present anywhere in it.

    Deliberately stricter than ``_reply_closes_thread`` just above: that one
    exists to actually close the thread (and to tell the capture gate that this
    reply is the interview's last) and is intentionally permissive about where
    the marker appears, while this one is asking "did the model follow the
    documented opening convention" for
    ``_warn_if_hub_conclude_missing_assessment``'s absent-sidecar detection — a
    marker buried mid-reply would not have been the ⏸️-only decline
    thread_guidance describes.
    """
    stripped = (text or "").strip()
    return stripped.startswith("⏸️") or stripped.startswith(":pause_button:")


# Case-insensitive and tolerant of stray whitespace inside the delimiters
# (e.g. `<ASSESSMENT_JSON>`, `<assessment_json >`) — a model is not guaranteed
# to reproduce the tag verbatim, and a tag variant that slips past these
# regexes is a verdict that leaks straight into Slack.
_ASSESSMENT_RE = re.compile(
    r"<\s*assessment_json\s*>\s*(.*?)\s*<\s*/\s*assessment_json\s*>",
    re.DOTALL | re.IGNORECASE,
)

# An opening tag with no matching close — e.g. the LLM response got truncated
# mid-sidecar (Phase 5's max_tokens budget plus an 11-section body ahead of a
# ~15-line sidecar makes this a realistic outcome, and the retry path does not
# re-check stop_reason). `_ASSESSMENT_RE` requires a literal closing tag, so it
# does not match an unclosed one and would leave the raw verdict JSON — scores,
# red flags, recommendation — sitting in the text. Strip everything from the
# orphaned opening tag to the end of the response instead.
#
# This can discard trailing legitimate prose that happened to follow the
# sidecar. That is an accepted, deliberate trade-off: losing a sentence of
# prose is strictly better than leaking dimension scores and red flags into a
# channel the assessed scientist reads. Do not "optimize" this into a lazy
# match that stops short of end-of-string — the whole point is to consume
# unconditionally to the end once an unclosed opening tag is found.
_ASSESSMENT_UNCLOSED_RE = re.compile(
    r"<\s*assessment_json\s*>.*", re.DOTALL | re.IGNORECASE
)

# Mop-up for any stray tag markup neither of the above removed (e.g. an
# orphaned closing tag with no opening).
_ASSESSMENT_ORPHAN_TAG_RE = re.compile(
    r"<\s*/?\s*assessment_json\s*>", re.IGNORECASE
)


def _strip_assessment_sidecar(text: str) -> str:
    """Remove the <assessment_json> sidecar from ``text`` before it reaches Slack.

    That block is for Blackbird staff and the DB, never for the channel. Order
    matters:
      1. Remove well-formed pairs whole (tags + contents).
      2. Anything left starting with an opening tag has no matching close —
         truncated mid-sidecar — so drop from there to the end of the text
         rather than leave the verdict JSON exposed.
      3. Mop up any remaining stray tag markup neither step removed.
    """
    text = _ASSESSMENT_RE.sub("", text)
    text = _ASSESSMENT_UNCLOSED_RE.sub("", text)
    text = _ASSESSMENT_ORPHAN_TAG_RE.sub("", text)
    return text


# A model routinely wraps the sidecar's JSON in a ```json``` fence despite the
# prompt asking for bare JSON (see _parse_phase5_response, which now strips
# the whole <assessment_json>...</assessment_json> span — fence included —
# before it ever looks for the action). That strip protects the action parse
# unconditionally, but it would be a shame to also throw the verdict away
# just because it arrived fenced: tolerate one optional wrapping fence here
# too, so the verdict itself still comes through.
_SIDECAR_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _unfence_sidecar(raw: str) -> str:
    """Strip one optional ```json```/``` fence wrapping ``raw``, if present."""
    match = _SIDECAR_FENCE_RE.match(raw.strip())
    return match.group(1) if match else raw


def _extract_assessment_json(text: str) -> dict | None:
    """Parse the scout hub's machine-readable verdict sidecar, or None.

    The sidecar is deliberately BARE JSON, not a ```json``` fence:
    _parse_phase5_response strips this whole tagged span before it ever looks
    for the action fence, so a fenced sidecar would otherwise hijack the
    action data and silently no-op every assessment post. A fenced sidecar is
    still tolerated here (see _unfence_sidecar) — the action is already
    protected regardless, so there is no reason to also lose the verdict over
    a fence the model added despite the instruction not to.

    Walks blocks newest-first and returns the first one that parses to a JSON
    object — "the newest verdict that is actually usable", not "the newest
    block, or nothing": a model that emits a good verdict and then a broken
    revision (invalid JSON, or valid JSON that isn't an object, e.g. an array)
    must not lose the good one just because it revised afterward. Last-wins is
    still right when a revision *does* parse — it should supersede the
    earlier verdict, which is exactly what returning on the first hit here
    does. Returns None only when no block parses to a dict; never raises.
    """
    matches = _ASSESSMENT_RE.findall(text or "")
    if not matches:
        return None
    last_index = len(matches) - 1
    for index in range(last_index, -1, -1):
        try:
            parsed = json.loads(_unfence_sidecar(matches[index]))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "[assessment] sidecar block %d/%d unparseable: %s",
                index + 1, len(matches), exc,
            )
            continue
        if not isinstance(parsed, dict):
            logger.warning(
                "[assessment] sidecar block %d/%d parsed but was not a JSON "
                "object (got %s)",
                index + 1, len(matches), type(parsed).__name__,
            )
            continue
        if index != last_index:
            logger.warning(
                "[assessment] using sidecar block %d/%d — %d later block(s) "
                "were present but unusable, so an earlier valid verdict was "
                "used instead of losing it",
                index + 1, len(matches), last_index - index,
            )
        return parsed
    return None


def _sidecar_has_valid_json_block(text: str) -> bool:
    """True if any <assessment_json> block in ``text`` is syntactically valid
    JSON, whatever its shape.

    Lets a caller distinguish "no block ever parsed" from "a block parsed
    fine but wasn't an object" (Finding A3) — the two outcomes both leave
    ``_extract_assessment_json`` returning None, but only the first is
    actually "unparseable".
    """
    for raw in _ASSESSMENT_RE.findall(text or ""):
        try:
            json.loads(_unfence_sidecar(raw))
        except (json.JSONDecodeError, ValueError):
            continue
        return True
    return False


# The prompt's tri-state gating contract (see the <assessment_json> skeleton in
# prompts/roles/scout_hub/phase4-thread-reply.md — relocated there from the
# deleted phase5-new-post.md by the 2026-08-12 removal cycle's reply-only-hub
# reconciliation): every gating.* value must be exactly one of these three
# strings, never a bare boolean — "the PI declined" (not_met) and "we never
# asked" (unconfirmed) are different facts, and a boolean can express only the
# first two of these three outcomes.
_VALID_GATING_STATES = frozenset({"met", "not_met", "unconfirmed"})

#: Contract bounds from prompts/roles/scout_hub/phase4-thread-reply.md items
#: 6-7. SOFT: exceeding one logs a WARNING and stores the value as emitted.
#: Enforcing them by dropping would trade a long headline for a lost verdict,
#: and the row is the archive.
_HEADLINE_SOFT_LIMIT = 110
#: `company_or_project` is the SHORT label, and it is the only project field the
#: public #assessments-summary headline renders — where it is clipped to
#: PROJECT_DISPLAY_CHARS (120, src/services/assessment_headline.py). A label
#: over this bound is stored in full and warned about; one over 120 loses its
#: tail in Slack.
_PROJECT_SOFT_LIMIT = 70
#: The pitch's own soft bound, raised from 900 CHARACTERS to 250 WORDS on
#: 2026-09-28 at the operator's request (scout_hub 1.9.0), counted as
#: `len(text.split())`. Measured before the change: the five verdicts written
#: under scout_hub 1.8.0 carried 113-222-word pitches (797-961 characters), so
#: 900 characters was in practice a ~150-word bound.
#: The public excerpt did NOT move with it: PITCH_DISPLAY_CHARS (600,
#: src/services/assessment_headline.py) still clips what reaches
#: #assessments-summary, and item 8 still asks that sentences 1-4 END within
#: ~550 characters so the provenance citation completes inside that window.
#: A longer pitch makes that harder, not easier, which is why the
#: citation-loss alarm in `_persist_assessment` is now the load-bearing check
#: rather than this one.
_PITCH_WORD_LIMIT = 250
_KEY_POINTS_MIN = 3
_KEY_POINTS_MAX = 5
# scout_hub >= 1.9.0: the exact bullet count each current group carries
# (prompt item 7 — one each since 1.9.0), and the per-bullet bound. Warnings
# only (D12 of docs/specs/2026-09-24-reviewer-rubric-and-key-points-design.md)
# — a shape violation is never a drop. Keys and order are pinned to
# KEY_POINT_GROUPS by tests/unit/test_rubric_prompt_sync.py. The
# retired 1.8.0 group (`key_questions`, RETIRED_KEY_POINT_GROUPS) carries no
# count: it still stores, with its own warning in `_persist_assessment`.
_KEY_POINT_GROUP_BULLETS = {
    "indication_audience": 1,
    "lab_background": 1,
    "proposal": 1,
    "clinical_actionability": 1,
    "path_to_clinic": 1,
    "commercial_opportunity": 1,
}
_KEY_POINT_BULLET_CHARS = 300
#: What counts as a CITATION in `elevator_pitch` for the drift alarm in
#: `_persist_assessment`. Item 8 of phase4-thread-reply.md asks for the source
#: "cited the way the lab's own public profile cites it (DOI or PubMed link)",
#: and every one of these forms is legal under that wording — so testing for
#: `http` alone would stay silent for a bare `doi:10.7554/...` or a `PMID`,
#: which is precisely the citation most likely to be written without a scheme.
_PITCH_CITATION_RE = re.compile(
    r"https?://\S+|\bdoi:\s*\S+|\b10\.\d{4,9}/\S+|\bPMID:?\s*\d+",
    re.I,
)
#: Sidecar items 11/12 (scout_hub >= 1.5.0): the hub's own strengths/risks
#: bullets — and, since 1.7.0, items 13/14 as well, `competitive_landscape`
#: and `evidence_maturity`. All four share these bounds and are checked by the
#: one loop in `_persist_assessment`.
#: SOFT bounds, same policy as the key_points groups above — a
#: contract violation is warned about and stored as emitted, never dropped
#: for a count/length reason alone (only `normalize_bullets` itself drops the
#: whole field, and only for a genuine shape violation).
_HUB_BULLETS_MIN = 2
_HUB_BULLETS_MAX = 4
_HUB_BULLET_CHARS = 200
#: Sidecar item 2's companion (scout_hub >= 1.9.0, migration 0052): one
#: sentence per dimension. Warnings only, like every other shape check here.
_DIMENSION_RATIONALE_CHARS = 200


def _normalize_gating(raw: object) -> dict | None:
    """Filter a verdict's ``gating`` map down to the keys already conforming to
    the tri-state contract; drop only the keys that don't.

    The ``gating`` column is plain JSONB, so nothing at the database layer
    stops a pre-tri-state boolean value — or a genuinely malformed one — from
    being written verbatim. A key that sometimes holds ``true``/``false`` and
    sometimes holds ``"met"``/``"not_met"``/``"unconfirmed"`` is worse than one
    that is occasionally absent: a consumer cannot tell which convention a
    given key uses without inspecting its value, which defeats the point of a
    structured column. So each key is kept only when its own value already
    conforms; anything else is dropped for that key alone.

    Filtering per key rather than dropping the whole map matches how every
    sibling field on this row already degrades —
    ``red_flags``/``derisking_milestones`` null only when THEY are the wrong
    type, never because some unrelated field was also bad. Wholesale-dropping
    a map with three good gates over one bad one denied the (now-shipped)
    triage page three gates it could have shown, for no correctness benefit:
    the reason to refuse the bad key stands on its own (see below) and has
    nothing to do with its siblings.

    Booleans are deliberately NOT coerced (``True`` -> "met", ``False`` ->
    "not_met"): under the old boolean-only contract there was no way to say
    "unconfirmed" at all, so a legacy ``False`` is genuinely ambiguous between
    "not_met" and "unconfirmed" — guessing would fabricate a certainty the
    original verdict never had, so that key is omitted rather than guessed.
    This never loses information regardless: ``raw_verdict`` keeps the
    original ``gating`` value verbatim no matter what survives here.

    Returns ``None`` when ``raw`` isn't a dict at all, or when no key survives
    filtering — an empty structured map is no more useful than a missing one.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    kept = {
        key: value for key, value in raw.items()
        if isinstance(value, str) and value in _VALID_GATING_STATES
    }
    return kept or None


def _bounded_str(value: object, max_len: int) -> str | None:
    """Coerce a verdict field expected to be a short string into one that
    fits its column, or drop it if it isn't a (non-empty) string at all.

    Every other field on this row degrades per-field on a bad value via an
    isinstance check ahead of the insert; a short VARCHAR column is the one
    place a value of the *right* type can still blow up the write, since an
    oversized string is a perfectly good Python str right up until Postgres
    raises DataError at commit — which takes the whole row down with it, not
    just the one field. Truncating instead of
    dropping is deliberate: a clipped recommendation is still useful for
    triage, an absent one is not. ``raw_verdict`` keeps the untruncated
    original regardless.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:max_len]


def _str_or_none(value: object) -> str | None:
    """Coerce a verdict field expected to be a string, dropping it if it
    isn't a (non-empty) string at all.

    ``company_or_project``/``rationale`` are Text columns, not bounded
    VARCHARs like ``_bounded_str`` guards, so there is no length to truncate
    to — but they are exactly as exposed to a wrong-typed value. A model that
    emits a structured (dict/list) ``rationale`` instead of prose is still a
    plain Python object of the wrong type for this column, and passing it
    straight to the ORM raises at commit — which takes the whole row down
    with it, the same failure ``_bounded_str`` exists to prevent for the
    VARCHAR columns (F9).
    """
    return value if isinstance(value, str) and value else None


def _strip_llm_preamble(text: str) -> str:
    """Remove LLM internal reasoning that leaks before the actual Slack message.

    Strategy: split into paragraphs, identify the first paragraph that looks like
    an actual Slack message (not meta-commentary), and discard everything before it.
    """
    # If there's a --- separator, take everything after the last one
    if "\n---\n" in text:
        parts = text.split("\n---\n")
        candidate = parts[-1].strip()
        if candidate:
            text = candidate

    # Split into paragraphs (separated by blank lines)
    paragraphs = re.split(r"\n\s*\n", text.strip())
    if len(paragraphs) <= 1:
        return text

    # Patterns that indicate internal reasoning / meta-commentary
    _PREAMBLE_RE = re.compile(
        r"^("
        r"(That('s| is) (not|exactly|interesting))"
        r"|Let me"
        r"|I('ll| should| need| couldn't| didn't| can't| wasn't| don't| have| want)"
        r"|Now I (have|can|know|need|should)"
        r"|These |The (search|result|profile|paper|abstract|tool|API|PubMed|query)"
        r"|My (search|query|tool|approach)"
        r"|Based on|After (review|search|look)|Since (the|I|my)"
        r"|Looking at|It seems|Ok[,.]|Okay[,.]|Hmm"
        r"|This (is|gives|shows|confirms|doesn't|isn't)"
        r"|None of|No (relevant|useful|results)"
        r"|Unfortunately"
        r")",
        re.IGNORECASE,
    )

    # Find the first non-preamble paragraph
    for i, para in enumerate(paragraphs):
        first_line = para.strip().split("\n")[0]
        if not _PREAMBLE_RE.match(first_line):
            if i > 0:
                stripped = "\n\n".join(paragraphs[i:]).strip()
                logger.info(
                    "Stripped %d preamble paragraph(s): %.120s",
                    i, " | ".join(p.strip()[:50] for p in paragraphs[:i]),
                )
                return stripped
            break

    return text


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response text."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract JSON from response: {text[:200]}")


__all__ = [
    "_ASSESSMENT_ORPHAN_TAG_RE", "_ASSESSMENT_RE", "_ASSESSMENT_UNCLOSED_RE",
    "_DIMENSION_RATIONALE_CHARS", "_HEADLINE_SOFT_LIMIT", "_HUB_BULLETS_MAX",
    "_HUB_BULLETS_MIN", "_HUB_BULLET_CHARS", "_KEY_POINTS_MAX", "_KEY_POINTS_MIN",
    "_KEY_POINT_BULLET_CHARS", "_KEY_POINT_GROUP_BULLETS", "_PITCH_CITATION_RE",
    "_PITCH_WORD_LIMIT", "_PROJECT_SOFT_LIMIT", "_SIDECAR_FENCE_RE", "_VALID_GATING_STATES",
    "_bounded_str", "_extract_assessment_json", "_extract_json", "_extract_slack_message",
    "_normalize_gating", "_reply_closes_thread", "_reply_opens_with_pause",
    "_sidecar_has_valid_json_block", "_str_or_none", "_strip_assessment_sidecar",
    "_strip_llm_preamble", "_unfence_sidecar",
]
