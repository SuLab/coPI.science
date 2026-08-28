"""Quality-ladder calibration harness for the specialist panel.

Replaces scripts/diagnose_specialist_calibration.py, which called
generate_agent_response directly and so never exercised the production consult
path's pinned model or max_tokens. This calls _execute_consult_specialist with
BOTH persistence callbacks None: it writes no specialist_consults rows and
credits no specialist floor.

NOT in ci.sh — it makes real Opus calls (~48 for the default grid, about 13% of
a one-hour simulation run).

Run it on any persona, rubric or model change. Acceptance criteria are in
docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md section 7.2.

Usage:
    # inside the agent container, so .env and the baked src/ are both present:
    docker compose -f docker-compose.prod.yml --profile agent run --rm --no-deps \
      -v /host/dir:/probe agent python /probe/panel_calibration_ladder.py
    # or list the grid without spending anything:
    .venv-test/bin/python scripts/panel_calibration_ladder.py --dry-run
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.specialists import (  # noqa: E402
    SPECIALIST_DOMAINS,
    VERDICT_SIGNALS,
    construct_sensitivity,
    invariance,
)
from src.agent.tools import _execute_consult_specialist  # noqa: E402

_SIGNAL = re.compile(r"signal:\s*([a-z_]+)", re.I)
_CONCURRENCY = 4

# --------------------------------------------------------------- contexts ---
# WEAK and STRONG, and the PROD/NEUTRAL framings below, are copied verbatim from
# docs/audits/2026-08-27-consult-persona-calibration/panel_probe.py — the
# archived probe that produced the RCA's published numbers. Reusing the exact
# text (not paraphrasing it) is what keeps a ladder run comparable with that
# audit's measurements.

WEAK = """\
PI: We see a strong phenotype when we knock down KIAA1443 in our patient-derived
glioma lines -- proliferation drops about 60%. It's the cleanest hit from our
shRNA screen.

Hub: Has that been reproduced outside your lab?
PI: Not yet. It's one postdoc, one cell line panel, three biological replicates.
We haven't published it -- there's a preprint we keep meaning to post.

Hub: Any compound?
PI: No small molecule yet. We think it's druggable because there's a pocket in
the homology model. Nothing has been filed -- I should probably talk to the TTO.

Hub: Controls?
PI: Two independent shRNAs, and a scrambled control. No rescue arm yet, and no
CRISPR KO -- that's on the list.

Hub: What would the work cost, and who runs it?
PI: I haven't costed it. It'd be me and the postdoc, who graduates in the
spring. I'd guess a couple of years? I don't really know what the competitive
landscape looks like -- I'd have to look.
"""

STRONG = """\
PI: The target is NSMCE3. We published the mechanism in Nature Cancer (2024) and
two independent groups have since reproduced the core result -- the Vogel lab in
Utrecht and a contract lab we paid to run it blind. Effect size in the blinded
replication was within 10% of ours.

Hub: How is causality established?
PI: Four orthogonal arms, all in the paper: CRISPR KO, KO plus wild-type rescue,
a catalytically-dead rescue that does NOT rescue, and the tool compound.
Pre-registered analysis plan on OSF, n=12 per arm powered at 0.9 for the effect
we saw, vehicle and scrambled arms in every experiment. It reads out either way:
if the compound arm had failed to phenocopy the KO we would have dropped it.

Hub: Human relevance?
PI: Confirmed in primary human tumour explants from 14 patients, and the mouse
and human dose-response curves overlap within 2-fold. We know where mouse and
human diverge on this pathway -- it's the paralogue, and we measured it.

Hub: Chemistry?
PI: BLK-2117 is our lead. 4 nM enzymatic, 40 nM cellular, 210-fold selective
against the nearest three family members on a 48-enzyme panel run at Eurofins.
hERG > 30 microM, no CYP inhibition below 25 microM, negative in Ames and in the
glutathione-trapping assay -- no reactive metabolite signal, no PAINS motif. Four
step route, already run at 200 g by a CRO, 71% overall yield. Oral bioavailability
62% in rat and 55% in dog, 14-day tolerability at 100 mg/kg with no adverse
findings -- therapeutic index about 30 against the efficacious dose.

Hub: Clinical framing?
PI: Indication is IDH-wildtype recurrent glioblastoma. Standard of care after
recurrence is lomustine with median OS about 7-9 months, so the bar is low and
well documented. US incidence of the recurrent population is roughly 12,000 a
year. Regulatory path is a precedented one -- overall survival against lomustine
control, the same endpoint three prior programmes used, and we have written
support from two neuro-oncology KOLs.

