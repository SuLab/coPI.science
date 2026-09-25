"""CLAUDE.md's PI-visibility claim vs. what the hub is actually told to say.

CLAUDE.md's Opportunity-Assessment bullet carries a claim about what a PI or
another lab can see. `5d67e92` ("feat(hub): post a headline summary to
#assessments-summary on every held verdict") widened that claim's subject from
"the `:mag:` label" to "the full verdict — rationale, red flags, gating,
`raw_verdict`" — the design-D12 field list for the *#assessments-summary*
headline, grafted onto an unrelated sentence about the interview thread. The
result was a documented invariant the code contradicts by design:
`src/agent/thread_guidance.py`'s `_SCOUT_HUB[CONCLUDE]` **mandates** that the hub
state those very fields inline in the visible reply, and so do
`prompts/roles/scout_hub/agent-system.md` and `phase4-thread-reply.md`.

Nothing pinned that sentence, and the cost was an audit finding that had to be
retracted: the measured "leak rates" of 11/13, 10/13 and 13/13 in
`docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` §1 turned out to be
*compliance* rates, and the recommended fix ("stop the model restating the
verdict") would have left every interview ending with nothing. This module is the
drift alarm: a CLAUDE.md claim about PI-visible content may not name a field the
CONCLUDE guidance requires be stated inline.

It deliberately does **not** constrain the `#assessments-summary` paragraph in
the same bullet. That paragraph's D12 field list is accurate about that channel
and makes no claim about what a PI or another lab sees — which is exactly the
distinction 5d67e92 lost.

The same false invariant once lived inside the prompt itself. Until scout_hub
1.7.1, the preamble to `phase4-thread-reply.md`'s sidecar field list banned the
whole list from the visible reply, including gating, red flags and the
recommendation. The last test below pins the correction.
"""
import re
from pathlib import Path

from src.agent import thread_guidance

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"
PHASE4 = ROOT / "prompts" / "roles" / "scout_hub" / "phase4-thread-reply.md"

# The visibility claim this module guards, verbatim. It is the phrase 5d67e92
# corrupted, and its disappearance is itself a failure below: a test that only
# forbade field names would pass just as well against a CLAUDE.md that had
# deleted the claim outright.
ANCHOR = "a PI or another lab sees"

# The fields the CONCLUDE guidance requires the hub state INLINE, in the visible
# Slack message. Written out rather than parsed out of the prose — the guidance
# is English, not a schema — but every entry is asserted to still appear in that
# guidance below, so the list cannot quietly drift away from its source of truth.
INLINE_FIELDS = ("gating", "recommendation", "red flags", "confidence")

# The sidecar-only half. CLAUDE.md is free — and correct — to say these never
# reach Slack, so they must NOT be in INLINE_FIELDS, and the guidance must never
# ask for them inline. Without this control, INLINE_FIELDS could grow to cover
# everything and the test above would forbid every true claim as well.
SIDECAR_ONLY = ("raw_verdict", "weighted_score", "band")


def _norm(text: str) -> str:
    """Collapse line-wraps so an assertion about prose survives a reflow."""
    return " ".join(text.split())


def _conclude_guidance() -> str:
    guidance, instructions = thread_guidance._SCOUT_HUB[thread_guidance.CONCLUDE]
    return _norm(f"{guidance} {instructions}")


def _visibility_clauses() -> list[str]:
    """Every clause of CLAUDE.md that claims something about what a PI or another
    lab sees.

    Split on `.` and `;` because the claim has lived on both sides of a
    semicolon: before 5d67e92 it was its own sentence, after it a clause hanging
    off the `:mag:` one. Clause granularity is what keeps the neighbouring —
    correct — `#assessments-summary` D12 sentence out of scope.
    """
    body = _norm(CLAUDE_MD.read_text(encoding="utf-8"))
    return [clause for clause in re.split(r"[.;]", body) if ANCHOR in clause]


def _assessment_bullet() -> str:
    """The reply-only/Opportunity-Assessment bullet, whole."""
    raw = CLAUDE_MD.read_text(encoding="utf-8")
    start = raw.index("- **Inside an interview thread the hub is reply-only")
    nxt = re.compile(r"^- \*\*", re.M).search(raw, start + 1)
    return _norm(raw[start : nxt.start() if nxt else len(raw)])


