"""Positive-control probe for specialist-panel discrimination.

Decisive experiment prescribed by docs/audits/2026-08-24-panel-clear-rate
README section 7, never run until now. 2x2 factorial:

  factor A  opportunity quality  WEAK (mirrors the observed population)
                                 STRONG (engineered to satisfy every domain's
                                 own checklist affirmatively)
  factor B  question framing     PROD (presupposition-laden degree questions,
                                 the shape actually measured in
                                 specialist_consults)
                                 NEUTRAL (symmetric: invites either answer)

8 domains x 4 cells = 32 consults through the production path
(_execute_consult_specialist), with NO on_consult/on_consult_record callbacks,
so nothing is written to specialist_consults and no floor is credited.

Read-only against production state. Costs 32 Opus calls.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, "/app")

from src.agent.tools import _execute_consult_specialist  # noqa: E402
from src.agent.specialists import SPECIALIST_DOMAINS  # noqa: E402

DOMAINS = ["scientific", "chemistry", "clinical", "commercial",
           "legal", "technologic", "talent", "budget"]

# ---------------------------------------------------------------- contexts ---

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

CONTEXTS = {"WEAK": WEAK, "STRONG": STRONG}
FRAMINGS = {"PROD": PROD, "NEUTRAL": NEUTRAL}

_SIG = re.compile(r"signal:\s*(blocking|caution|clear)", re.I)
_CONF = re.compile(r'"confidence"\s*:\s*"(high|moderate|low)"', re.I)


async def one(domain, qkey, ckey, sem):
    async with sem:
        try:
            out = await _execute_consult_specialist(
                domain,
                FRAMINGS[qkey][domain],
                CONTEXTS[ckey],
                agent_id="blackbird",
                channel=None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"domain": domain, "framing": qkey, "quality": ckey,
                    "signal": f"ERROR:{type(exc).__name__}", "detail": str(exc)[:300],
                    "concerns": None, "confidence": None, "len": 0}
    m = _SIG.search(out or "")
    c = _CONF.search(out or "")
    n_concerns = None
    try:
        body = out.split("\n\n", 1)[1] if "\n\n" in out else out
        start = body.find("{")
        if start >= 0:
            obj = json.loads(body[start:body.rfind("}") + 1])
            n_concerns = len(obj.get("concerns") or [])
    except Exception:  # noqa: BLE001
        pass
    return {"domain": domain, "framing": qkey, "quality": ckey,
            "signal": (m.group(1).lower() if m else "UNPARSED"),
            "confidence": (c.group(1).lower() if c else None),
            "concerns": n_concerns, "len": len(out or ""), "raw": out}


async def main():
    sem = asyncio.Semaphore(4)
    jobs = [one(d, f, q, sem)
            for q in CONTEXTS for f in FRAMINGS for d in DOMAINS]
    print(f"issuing {len(jobs)} consults (concurrency 4)...", flush=True)
    res = await asyncio.gather(*jobs)

    with open("/probe/panel_probe_results.json", "w") as fh:
        json.dump(res, fh, indent=1)

    print("\n=== per-cell signal counts (8 domains per cell) ===")
    print(f"{'quality':<9}{'framing':<9}{'clear':>7}{'caution':>9}{'blocking':>10}{'other':>7}")
    for q in CONTEXTS:
        for f in FRAMINGS:
            cell = [r for r in res if r["quality"] == q and r["framing"] == f]
            cnt = {k: sum(1 for r in cell if r["signal"] == k)
                   for k in ("clear", "caution", "blocking")}
            other = len(cell) - sum(cnt.values())
            print(f"{q:<9}{f:<9}{cnt['clear']:>7}{cnt['caution']:>9}{cnt['blocking']:>10}{other:>7}")

    print("\n=== per-domain signal by cell ===")
    print(f"{'domain':<13}{'WEAK/PROD':<12}{'WEAK/NEUT':<12}{'STRONG/PROD':<14}{'STRONG/NEUT':<12}")
    for d in DOMAINS:
        row = []
        for q in ("WEAK", "STRONG"):
            for f in ("PROD", "NEUTRAL"):
                hit = [r for r in res if r["domain"] == d and r["quality"] == q
                       and r["framing"] == f]
                row.append(hit[0]["signal"] if hit else "-")
        print(f"{d:<13}{row[0]:<12}{row[1]:<12}{row[2]:<14}{row[3]:<12}")

    print("\n=== mean concerns listed, by cell ===")
    for q in CONTEXTS:
        for f in FRAMINGS:
            cell = [r["concerns"] for r in res
                    if r["quality"] == q and r["framing"] == f
                    and isinstance(r["concerns"], int)]
            if cell:
                print(f"  {q:<8}{f:<9} n={len(cell)}  mean={sum(cell)/len(cell):.2f}"
                      f"  min={min(cell)} max={max(cell)}")

    errs = [r for r in res if r["signal"].startswith("ERROR") or r["signal"] == "UNPARSED"]
    if errs:
        print(f"\n=== {len(errs)} non-signal outcomes ===")
        for r in errs:
            print(f"  {r['domain']}/{r['quality']}/{r['framing']}: {r['signal']} "
                  f"{r.get('detail','')[:160]}")
    print("\nfull results -> /probe/panel_probe_results.json")


if __name__ == "__main__":
    asyncio.run(main())