Hub: Competition and IP?
PI: Named competing programmes: Servier's IDH franchise (different mechanism, not
overlapping), Chimerix ONC201 (dopamine receptor, published phase 2), and one
preclinical Astellas programme disclosed at AACR 2025. None of them touches
NSMCE3. Composition-of-matter patent ISSUED (US 12,043,611, JHU sole assignee,
verified single assignment chain, no consortium co-ownership). Outside counsel at
Cooley delivered a written freedom-to-operate opinion in March -- no blocking
art. It's a research-tool-free package: nothing in the chain is encumbered by a
material transfer agreement.

Hub: Platform claim?
PI: The screening method behind it has now produced validated leads on three
unrelated target classes, and the Utrecht group ran our protocol end-to-end from
the published methods without our help. What transfers is the protocol, the
reporter line, and the analysis code -- all deposited. If it fails on a fourth
class, that tells us the reporter chemistry is target-specific, which is exactly
what we'd want to know.

Hub: Team?
PI: I've delivered two DoD awards on time and on budget in the last five years.
Named co-investigator is Dr. Amara Osei (staff scientist, 8 years, runs the
in vivo work and could carry the project if I were hit by a bus -- she's named in
the succession plan). Medicinal chemistry is a dedicated FTE at the JHDD. I have
60% protected research time; my other commitments are one R01 in year 4 and no
company roles. No consulting agreements, no equity, no advisory seats -- full
disclosure is on file with the JHU COI office and there is nothing to declare in
this area.

Hub: Budget?
PI: 650,000 dollars over 18 months, line-itemed: 180K medicinal chemistry FTE,
150K GLP-adjacent tox, 140K the 40-animal efficacy study, 90K bioanalysis, 60K
the biomarker assay, 30K contingency. Milestone-gated at month 9 on the tox
readout -- if it fails we stop and you keep the rest. Follow-on is a 4M dollar
seed we've already had a term sheet conversation about with two funds, and the
JHDD covers the team for the six months a raise takes.
"""

# MEDIUM is copied verbatim from a SEPARATE archived file
# (docs/audits/2026-08-27-consult-persona-calibration/panel_probe_medium_tier.py)
# rather than imported from it — that directory's name is not a valid Python
# package path (it contains hyphens) — but it is the same text, recovered from
# the 2026-08-28 RCA run that produced the population-faithful measurement
# (87.5% caution / 12.5% blocking under the production framing). panel_probe.py
# never had a MEDIUM tier; this is where it lives.
MEDIUM = """\
PI: The target is NSMCE3. We published the mechanism in J Biol Chem last year --
one lab, our own hands. Nobody outside has repeated it yet, though a group in
Utrecht has asked for the reagents.

Hub: How is causality established?
PI: CRISPR KO and two independent siRNAs, with a scrambled arm and vehicle
controls. We have a wild-type rescue that works. We do not have a
catalytically-dead rescue -- that construct is being made now. n=6 per arm,
which was what the budget allowed; the analysis was not pre-registered.

Hub: Human relevance?
PI: Two primary human explants, both responded. Mouse and human curves are
within about 5-fold. We know the paralogue is a possible confounder and have not
measured it.

Hub: Chemistry?
PI: We have a tool compound from a commercial library, about 800 nM cellular,
maybe 12-fold selective against the two family members we tested. No PK yet, no
tox. It has a phenol we are not thrilled about. Nothing has been optimised -- a
real lead would need a medchem campaign we cannot run in-house.

Hub: Clinical framing?
PI: Recurrent glioblastoma. Standard of care is lomustine, outcomes are poor.
I would have to look up the exact incidence numbers.

Hub: Competition and IP?
PI: A provisional was filed in January on the target-pathway link -- method of
use, not composition. The TTO has not decided on national phase. I am aware of
the Chimerix programme; I have not done a systematic landscape. No FTO opinion.

Hub: Platform claim?
PI: The screen that found it could probably generalise -- we have run it on one
other target class and got something interesting, unpublished.

Hub: Team?
PI: Me, one postdoc who leaves in eighteen months, one rotating student. No staff
scientist. I have two other R01s running. No company roles or consulting.

