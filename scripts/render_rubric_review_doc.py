"""Render the screening rubric as a human-reviewable document.

Produces a reviewer-friendly markdown copy of prompts/rubric/blackbird-rubric.toml
for circulation to external reviewers (typically converted to .docx with pandoc).
Every block is labelled with its address in the TOML so reviewed edits can be
transposed back mechanically, and structural items (keys, weights, thresholds —
anything code or the database depends on) are tagged so a reviewer knows which
edits are prose and which are engineering changes.

The document is DERIVED, never edited: it is stamped with the rubric version and
content hash it was generated from, and a new review round starts by regenerating
it — that is what prevents the stale-export trap the 2026-08-21 review fell into
(hand-edited Word exports of an already-superseded checkout).

Usage (from the repo root, on the host):
    .venv-test/bin/python scripts/render_rubric_review_doc.py            # writes docs/rubric-review/
    .venv-test/bin/python scripts/render_rubric_review_doc.py --stdout   # prints instead
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.services.blackbird_rubric import (  # noqa: E402
    RUBRIC_PATH,
    Rubric,
    _format_threshold,
    load_rubric,
)

OUT_DIR = REPO_ROOT / "docs" / "rubric-review"


def render_review_markdown(rubric: Rubric, generated: str, changelog: str) -> str:
    r = rubric
    cond_upper = _format_threshold(r.advance_min - 0.1)
    fmt = _format_threshold

    lines: list[str] = [
        "# Blackbird screening rubric — review copy",
        "",
        f"**Rubric version {r.version} · content hash `{r.content_hash}` · "
        f"generated {generated} from `prompts/rubric/blackbird-rubric.toml` "
        f"(document date {r.date})**",
        "",
        "This is a faithful, reviewer-oriented rendering of the rubric Blackbird's",
        "scouting hub applies to every PI interview. The same document drives both the",
        "prompt the hub reads and the arithmetic that scores its verdicts, so the two",
        "cannot drift apart. **Internal** — the hub is instructed never to share the",
        "rubric verbatim or reveal the weightings, so circulate this copy accordingly.",
        "",
        "## How to review this document",
        "",
        "- **Prose is freely editable.** Reword, tighten, or challenge any of it —",
        "  tracked changes or comments both work.",
        "- **Items tagged `STRUCTURAL` are wired into code and the database.** They can",
        "  be changed, but the change is an engineering change as well as an editorial",
        "  one, so flag it explicitly rather than rewording in place: the six",
        "  dimension keys, the three gating keys, the band names",
        "  advance / conditional / pass, the 1–5 score scale, the integer weights",
        "  (they must sum to exactly 100), and the two band thresholds.",
        "- **Every block is labelled with its address in the source document** (for",
        "  example `[gating.credible_science].description`). Reviewed edits are",
        "  transposed back to that address, the version is bumped, and the automated",
        "  gate re-runs — see Appendix B.",
        "- **Out of scope here:** the machine-readable sidecar contract (the JSON the",
        "  hub files with each verdict) and the interview-phase instructions. Those",
        "  live in the hub's phase-4 prompt; comments are welcome but belong to a",
        "  separate review.",
        "",
        "## 0. Scope and application order — `[intro]`",
        "",
        r.intro,
        "",
        '## 1. Gating criteria (pass/fail — a "no" blocks or heavily discounts)',
        "",
        "Three criteria. Each is recorded on every stored verdict under its key",
        "(`STRUCTURAL`) as `met` / `not_met` / `unconfirmed` — string values, and",
        "`unconfirmed` is an honest, non-blocking answer.",
        "",
    ]
    for i, (key, gate) in enumerate(r.gating.items(), start=1):
        lines += [
            f"### 1.{i} {gate['title']} — key `{key}` (`STRUCTURAL`)",
            "",
            f"`[gating.{key}].description`",
            "",
            f"{gate['description']}",
            "",
        ]
    lines += [
        "## 2. Weighted scoring dimensions",
        "",
        f"Score each dimension {r.scale_min}–{r.scale_max} ({r.scale_max} = strongly",
        "meets the bar). The scale is `STRUCTURAL` (`[scale]`). One scale scores every",
        "verdict.",
        "",
        "### 2.1 Preamble — `[scoring].preamble`",
        "",
        "> Note for reviewers: the 35% / 65% split quoted below is derived from the",
        "> weight column in §2.2 — a drift test recomputes it. If you change weights,",
        "> this prose has to be re-derived with them.",
        "",
        r.scoring_preamble,
        "",
        "### 2.2 Weights at a glance",
        "",
        "The keys and the weights are `STRUCTURAL`; the weights must sum to exactly",
        "100.",
        "",
        "| # | Dimension | Key (`STRUCTURAL`) | Weight | Owning specialist |",
        "|---|---|---|---|---|",
    ]
    for i, dim in enumerate(r.dimensions, start=1):
        lines.append(
            f"| {i} | {dim.title} | `{dim.key}` | {dim.weight}% | "
            f"{dim.specialist or '—'} |"
        )
    lines += [
        "",
        "### 2.3 Anchors and evidence — what a strong score means",
        "",
    ]
    for i, dim in enumerate(r.dimensions, start=1):
        lines += [
            f"#### 2.3.{i} {dim.title} — key `{dim.key}`",
            "",
            f"**Anchor** (`anchors`, weight {dim.weight}%): {dim.anchors}",
            "",
        ]
        if dim.evidence:
            lines += [
                "**Evidence to look for** (`evidence` — ask whether evidence exists, "
                "internal and/or public, for each):",
                "",
            ]
            lines += [f"- {item}" for item in dim.evidence]
            lines.append("")
    lines += [
        "### 2.4 Banding — `[banding]`",
        "",
        "Thresholds and band names are `STRUCTURAL`; the computed band is stored with",
        "every verdict and shown to staff beside the hub's own recommendation.",
        "",
        f"- **Bands:** ≥{fmt(r.advance_min)} → advance/recommend; "
        f"{fmt(r.conditional_min)}–{cond_upper} → conditional "
        f"({r.banding_conditional_note}); <{fmt(r.conditional_min)} → pass.",
        f"- **What each band commits someone to** "
        f"(`[banding].semantics`): {r.banding_semantics}.",
        "- **Vocabulary:** the stored band value \"pass\" means pass ON the deal "
        f"(decline) — displayed as `{r.pass_label}`.",
        "",
        "**How the band is used** (`[banding].advisory_note`):",
        "",
        r.banding_advisory_note,
        "",
        "## 3. Red flags (disqualifier-grade only) — `[red_flags]`",
        "",
        r.red_flags_intro,
        "",
    ]
    lines += [f"- {item}" for item in r.red_flags]
    lines += [
        "",
        "## 4. Structured recommendation — `[recommendation]`",
        "",
        r.recommendation,
        "",
        "## 5. One-line decision heuristic — `[heuristic]`",
        "",
        r.heuristic,
        "",
        "## 6. Stage bars — `[stage_bar_global]` and `[stage_bar.*]`",
        "",
        "One bar per evaluation-panel specialist, rendered into that "
        "specialist's persona at consult time. Each is a CONDENSATION of the "
        "clause named in `source` — nothing here is new policy, and `source` is "
        "printed so this section can be checked against the sections above it.",
        "",
        "**The global bar, which every one of the eight specialists reads "
        "FIRST**, above its own domain bar — `[stage_bar_global]` (source: "
        f"`{r.stage_bar_global.source}`):",
        "",
        f"> {r.stage_bar_global.text}",
        "",
        "Then the domain bar:",
        "",
        "| Domain | Source clause(s) | The bar as the specialist reads it |",
        "|---|---|---|",
    ]
    lines += [
        f"| {bar.domain} | `{bar.source}` | {bar.text} |"
        for bar in r.stage_bars.values()
    ]
    lines += [
        "",
        "## Appendix A — change history (`[meta].changelog`)",
        "",
        "```",
        changelog,
        "```",
        "",
        "## Appendix B — how reviewed edits land",
        "",
        "1. Each accepted edit is transposed into `prompts/rubric/blackbird-rubric.toml`",
        "   at the address the section carries; `STRUCTURAL` changes also get their",
        "   code/database counterpart.",
        "2. `[meta].version` is bumped and the change recorded in `[meta].changelog`.",
        "3. `./scripts/ci.sh` runs the import-time validator (6 dimensions, unique",
        "   keys, weights summing to 100, threshold sanity, non-empty prose)",
        "   and the pins in `tests/unit/test_rubric_document.py`.",
        "4. The web tier and the agent run are restarted; the startup banner must print",
        "   the new version and content hash.",
        "5. New assessments are stamped with that version + hash, so verdicts written",
        "   before and after the review stay comparable — nothing historical is",
        "   rewritten.",
        "",
        f"*Generated by `scripts/render_rubric_review_doc.py` from "
        f"`{RUBRIC_PATH.name}` — do not edit the source rubric and this copy "
        f"independently; regenerate instead.*",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    rubric = load_rubric()
    generated = date.today().isoformat()
    text = render_review_markdown(rubric, generated, _changelog_text())
    if "--stdout" in argv:
        sys.stdout.write(text)
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"blackbird-rubric-v{rubric.version}-{rubric.content_hash}-review.md"
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


def _changelog_text() -> str:
    import tomllib

    with RUBRIC_PATH.open("rb") as f:
        return str(tomllib.load(f)["meta"]["changelog"]).strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
