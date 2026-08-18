"""Is `clear` reachable for the specialist panel, or does it only ever caution?

Production run 1787010946 returned caution/blocking on 142/142 consults with
ZERO parse failures — so this is genuine model output, not a parsing artifact.
If `clear` had a 10% base rate, P(0 in 142) is about 3e-7.

Two readings fit that evidence and they call for opposite responses:
  (a) the eight personas are miscalibrated and cannot say `clear`; fix them.
  (b) the 18 assessed ideas really were all weak; change nothing.

This script separates them by asking the SAME personas about ideas built to be
clean in the relevant domain. Throwaway: run it, record the finding, delete it
or leave it — it is not imported by anything.

Usage:  .venv-test/bin/python scripts/diagnose_specialist_calibration.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.specialists import parse_opinion, persona_path  # noqa: E402
from src.services.llm import generate_agent_response  # noqa: E402

# Deliberately clean in the named domain. If `clear` is reachable at all, these
# are where it should appear: each one pre-empts the specific objection its
# persona is built to raise.
STRONG_CASES = [
    (
        "scientific",
        "Does the evidence support the mechanism claim?",
        "We ran vehicle and scrambled-siRNA arms in every cohort, n=24/arm "
        "powered at 80% for the 40% effect we pre-registered on OSF before "
        "unblinding. Two independent labs reproduced the rescue. The readout "
        "is decision-enabling either way: if the knockdown does not rescue, "
        "the target is wrong and we stop.",
    ),
    (
        "chemistry",
        "Is there a credible path to a development candidate?",
        "We have a 40-compound lead series off a validated crystal structure, "
        "best compound 12 nM with 400x selectivity over the two nearest family "
        "members, clean hERG at 30 uM, no structural alerts, and oral "
        "bioavailability of 55% in rat. Med-chem is tractable: three vectors "
        "on the scaffold are open.",
    ),
    (
        "commercial",
        "Is this differentiated against the current landscape?",
        "No approved agent and no competitor in registrational trials for this "
        "indication. The two prior programs (Tango, CUE-401) were discontinued "
        "for a liability our chemotype does not share. Two comparable deals "
        "closed above $200M upfront in the last 18 months.",
    ),
]


async def ask(domain: str, question: str, context: str) -> str:
    persona = persona_path(domain).read_text(encoding="utf-8")
    raw = await generate_agent_response(
        system_prompt=persona,
        messages=[{
            "role": "user",
            "content": f"## Question from the hub\n\n{question}\n\n"
                       f"## What the PI has said\n\n{context}",
        }],
        max_tokens=900,
        log_meta={"agent_id": "diagnostic", "phase": f"consult_{domain}"},
    )
    return raw


async def main() -> None:
    print("=== STRONG cases: `clear` SHOULD be reachable here ===")
    signals = []
    for domain, question, context in STRONG_CASES:
        raw = await ask(domain, question, context)
        opinion = parse_opinion(raw, domain=domain)
        signals.append(opinion.verdict_signal)
        print(f"  {domain:<12} -> {opinion.verdict_signal:<9} ({opinion.confidence})")
        for concern in opinion.concerns[:2]:
            print(f"       concern: {concern[:100]}")

    print()
    if "clear" in signals:
        print("VERDICT: `clear` IS reachable. The personas are calibrated and the")
        print("         18 production assessments really were weak ideas.")
        print("         -> Record this in the spec. Change NO persona files.")
    else:
        print("VERDICT: `clear` is NOT reachable even for deliberately clean cases.")
        print("         This is a prompt defect, not a property of the ideas.")
        print("         -> Proceed to Step 4 and fix the personas.")


if __name__ == "__main__":
    asyncio.run(main())