Hub: Budget?
PI: I have not costed it properly. Somewhere in the low hundreds of thousands
over two years, I would guess. If it works the next step would be a bigger
programme but I have not thought about who funds that.
"""

# --------------------------------------------------------------- questions ---
# PROD framing reproduces the measured shape in specialist_consults: a
# presupposition-laden degree question naming a defect ("how disqualifying is",
# "or is X a risk that cannot be managed"). NEUTRAL framing is symmetric: it
# names the same subject matter but invites either answer explicitly.

PROD = {
    "scientific": "How disqualifying is it that the causal chain rests on the arms described, given there is no independent confirmation that the effect is attributable to the nominal target rather than to an off-pathway consequence of the manipulation?",
    "chemistry": "Is the chemical matter here a controllable path to a development candidate, or is the tractability risk one that medicinal chemistry cannot manage from where this sits?",
    "clinical": "Is preclinical ranking in this indication a real decision point for developers, or does clinical practice already run on something that makes this irrelevant?",
    "commercial": "Is there any venture-scale or even licensable shape here, given how crowded and how commoditised this space already is?",
    "legal": "How ownable is this in fact, given the encumbrance and co-ownership exposure that academic filings of this kind routinely carry?",
    "technologic": "How much of the platform claim is actually demonstrated versus asserted from a single favourable example, and how damaging is the gap?",
    "talent": "Can this lab actually execute the package on top of its existing commitments, or is the bandwidth and succession exposure disqualifying?",
    "budget": "How badly does the proposed scope overrun the vehicle being discussed, and how disqualifying is the follow-on gap?",
}

NEUTRAL = {
    "scientific": "Given the record below, is the experimental basis adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "chemistry": "Given the record below, is the path to a development candidate adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "clinical": "Given the record below, is the clinical framing -- unmet need, indication, endpoint, patient numbers -- adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "commercial": "Given the record below, is the commercial case adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "legal": "Given the record below, is the IP and freedom-to-operate position adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "technologic": "Given the record below, is the platform claim adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "talent": "Given the record below, is the team's execution capacity adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
    "budget": "Given the record below, is the scope and cost adequate for an incubation-stage go/no-go decision, or does something in your domain stand in the way? Answer either way, and say which it is.",
}

# Insertion order is load-bearing: construct_sensitivity compares ADJACENT
# tiers, and tests/unit/test_calibration_ladder_fixtures.py pins this exact
# order.
CONTEXTS = {"WEAK": WEAK, "MEDIUM": MEDIUM, "STRONG": STRONG}
FRAMINGS = {"PROD": PROD, "NEUTRAL": NEUTRAL}

# Preserved from the deleted throwaway diagnostic
# (scripts/diagnose_specialist_calibration.py, commit 84fa1aa) so that script's
# 2026-08-18 STRONG-case diagnosis stays reproducible. Deliberately NOT part of
# CONTEXTS/build_cells's grid: only 3 of the 8 specialist domains have a case
# here, so folding it into the default grid would silently exempt the other 5
# domains from "every domain exercised" — exactly the failure mode
# test_every_specialist_domain_is_exercised exists to catch.
LEGACY_STRONG_CASES: dict[str, tuple[str, str]] = {
    "scientific": (
        "Does the evidence support the mechanism claim?",
        "We ran vehicle and scrambled-siRNA arms in every cohort, n=24/arm "
        "powered at 80% for the 40% effect we pre-registered on OSF before "
        "unblinding. Two independent labs reproduced the rescue. The readout "
        "is decision-enabling either way: if the knockdown does not rescue, "
        "the target is wrong and we stop.",
    ),
    "chemistry": (
        "Is there a credible path to a development candidate?",
        "We have a 40-compound lead series off a validated crystal structure, "
        "best compound 12 nM with 400x selectivity over the two nearest family "
        "members, clean hERG at 30 uM, no structural alerts, and oral "
        "bioavailability of 55% in rat. Med-chem is tractable: three vectors "
        "on the scaffold are open.",
    ),
    "commercial": (
        "Is this differentiated against the current landscape?",
        "No approved agent and no competitor in registrational trials for this "
        "indication. The two prior programs (Tango, CUE-401) were discontinued "
        "for a liability our chemotype does not share. Two comparable deals "
        "closed above $200M upfront in the last 18 months.",
    ),
}


def build_cells() -> list[tuple[str, str, str]]:
    """Every (tier, framing, domain) triple. Ordered tier-major so a partial
    run still covers whole tiers, which is what the acceptance criteria are
    stated over."""
    return [
        (tier, framing, domain)
        for tier in CONTEXTS
        for framing in FRAMINGS
        for domain in SPECIALIST_DOMAINS
    ]


async def _one(tier: str, framing: str, domain: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            out = await _execute_consult_specialist(
                domain, FRAMINGS[framing][domain], CONTEXTS[tier],
                agent_id="blackbird", channel=None,
                # Both None is the whole point: no row, no floor credit.
                on_consult=None, on_consult_record=None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"tier": tier, "framing": framing, "domain": domain,
                    "signal": f"ERROR:{type(exc).__name__}", "detail": str(exc)[:300]}
    m = _SIGNAL.search(out or "")
    return {"tier": tier, "framing": framing, "domain": domain,
            "signal": m.group(1).lower() if m else "UNPARSED", "raw": out}


def _is_real_signal(signal: str) -> bool:
    """Whether ``signal`` is an actual verdict signal rather than an
    ``ERROR:``/``UNPARSED`` sentinel stored by ``_one``.

    R (``construct_sensitivity``) and S (``invariance``) both work by comparing
    signal STRINGS for equality across a pair of observations. A sentinel is
    never equal to a real signal (and, for ``ERROR:{exc}``, never equal to
    another sentinel either, since the exception type varies), so feeding one
    into either metric is not a neutral no-op: against any real signal it
    always reads as "moved" (inflating R) and never as "held" (deflating S). A
    run that happened to drop a handful of calls to a flaky API would then
    look MORE sensitive and LESS invariant than a clean run of the very same
    panel — backwards, and dangerous given a later gate in this plan compares
    a measured R against a floor. So callers must exclude sentinels from the
    R/S observation sets, while still surfacing them in the per-cell table and
    the raw results JSON — an operator needs to see that calls failed.

    Deliberately checks membership in ``VERDICT_SIGNALS`` rather than a
    hardcoded ``{"blocking", "caution", "clear"}`` literal: the verdict
    vocabulary is scheduled to change later in this plan, and a hardcoded list
    here would silently stop recognising the new words as real signals the
    day the rename ships, quietly corrupting every ladder run after it.
    """
    return signal in VERDICT_SIGNALS


def report(results: list[dict]) -> None:
    """Per-cell counts, then R and S. Both metrics always, never one: a
    constant judge scores perfectly on invariance alone.

    ERROR/UNPARSED cells are kept in the per-cell table below (that visibility
    is the point — an operator needs to see that calls failed) but excluded
    from the R/S observation sets built further down, via ``_is_real_signal``.
    """
    labels = sorted({r["signal"] for r in results})
    print(f"\n{'tier':<10}{'framing':<10}" + "".join(f"{s:>12}" for s in labels))
    for tier in CONTEXTS:
        for framing in FRAMINGS:
            cell = [r for r in results if r["tier"] == tier and r["framing"] == framing]
            counts = "".join(
                f"{sum(1 for r in cell if r['signal'] == s):>12}" for s in labels
            )
            print(f"{tier:<10}{framing:<10}{counts}")

    real = [r for r in results if _is_real_signal(r["signal"])]
    excluded = len(results) - len(real)
    if excluded:
        print(
            f"\n{excluded} of {len(results)} cells excluded from the R/S computation "
            "below (ERROR/UNPARSED sentinels are not real verdict signals — see "
            "_is_real_signal)."
        )

    tiers = list(CONTEXTS)
    for a, b in zip(tiers, tiers[1:]):
        for framing in FRAMINGS:
            obs = {
                (r["tier"], r["domain"]): r["signal"]
                for r in real
                if r["tier"] in (a, b) and r["framing"] == framing
            }
            moved, total = construct_sensitivity(obs)
            rate = f"{moved / total:.3f}" if total else "n/a"
            print(f"  R {a}->{b} [{framing}]: {moved}/{total} = {rate}")
    for tier in CONTEXTS:
        obs = {
            (r["framing"], r["domain"]): r["signal"]
            for r in real if r["tier"] == tier
        }
        held, total = invariance(obs)
        rate = f"{held / total:.3f}" if total else "n/a"
        print(f"  S {tier}: {held}/{total} = {rate}")

    print("\nper-domain signal by (tier, framing):")
    for domain in SPECIALIST_DOMAINS:
        row = [
            next((r["signal"] for r in results
                  if r["domain"] == domain and r["tier"] == t and r["framing"] == f),
                 "-")
            for t in CONTEXTS for f in FRAMINGS
        ]
        print(f"  {domain:<13}" + "".join(f"{s:<12}" for s in row))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list the grid and exit without any API call")
    ap.add_argument("--out", default="panel_calibration_results.json")
    args = ap.parse_args()

    cells = build_cells()
    if args.dry_run:
        for tier, framing, domain in cells:
            print(f"{tier:<10}{framing:<10}{domain}")
        print(f"\n{len(cells)} cells; {len(cells)} Opus calls if run for real.")
        return

    sem = asyncio.Semaphore(_CONCURRENCY)
    print(f"issuing {len(cells)} consults (concurrency {_CONCURRENCY})...", flush=True)
    results = list(await asyncio.gather(*(_one(*c, sem) for c in cells)))
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    report(results)
    print(f"\nfull results -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