def test_claude_md_still_makes_a_pi_visibility_claim():
    """Control. The finding below is an absence assertion, so it would pass
    vacuously against a CLAUDE.md that had simply dropped the claim — leaving the
    confidentiality boundary undocumented rather than mis-documented."""
    clauses = _visibility_clauses()
    assert clauses, (
        f"CLAUDE.md no longer says anything about what {ANCHOR!r} — the "
        "confidentiality boundary between the visible reply and the "
        "<assessment_json> sidecar must stay documented somewhere in it"
    )


def test_the_inline_field_list_still_matches_the_guidance():
    """Control on this module's own vocabulary, in both directions.

    If the CONCLUDE guidance stops mandating one of these fields inline, this
    test fails rather than the finding silently over-constraining CLAUDE.md; and
    if the guidance ever starts naming a sidecar-only field, the split this
    module rests on has moved and both lists need re-deriving.
    """
    guidance = _conclude_guidance().lower()
    for field in INLINE_FIELDS:
        assert field in guidance, (
            f"thread_guidance._SCOUT_HUB[CONCLUDE] no longer mentions {field!r}; "
            "INLINE_FIELDS is out of date with its source of truth"
        )
    for field in SIDECAR_ONLY:
        assert field not in guidance, (
            f"thread_guidance._SCOUT_HUB[CONCLUDE] now names {field!r}, which this "
            "module treats as sidecar-only — re-derive INLINE_FIELDS/SIDECAR_ONLY"
        )


def test_claude_md_does_not_claim_the_inline_verdict_fields_are_hidden():
    """The finding (RCA §1). A clause claiming a PI or another lab cannot see X
    may not name a field the hub is *required* to state inline in the visible
    reply — that is a false invariant, and the last one sent an audit chasing a
    leak that was compliance."""
    for clause in _visibility_clauses():
        named = [field for field in INLINE_FIELDS if field in clause.lower()]
        assert not named, (
            f"CLAUDE.md claims a PI or another lab never sees {named} — but "
            "thread_guidance._SCOUT_HUB[CONCLUDE], agent-system.md and "
            "phase4-thread-reply.md all require the hub state those inline in the "
            f"visible reply. The clause is: {clause.strip()!r}"
        )


def test_claude_md_records_that_the_verdict_is_stated_inline():
    """The positive half of the correction, so it cannot be dropped either: the
    bullet has to say the verdict IS stated inline, point at the code that
    requires it, and name the class that actually is confidential in the visible
    half (the PI's unpublished disclosures, not the verdict)."""
    bullet = _assessment_bullet()
    assert "verdict inline" in bullet, (
        "CLAUDE.md's assessment bullet no longer records that the hub's concluding "
        "reply states its verdict inline — the fact 5d67e92 obscured"
    )
    assert "thread_guidance" in bullet, (
        "CLAUDE.md's assessment bullet no longer points at src/agent/thread_guidance.py, "
        "the code that mandates the inline verdict"
    )
    assert "unpublished" in bullet, (
        "CLAUDE.md's assessment bullet no longer records that the protected class in the "
        "VISIBLE half is the PI's unpublished disclosures (phase4-thread-reply.md)"
    )


def _sidecar_preamble() -> str:
    """phase4-thread-reply.md's paragraph introducing the sidecar field list, from
    its bare-JSON instruction up to item 1."""
    body = _norm(PHASE4.read_text(encoding="utf-8"))
    start = body.index("**Emit the sidecar as bare JSON")
    return body[start : body.index("1. **Gating criteria.**", start)]


def test_the_sidecar_preamble_admits_the_inline_verdict():
    """The prompt may not ban from the visible reply what CONCLUDE requires in it.

    The old preamble said "none of it may appear anywhere in `<slack_message>`"
    over a list that opens with gating, red flags and the recommendation, so on the
    CONCLUDE turn the hub was told both to state its verdict inline and never to. The
    one sentence admitting fields into the visible reply must name every inline field.
    """
    preamble = _sidecar_preamble()
    assert "none of it may appear" not in preamble.lower(), (
        "the sidecar preamble bans its whole field list from <slack_message> again"
    )
    admits = [s for s in preamble.split(". ") if "also appears in `<slack_message>`" in s]
    assert len(admits) == 1, (
        "the sidecar preamble no longer says which fields the visible reply carries"
    )
    missing = [field for field in INLINE_FIELDS if field not in admits[0].lower()]
    assert not missing, (
        f"the sidecar preamble's visible-reply sentence no longer admits {missing}, "
        "which thread_guidance._SCOUT_HUB[CONCLUDE] requires inline"
    )
