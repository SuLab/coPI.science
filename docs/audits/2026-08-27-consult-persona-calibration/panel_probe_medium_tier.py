"""MEDIUM quality tier for the specialist panel calibration ladder.

Recovered verbatim from the 2026-08-28 RCA run that produced the
population-faithful measurement: 87.5% caution / 12.5% blocking under the
production framing, whose Wilson intervals overlap production's 85.7% / 13.5%.

Archived because the original lived in a patched throwaway script while the
WEAK and STRONG tiers were archived in panel_probe.py. The maintained harness
imports this text rather than restating it, so future ladder runs stay
comparable with the audit's published numbers.
"""

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
