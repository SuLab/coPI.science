"""Task T4 — the profile pipeline, end to end and live.

`live_api` **and** `real_llm`. This is the first test in the system where ORCID, PubMed
and Anthropic run together against a real database. It is the actual production path for
onboarding a PI: `worker.execute_generate_profile` calls exactly this function.

Everything else that touches `run_profile_pipeline` — the four golden masters in
`tests/characterization/test_profile_pipeline_gm.py` — replaces every external boundary
with a fake. Those tests prove the pipeline wires its own parts together. They cannot
prove that the parts still fit the world: the fakes return the shapes their author
believed ORCID, NCBI and Claude produce (Rule L1). T4.5 reconciles the two.

**The assertion that matters most** is in T4.1: the generated summary must mention
something *from the fetched works*. Without it, a pipeline that ignored its entire input
and hallucinated a plausible profile from the researcher's name would pass every other
assertion in this file. The expected vocabulary is derived from a live ORCID fetch the
test performs itself, never hardcoded.

That assertion needed two attempts, and the first one was wrong. Matching the corpus
vocabulary against a hand-written decoy only proves the matcher *can* say no. Measured on
2026-07-30: given nothing but "Lisa Racki, Scripps Research Institute, Integrative
Structural and Computational Biology" and an empty publication list, Claude Opus returns
a confident, `_validate_profile`-passing profile that already contains three of the seven
derived corpus terms (chromatin, histone, remodeling), from what it remembers about her.
So the real control is empirical and runs inside T4.1: the same model is asked for a
name-only profile on the same day, and the run must produce corpus vocabulary the
name-only run did NOT. Only that difference is attributable to the pipeline having read
its inputs. Measured the same day, the real run covered all 7 terms and the difference
was {polyphosphate, granule, pseudomonas, aeruginosa} — the researcher's independent
programme, which the model does not recall but the fetched abstracts supply.

Rule L3: each assertion message names which of provider-down / rate-limited /
schema-changed / our-code-broken it observed.

Cost: 8 real Anthropic calls for the whole file — 3 for T4.1 (public synthesis, private
seed, and the name-only control), 3 for T4.2, 0 for T4.3, 2 for T4.4, 0 for T4.5. T4.5
makes two further Anthropic *requests* that spend no tokens: they are deliberately
unauthenticated, which is how the GM #2 failure path is reproduced against the real API
rather than against a fake that raises RuntimeError.

Run it with:

    docker compose exec -T -e LIVE_API_TESTS=1 -e ANTHROPIC_API_KEY=sk-ant-... \\
      -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_b2 \\
      app python -m pytest tests/integration/test_profile_pipeline_live.py -q \\
      -m 'live_api and real_llm'
"""

import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import httpx
import pytest
import respx
from sqlalchemy import select

from src.models import ProfileRevision, Publication, ResearcherProfile
from src.services import orcid as orcid_service
from src.services import profile_pipeline, pubmed
from tests import factories

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_api,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="no ANTHROPIC_API_KEY — real-API tests are opt-in and cost money",
    ),
]


# --------------------------------------------------------------------------- the record
#
# Lisa Racki — Scripps Research, Integrative Structural and Computational Biology.
#
# Why not Josiah Carberry (0000-0002-1825-0097), the record T1 uses: that persona has no
# works at all, so `pubs_for_synthesis` is empty and the synthesis path — the entire
# point of T4 — never runs. Carberry can only prove the pipeline survives an empty
# corpus, which is what T4.4 covers by other means.
#
# Why this record is a defensible choice (Rule L2 — permanence, and nothing pinned):
#
#   * It is already a production dependency of this repository. It is listed in
#     `orcids.txt` under "Pilot lab ORCIDs — Scripps Research" (verified 2026-03-21), so
#     if it were ever deleted or made private the product would break long before this
#     test noticed, and the failure would be the *correct* signal rather than noise.
#   * It is small and slow-growing — 12 work entries spanning 2002-2025 as of
#     2026-07-30, roughly one paper a year. Small matters twice over: it bounds the token
#     spend, and it bounds the number of unthrottled NCBI requests `run_profile_pipeline`
#     fires (see `_ncbi_get`, which paces itself at ~8 req/s against a 3 req/s anonymous
#     policy limit — a large corpus would be the thing that gets this IP blocked).
#   * It exercises BOTH ORCID→PubMed resolution paths: as of 2026-07-30, 7 works carry a
#     PMID directly and 5 are DOI-only, so `convert_dois_to_pmids` (ID converter, then
#     the per-DOI ESearch fallback) really runs. One of the DOI-only entries is a bioRxiv
#     preprint that resolves to nothing, which exercises the unresolved branch too.
#   * The research has two clearly separated phases — early chromatin-remodelling work,
#     then an independent programme on bacterial polyphosphate granules — and measurement
#     showed the model recalls the first from pretraining but not the second. That
#     separation is what gives T4.1's grounding check something to detect; a subject whose
#     entire corpus the model already knows by heart would make it unfalsifiable.
#   * Nothing below is pinned to its contents. Every expected value is derived at run
#     time from whatever the live record returns.
RACKI = "0000-0003-2209-7301"


# --------------------------------------------------------------- grounding vocabulary
#
# Words a wholly invented life-sciences profile would plausibly contain anyway. If a
# corpus term is in here it cannot serve as evidence that the pipeline read its inputs,
# so it is excluded from the derived vocabulary before the check runs.
_GENERIC = {
    "analysis", "approach", "approaches", "bacteria", "bacterial", "between",
    "biology", "biological", "cellular", "cells", "complex", "complexes",
    "computational", "disease", "diseases", "dynamics", "expression", "function",
    "functional", "genome", "genomic", "involved", "mechanism", "mechanisms",
    "molecular", "process", "processes", "protein", "proteins", "regulates",
    "regulation", "research", "researcher", "science", "structural", "structure",
    "structures", "studies", "study", "system", "systems", "therapeutic",
}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _stem(word: str) -> str:
    """Crude singularisation so 'condensates' and 'condensate' count as one term.

    Deliberately not a real stemmer: a real one would collapse distinct technical terms
    and weaken the check. Only a trailing 's' comes off.
    """
    w = word.lower()
    return w[:-1] if len(w) > 5 and w.endswith("s") and not w.endswith("ss") else w


def distinctive_corpus_terms(titles, *, min_docs=2, min_len=7):
    """Terms that recur across DISTINCT work titles and are not generic vocabulary.

    Returns ``{term: document_frequency}``. Document frequency, not raw count: a term
    repeated inside one title is one paper's worth of evidence, and ORCID routinely
    lists the same paper twice (preprint plus version of record), so titles are
    de-duplicated first.
    """
    seen: set[str] = set()
    docs: list[set[str]] = []
    for t in titles:
        key = re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        docs.append({_stem(w) for w in _TOKEN.findall(key)})

    freq: dict[str, int] = {}
    for bag in docs:
        for term in bag:
            if len(term) >= min_len and term not in _GENERIC:
                freq[term] = freq.get(term, 0) + 1
    return {t: n for t, n in freq.items() if n >= min_docs}


def mentioned(text: str, terms) -> list[str]:
    """Which of ``terms`` occur in ``text`` (substring, case-insensitive on the stem)."""
    low = (text or "").lower()
    return sorted(t for t in terms if t in low)


# A plausible, entirely invented profile for a structural/computational biology lab at
# the same institution. The grounding matcher must find NOTHING in it. If it does, the
# derived vocabulary is too generic and T4.1's control has no teeth — which the test
# says out loud rather than reporting a pass.
_HALLUCINATION_DECOY = (
    "The laboratory investigates the molecular architecture of macromolecular machines "
    "using cryo-electron microscopy, single-particle reconstruction and integrative "
    "modelling. Recent work has characterised conformational landscapes of membrane "
    "transporters and established a computational pipeline for interpreting "
    "heterogeneous structural ensembles. The group combines biophysical measurements "
    "with molecular dynamics simulation to connect structure to cellular function, and "
    "is now extending these approaches to disease-associated variants in human tissue."
)


# ------------------------------------------------------------------------- test plumbing


class PipelineProbe:
    """Observes three seams of `run_profile_pipeline` without replacing any of them.

    Every wrapper delegates to the real function, so the pipeline under test is the real
    pipeline making real calls; only the arguments and the call count are recorded. This
    is how the LLM-call count (which GM #1 and GM #4 pin) and the synthesis context
    (T4.3, T4.4) are observed from the outside.

    ``private_calls`` is retained (always 0) rather than removed: the
    private-instructions removal cycle deleted step 9b
    (``synthesize_private_profile``) outright, so this probe can no longer
    instrument it, but ``llm_calls`` and every call site below still add
    ``public_calls + private_calls`` — deleting the field would be a wider,
    out-of-scope rewrite of this file's many ``private_calls``/
    ``private_profile_seed`` assertions (still pending a full pass; those
    assertions are stale until then).
    """

    def __init__(self):
        self.public_calls = 0
        self.private_calls = 0
        self.contexts: list[str] = []
        self.pubs_for_synthesis: list[list[dict]] = []

    @property
    def llm_calls(self) -> int:
        return self.public_calls + self.private_calls

    def install(self, monkeypatch):
        real_public = profile_pipeline.synthesize_profile
        real_ctx = profile_pipeline._build_synthesis_context

        async def public(context_text, researcher_name):
            self.public_calls += 1
            return await real_public(context_text, researcher_name)

        def ctx(**kwargs):
            out = real_ctx(**kwargs)
            self.contexts.append(out)
            self.pubs_for_synthesis.append(list(kwargs.get("publications") or []))
            return out

        monkeypatch.setattr(profile_pipeline, "synthesize_profile", public)
        monkeypatch.setattr(profile_pipeline, "_build_synthesis_context", ctx)
        return self


async def seed_pi(db_session, tmp_path, monkeypatch, *, orcid_id=RACKI):
    """A User with no name/institution + the AgentRegistry row the revision leg needs.

    The name is left blank on purpose: step 1 of the pipeline fills it from ORCID only
    when it is falsy, so a blank name turns "did ORCID actually reach the User row?" into
    an observable.

    `PROFILES_DIR` is redirected at a tmp dir so a live run cannot leave a stray
    `profiles/public/<agent>.md` in the working tree. The export code itself is
    untouched — the revision content is still whatever `export_profile_to_markdown`
    wrote and read back.
    """
    suffix = uuid.uuid4().hex[:8]
    monkeypatch.setattr("src.services.profile_export.PROFILES_DIR", tmp_path / "public")

    user = await factories.make_user(
        db_session,
        name="",
        orcid=orcid_id,
        institution=None,
        department=None,
        email=f"t4-{suffix}@example.edu",
        onboarding_complete=False,
    )
    agent = await factories.make_agent(
        db_session,
        user=user,
        agent_id=f"t4live{suffix}",
        bot_name=f"T4Live{suffix}Bot",
        pi_name="T4 live subject",
        status="pending",
    )
    return user, agent


async def profile_rows(db_session, user_id) -> list[ResearcherProfile]:
    res = await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )
    return list(res.scalars().all())


async def revisions(db_session, agent_id) -> list[ProfileRevision]:
    res = await db_session.execute(
        select(ProfileRevision)
        .where(ProfileRevision.agent_registry_id == agent_id)
        .order_by(ProfileRevision.created_at)
    )
    return list(res.scalars().all())


async def publications(db_session, user_id) -> list[Publication]:
    res = await db_session.execute(
        select(Publication).where(Publication.user_id == user_id)
    )
    return list(res.scalars().all())


def as_synthesized(profile: ResearcherProfile) -> dict:
    """The dict `_validate_profile` saw, reconstructed from what step 9 stored."""
    return {
        "research_summary": profile.research_summary or "",
        "techniques": profile.techniques or [],
        "experimental_models": profile.experimental_models or [],
        "disease_areas": profile.disease_areas or [],
        "key_targets": profile.key_targets or [],
        "keywords": profile.keywords or [],
    }


def profile_prose(profile: ResearcherProfile) -> str:
    """Every free-text field of the profile, concatenated, for the grounding check."""
    fields = [profile.research_summary or ""]
    for lst in (
        profile.techniques, profile.experimental_models, profile.disease_areas,
        profile.key_targets, profile.keywords, profile.grant_titles,
    ):
        fields.extend(lst or [])
    return " ".join(fields)


# Observations shared between tests. Each live pipeline run costs real money and real
# time, so T4.4 and T4.5 read what T4.1/T4.2 already measured rather than re-running.
# Every consumer states loudly when the producer did not run, so a `-k` selection can
# never turn a missing comparison into a silent pass.
_OBSERVED: dict[str, dict] = {}


def require_observation(key: str, producer: str) -> dict:
    if key not in _OBSERVED:
        pytest.skip(
            f"this test compares against the live run recorded by {producer}, which did "
            "not run in this session (deselected, or it failed before recording). "
            "Nothing was verified — do not read this skip as a pass."
        )
    return _OBSERVED[key]


# =========================================================================== T4.1


async def test_t41_one_real_orcid_becomes_a_stored_profile_grounded_in_its_works(
    db_session, tmp_path, monkeypatch, api_budget
):
    """T4.1 — one real ORCID, all the way to a stored profile, over a real database.

    The control is the last block, and it has three layers because the first two are not
    enough on their own:

      1. the vocabulary is derived from a live ORCID fetch the test performs itself, not
         from the pipeline's own DB writes, so a pipeline that stored nothing cannot make
         the comparison vacuous;
      2. a hand-written decoy profile must score zero, proving the matcher can say no;
      3. and — the layer that actually matters — the same model is asked for a profile of
         the same person with an EMPTY publication list, and the real run must produce
         corpus vocabulary the name-only run did not. Layers 1 and 2 alone are satisfied
         by a wholly hallucinated profile, which is exactly what layer 3 measures.
    """
    user, agent = await seed_pi(db_session, tmp_path, monkeypatch)
    probe = PipelineProbe().install(monkeypatch)

    api_budget.wait("orcid")
    profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    # --- the row exists, exactly once, and step 1 reached the User record ------------
    rows = await profile_rows(db_session, user.id)
    assert len(rows) == 1, (
        f"{len(rows)} ResearcherProfile rows for one user after one run. "
        "researcher_profiles.user_id is UNIQUE, so anything but 1 means the pipeline is "
        "writing through a path the constraint does not cover"
    )
    assert rows[0].id == profile.id

    await db_session.refresh(user)
    assert user.name and user.name.strip(), (
        "the User row still has a blank name after a successful run. Step 1 fills it "
        "from ORCID; a blank name means fetch_orcid_profile raised and the pipeline "
        "swallowed it (provider down or the person.name shape changed) — every "
        "downstream assertion here would then be about an ORCID-less run"
    )
    assert user.institution, (
        "ORCID reported no institution for a record whose employment block is populated "
        "— fetch_orcid_profile's affiliation-group traversal, or ORCID's shape, changed"
    )

    # --- the synthesized fields ------------------------------------------------------
    assert profile.research_summary and profile.research_summary.strip(), (
        "research_summary is empty after a run that reported success. synthesize_profile "
        "raised and the pipeline swallowed it (see the logged 'LLM synthesis failed'): "
        "Anthropic is down, the key is bad, or the model's reply did not parse as JSON"
    )
    assert isinstance(profile.techniques, list) and len(profile.techniques) >= 3, (
        f"techniques is {profile.techniques!r}; the synthesis prompt requires >=3 and "
        "_validate_profile enforces it, so this is a validation bypass, not model drift"
    )
    assert isinstance(profile.keywords, list) and profile.keywords, (
        f"keywords is {profile.keywords!r}. T4.1 requires it non-empty. Note that "
        "prompts/profile-synthesis.md calls keywords OPTIONAL and _validate_profile does "
        "not check it, so an empty list here is a gap between the plan and the prompt, "
        "not a pipeline fault"
    )
    assert isinstance(profile.disease_areas, list) and profile.disease_areas
    assert profile.profile_version == 1, (
        f"profile_version is {profile.profile_version} after the first run; step 9 "
        "increments it only when synthesis returned something, so 0 means the fields "
        "above came from somewhere else"
    )
    assert profile.profile_generated_at is not None

    # T4.1 asks for a non-empty `private_profile_md`. The pipeline NEVER sets that
    # column — step 9b writes `private_profile_seed`, and `private_profile_md` is the
    # live copy the PI edits later through the web UI. GM #1 pins the same thing
    # ('private_profile_md': None in the snapshot). Both halves are asserted so the
    # discrepancy is recorded rather than quietly reinterpreted.
    assert profile.private_profile_seed and profile.private_profile_seed.strip(), (
        "step 9b produced no private-profile seed. synthesize_private_profile raised "
        "(logged as 'Private profile seed generation failed') — same three causes as "
        "the public synthesis above"
    )
    assert profile.private_profile_md is None, (
        "the pipeline set private_profile_md. It has never done that (step 9b writes "
        "private_profile_seed, and GM #1 snapshots private_profile_md as None); if this "
        "changed, the PI's hand-edited private profile is now being overwritten by a "
        "monthly refresh"
    )

    # --- _validate_profile accepted it, and the row says so ---------------------------
    assert profile_pipeline._validate_profile(as_synthesized(profile)) is True, (
        "the profile the pipeline STORED does not pass _validate_profile. Since 0023 "
        "step 9 records that verdict in synthesis_validated rather than discarding it, "
        "so this should be impossible for a True flag below — a mismatch means the "
        "stored fields and the recorded verdict came from different syntheses"
    )
    assert profile.synthesis_validated is True, (
        f"synthesis_validated is {profile.synthesis_validated!r} after a run whose "
        "stored fields pass the validator. False means step 8's retry also failed and "
        "the draft was stored marked (the run cost 2 public calls — see below); None "
        "means step 9 stored the fields without recording the verdict, which is the "
        "pre-0023 defect back again"
    )
    # Grounded, and the row can prove it: this is the assertion that separates a real
    # profile from the one T4.4 produces with PubMed unreachable.
    assert (profile.evidence_pub_count or 0) > 0, (
        f"evidence_pub_count is {profile.evidence_pub_count!r} after a live run over a "
        f"real corpus ({profile.evidence_pmid_count!r} PMIDs in hand). Either no abstract "
        "reached the prompt — in which case this whole test is measuring a fabricated "
        "profile — or step 9 is not writing the count, and a fabricated profile is "
        "indistinguishable from this one again"
    )
    assert profile.evidence_state == "grounded", (
        f"evidence_state is {profile.evidence_state!r} for a live run with a real corpus"
    )
    assert probe.public_calls == 1, (
        f"{probe.public_calls} public-synthesis calls. 2 means validation rejected the "
        "first reply and the stricter retry fired — the profile is still stored, but "
        "the run cost double and GM #1's 'exactly two LLM calls' no longer holds"
    )
    assert probe.private_calls == 1

    # --- publications were persisted ---------------------------------------------------
    pubs = await publications(db_session, user.id)
    assert pubs, (
        "no Publication rows. Either ORCID returned no works (provider/record change) or "
        "every PubMed fetch failed (NCBI down or rate-limiting — look for 'Failed to "
        "fetch PubMed batch'). The grounding check below cannot mean anything without them"
    )
    assert all(p.pmid for p in pubs), "a Publication was stored with no PMID"
    assert all(p.title and p.title.strip() for p in pubs), (
        "a Publication was stored with an empty title — _parse_pubmed_xml's ArticleTitle "
        "read is broken, or NCBI moved it"
    )
    assert len({p.pmid for p in pubs}) == len(pubs), (
        "duplicate PMIDs stored for one user on a single run"
    )

    # --- a revision was recorded ---------------------------------------------------------
    revs = await revisions(db_session, agent.id)
    assert len(revs) == 1, (
        f"{len(revs)} ProfileRevision rows after one run, expected 1. The revision leg is "
        "gated on BOTH an AgentRegistry row and a successful markdown export, so 0 means "
        "export_profile_to_markdown returned None (a filesystem problem), not an LLM one"
    )
    assert revs[0].profile_type == "public"
    assert revs[0].mechanism == "pipeline"
    assert profile.research_summary in revs[0].content, (
        "the recorded revision does not contain the summary that was just generated — "
        "the revision is snapshotting a stale export"
    )

    # --- THE CONTROL: is the summary grounded in the works that were fetched? -----------
    api_budget.wait("orcid")
    live_works = await orcid_service.fetch_orcid_works(RACKI)
    assert len(live_works) >= 5, (
        f"ORCID returned {len(live_works)} works for {RACKI}; the grounding check needs a "
        "real corpus to derive vocabulary from and proves nothing without one"
    )
    corpus = distinctive_corpus_terms([w.get("title", "") for w in live_works])
    assert len(corpus) >= 3, (
        f"only {len(corpus)} distinctive terms could be derived from the live work titles "
        f"({sorted(corpus)}). The check below would be near-vacuous; the record's titles "
        "have changed character and this test needs a different subject"
    )

    decoy_hits = mentioned(_HALLUCINATION_DECOY, corpus)
    assert not decoy_hits, (
        f"the derived vocabulary {sorted(corpus)} also matches a completely invented "
        f"profile (on {decoy_hits}). The matcher cannot say no, so the assertion below "
        "would pass for a hallucinated summary — tighten _GENERIC or min_len"
    )

    summary_hits = mentioned(profile.research_summary, corpus)
    assert summary_hits, (
        "THE GENERATED SUMMARY CONTAINS NOTHING FROM THE FETCHED WORKS. None of the "
        f"terms that recur across this researcher's own publication titles ({sorted(corpus)}) "
        "appears in it, while the same matcher correctly finds nothing in a decoy. Either "
        "the corpus never reached the prompt (check _build_synthesis_context) or the model "
        f"ignored it. Summary was: {profile.research_summary[:400]!r}"
    )
    whole_hits = mentioned(profile_prose(profile), corpus)
    assert len(whole_hits) >= 2, (
        f"the whole profile matches only {whole_hits} of {sorted(corpus)}. One term could "
        "be a coincidence; the profile as a whole should reflect more than one recurring "
        "theme of a corpus this small"
    )

    # --- and the control that makes the two assertions above mean anything --------------
    #
    # The decoy above is hand-written, which only proves the matcher CAN say no. It does
    # not prove it would say no to THIS model writing about THIS person. Measured on
    # 2026-07-30: asked to profile "Lisa Racki, Scripps Research Institute, Integrative
    # Structural and Computational Biology" with an EMPTY publication list, Opus returns a
    # confident, _validate_profile-passing profile that already contains "chromatin",
    # "remodeling" and "histone" — three of the seven derived corpus terms — purely from
    # what it remembers about her. A grounding check that accepted any corpus term would
    # therefore pass for a pipeline that fetched nothing at all.
    #
    # So the control is run empirically, here, against the same model on the same day:
    # synthesize once more from a context stripped of every work, and require the real
    # run to have said something the name-only run did NOT. That difference is the only
    # part of the summary that is attributable to the fetched works rather than to the
    # model's prior knowledge of the researcher.
    from src.services.llm import synthesize_profile as _raw_synthesize

    nameonly_context = (
        "## Researcher Information\n"
        f"- Name: {user.name}\n"
        f"- Institution: {user.institution}\n"
        f"- Department: {user.department}"
    )
    assert not mentioned(nameonly_context, corpus), (
        "the name-only control context already contains corpus vocabulary, so the "
        "comparison below would understate grounding"
    )
    ungrounded = await _raw_synthesize(nameonly_context, user.name)
    ungrounded_hits = mentioned(ungrounded.get("research_summary", ""), corpus)

    evidence = sorted(set(summary_hits) - set(ungrounded_hits))
    assert evidence, (
        "THE PROFILE CANNOT BE SHOWN TO HAVE USED THE FETCHED WORKS. Every corpus term "
        f"in the generated summary ({sorted(summary_hits)}) is also produced by the same "
        "model given nothing but this researcher's name, institution and department "
        f"({sorted(ungrounded_hits)}). A pipeline whose ORCID and PubMed legs were both "
        "dead would have produced an equally 'grounded'-looking profile, so this run "
        "provides no evidence that the corpus reached the model. Grounded summary: "
        f"{profile.research_summary[:300]!r} ... Name-only summary: "
        f"{ungrounded.get('research_summary', '')[:300]!r}"
    )

    _OBSERVED["single_run"] = {
        "context": probe.contexts[0],
        "pubs_for_synthesis": probe.pubs_for_synthesis[0],
        "llm_calls": probe.llm_calls,
        "profile_version": profile.profile_version,
        "research_summary": profile.research_summary,
        "raw_abstracts_hash": profile.raw_abstracts_hash,
        "private_profile_md": profile.private_profile_md,
        "private_profile_seed": profile.private_profile_seed,
        "synthesis_validated": profile.synthesis_validated,
        "evidence_pmid_count": profile.evidence_pmid_count,
        "evidence_pub_count": profile.evidence_pub_count,
        "evidence_state": profile.evidence_state,
        # Read off the ORM object, NOT off as_synthesized() — that helper coerces None to
        # ""/[] for the validator, which would make T4.5's type comparison always pass.
        "field_types": {
            k: type(getattr(profile, k)).__name__
            for k in (
                "research_summary", "techniques", "experimental_models",
                "disease_areas", "key_targets", "keywords",
            )
        },
        "pub_count": len(pubs),
        # Every column GM #1's snapshot enumerates for a stored publication, so T4.5 can
        # reconcile the shape as well as the values.
        "stored_pubs": [
            {
                "pmid": p.pmid, "doi": p.doi, "title": p.title, "journal": p.journal,
                "year": p.year, "abstract": p.abstract, "pmcid": p.pmcid,
            }
            for p in pubs
        ],
        "revision_count": len(revs),
        "corpus": corpus,
        "summary_hits": summary_hits,
        "ungrounded_hits": ungrounded_hits,
        "grounding_evidence": evidence,
    }


# =========================================================================== T4.2


async def test_t42_a_second_run_updates_the_same_row_and_adds_a_second_revision(
    db_session, tmp_path, monkeypatch, api_budget
):
    """T4.2 — idempotency, both halves.

    Half one: the profile row must not be duplicated. Half two: a *second*
    ProfileRevision must nonetheless be written — history is append-only, and a
    "no duplicates" implementation that also skipped the revision would satisfy half one
    while silently losing the audit trail. The publication rows are checked the same
    way: updated in place, never re-inserted.
    """
    user, agent = await seed_pi(db_session, tmp_path, monkeypatch)
    probe = PipelineProbe().install(monkeypatch)

    api_budget.wait("orcid")
    first = await profile_pipeline.run_profile_pipeline(user.id, db_session)
    first_id = first.id
    first_version = first.profile_version
    first_seed = first.private_profile_seed
    first_pubs = {p.pmid for p in await publications(db_session, user.id)}
    first_revs = await revisions(db_session, agent.id)
    calls_after_first = probe.llm_calls

    assert first_version == 1 and first_pubs and len(first_revs) == 1, (
        "the first run did not reach a good state, so the second run cannot test "
        f"idempotency: version={first_version} pubs={len(first_pubs)} "
        f"revisions={len(first_revs)}"
    )

    api_budget.wait("orcid")
    second = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    # --- half one: no duplicate profile row -------------------------------------------
    rows = await profile_rows(db_session, user.id)
    assert len(rows) == 1, (
        f"{len(rows)} ResearcherProfile rows after two runs — step 6's "
        "select-then-create is inserting instead of loading"
    )
    assert second.id == first_id, "the second run created a different profile row"
    assert second.profile_version == first_version + 1 == 2, (
        f"profile_version went {first_version} -> {second.profile_version}; step 9 "
        "increments by one per successful synthesis"
    )
    second_pubs = {p.pmid for p in await publications(db_session, user.id)}
    assert second_pubs == first_pubs, (
        "the publication set changed between two runs of the same corpus. New PMIDs "
        f"({sorted(second_pubs - first_pubs)}) mean the existing-publication lookup "
        f"missed; lost PMIDs ({sorted(first_pubs - second_pubs)}) mean a fetch failed"
    )
    all_pubs = await publications(db_session, user.id)
    assert len(all_pubs) == len(second_pubs), (
        f"{len(all_pubs)} Publication rows for {len(second_pubs)} distinct PMIDs — the "
        "second run re-inserted instead of updating"
    )

    # --- half two: a SECOND revision was recorded --------------------------------------
    revs = await revisions(db_session, agent.id)
    assert len(revs) == 2, (
        f"{len(revs)} ProfileRevision rows after two runs, expected 2. Profile history is "
        "append-only and the monthly refresh depends on it; one row means the second run "
        "overwrote history, which is the failure that makes 'what changed?' unanswerable"
    )
    assert revs[0].id != revs[1].id, "the same revision row was returned twice"
    assert all(r.mechanism == "pipeline" and r.profile_type == "public" for r in revs)
    assert second.research_summary in revs[-1].content, (
        "the second revision does not contain the second run's summary"
    )

    # The seed is generated once and then left alone (GM #4 pins this). A pipeline that
    # regenerated it every month would silently discard the PI's edits.
    assert second.private_profile_seed == first_seed, (
        "the re-run regenerated private_profile_seed. Step 9b is guarded on the seed "
        "being absent; if that guard broke, every refresh overwrites the PI's staged text"
    )

    _OBSERVED["rerun"] = {
        "first_version": first_version,
        "second_version": second.profile_version,
        "same_profile_row": second.id == first_id,
        "pub_count_after_two_runs": len(all_pubs),
        "seed_set_after_first_run": first_seed is not None,
        "seed_unchanged_on_rerun": second.private_profile_seed == first_seed,
        "llm_calls_total": probe.llm_calls,
        "llm_calls_first_run": calls_after_first,
        "revision_count": len(revs),
    }


# =========================================================================== T4.3


async def test_t43_the_synthesis_context_is_bounded_and_contains_the_fetched_works(
    api_budget,
):
    """T4.3 — `_build_synthesis_context` under a real corpus.

    Spends no Anthropic calls: the context is a pure function of the fetched data, and
    the fetched data is the expensive-to-fake part. The pipeline's step 3-4 sequence is
    reproduced here rather than observed through a run, so this test stands alone and a
    `-k t43` selection still verifies something.

    Two independent properties:
      * BOUNDED — it goes verbatim into a prompt, so an unbounded context is a bill and
        a context-window overflow. The 30-publication cap is exercised with a padded
        corpus, because a live record with 12 works cannot reach it.
      * COMPLETE — every publication that survived the research-article filter appears.
        Control: the assertion is preceded by a check that there is more than a header
        to find, so "all N titles present" cannot be satisfied by N == 0.
    """
    api_budget.wait("orcid")
    profile = await orcid_service.fetch_orcid_profile(RACKI)
    api_budget.wait("orcid")
    works = await orcid_service.fetch_orcid_works(RACKI)
    assert works, "ORCID returned no works — nothing below would be meaningful"

    pmids = [w["pmid"] for w in works if w.get("pmid")]
    doi_only = sorted({w["doi"] for w in works if w.get("doi") and not w.get("pmid")})
    if doi_only:
        api_budget.wait("ncbi")
        resolved = await pubmed.convert_dois_to_pmids(doi_only)
        pmids.extend(resolved.values())
    pmids = sorted(set(pmids))
    assert len(pmids) >= 5, (
        f"only {len(pmids)} PMIDs resolved from {len(works)} ORCID works. Either NCBI is "
        "degraded (convert_dois_to_pmids swallows errors and returns {}) or the record "
        "changed; a corpus this thin makes the completeness check below weak"
    )

    api_budget.wait("ncbi")
    records = await pubmed.fetch_pubmed_records(pmids)
    assert records, (
        "efetch returned nothing for PMIDs that exist — NCBI is down or rate-limiting, "
        "not a parser fault (fetch_pubmed_records logs 'Failed to fetch PubMed batch')"
    )

    for_synthesis = [
        r for r in records
        if r.get("abstract")
        and not any(
            t in profile_pipeline.EXCLUDED_TYPES
            for t in (x.lower() for x in r.get("pub_types", []))
        )
    ]
    assert len(for_synthesis) >= 3, (
        f"only {len(for_synthesis)} of {len(records)} records survived the "
        "research-article + has-abstract filter; the completeness check needs more"
    )

    context = profile_pipeline._build_synthesis_context(
        orcid_profile=profile,
        grant_titles=[],
        publications=for_synthesis,
        methods_by_pmid={},
    )

    # --- complete ----------------------------------------------------------------------
    assert context.count("\n### ") == len(for_synthesis), (
        f"the context has {context.count(chr(10) + '### ')} publication sections for "
        f"{len(for_synthesis)} publications — works are being dropped between step 4 and "
        "the prompt, which is exactly how a profile ends up ungrounded"
    )
    absent = [p["title"] for p in for_synthesis if p.get("title") and p["title"] not in context]
    assert not absent, (
        f"{len(absent)} fetched publication titles never reach the prompt: {absent[:3]}"
    )
    with_abstract = sum(1 for p in for_synthesis if p["abstract"][:1500] in context)
    assert with_abstract == len(for_synthesis), (
        f"only {with_abstract}/{len(for_synthesis)} abstracts appear in the context — the "
        "titles are there but the evidence is not"
    )
    assert profile.get("name", "") in context

    # --- bounded -------------------------------------------------------------------------
    # Arithmetic the code commits to: <=30 publications, each abstract truncated at 1500
    # chars, <=10 methods sections truncated at 2000. Header and grant lines are small.
    ceiling = 30 * (1500 + 400) + 10 * (2000 + 100) + 4000
    assert len(context) <= ceiling, (
        f"the synthesis context is {len(context)} chars, over the {ceiling}-char ceiling "
        "implied by the truncation constants in _build_synthesis_context. One of those "
        "truncations has been removed and the whole abstract corpus is now going into "
        "every prompt"
    )

    long_abstracts = [p for p in for_synthesis if len(p.get("abstract", "")) > 1500]
    if long_abstracts:
        p = long_abstracts[0]
        assert p["abstract"] not in context, (
            f"the full {len(p['abstract'])}-char abstract for PMID {p.get('pmid')} is in "
            "the context — the [:1500] truncation is gone"
        )
        assert p["abstract"][:1500] in context
    else:
        # Not a skip: the rest of the test is unaffected, but the reader should know the
        # truncation itself went unexercised on today's data.
        assert all(len(p.get("abstract", "")) <= 1500 for p in for_synthesis)

    # The 30-publication cap, exercised with a padded corpus built from the live records.
    padded = []
    for i in range(40):
        src = dict(for_synthesis[i % len(for_synthesis)])
        src["title"] = f"PAD{i:02d} {src.get('title', '')}"
        src["year"] = 2100 - i          # strictly descending, so the order is knowable
        padded.append(src)
    capped = profile_pipeline._build_synthesis_context(
        orcid_profile=profile, grant_titles=[], publications=padded, methods_by_pmid={}
    )
    assert capped.count("\n### ") == 30, (
        f"a 40-publication corpus produced {capped.count(chr(10) + '### ')} sections; "
        "_build_synthesis_context caps at 30 and an uncapped prompt scales with a PI's "
        "whole career"
    )
    assert "PAD00 " in capped and "PAD29 " in capped, (
        "the 30 kept publications are not the 30 most recent — the sort is broken"
    )
    assert "PAD30 " not in capped and "PAD39 " not in capped


# =========================================================================== T4.4


def _ncbi_hosts() -> set[str]:
    """The hosts every NCBI call in the system goes to, read from the code."""
    return {
        urlparse(pubmed.EUTILS_BASE).hostname,
        urlparse(pubmed.IDCONV_BASE).hostname,
    }


async def test_t44_pubmed_unreachable_still_yields_a_profile_but_a_measurably_thinner_one(
    db_session, tmp_path, monkeypatch, api_budget
):
    """T4.4 — degradation. NCBI unreachable; ORCID and Anthropic still live.

    respx is used *inside* the live tier: every host `src/services/pubmed.py` talks to
    raises ConnectError, and everything else — ORCID, api.anthropic.com — passes through
    to the real network. That is the honest simulation of "PubMed is down" and it is the
    reason this test is here rather than in the contract tier.

    Requirement: onboarding must not fail. Control: the degraded profile must be
    measurably thinner, otherwise "it still produced a profile" is satisfied by a
    pipeline that never used PubMed in the first place.

    The thinness assertions are deliberately the deterministic ones — publication rows,
    the abstract hash, and the size and structure of the synthesis context, which IS the
    profile's entire evidence base. Word counts of model prose are not evidence of
    anything.
    """
    baseline = require_observation("single_run", "T4.1")
    user, _agent = await seed_pi(db_session, tmp_path, monkeypatch)
    probe = PipelineProbe().install(monkeypatch)

    hosts = _ncbi_hosts()
    assert hosts and None not in hosts, f"could not read NCBI hosts from the code: {hosts}"

    api_budget.wait("orcid")
    with respx.mock(assert_all_called=False) as router:
        for host in hosts:
            router.route(host=host).mock(
                side_effect=httpx.ConnectError(f"simulated {host} outage (T4.4)")
            )
        blocked = [r for r in router.routes]
        router.route().pass_through()   # ORCID and Anthropic reach the real network

        profile = await profile_pipeline.run_profile_pipeline(user.id, db_session)

        ncbi_attempts = sum(r.call_count for r in blocked)

    # Control on the simulation itself: if the pipeline never tried to reach NCBI, this
    # test degraded nothing and its result means nothing.
    assert ncbi_attempts > 0, (
        f"the pipeline made no request to any of {sorted(hosts)}, so the simulated outage "
        "blocked nothing. Either the URLs moved or PubMed is no longer on this path — "
        "either way the 'degraded' profile below is just a normal profile"
    )

    # --- onboarding still completes ----------------------------------------------------
    rows = await profile_rows(db_session, user.id)
    assert len(rows) == 1, (
        f"{len(rows)} profile rows with PubMed down; a PubMed outage must not stop a PI "
        "being onboarded"
    )
    assert profile.research_summary and profile.research_summary.strip(), (
        "with PubMed unreachable the pipeline produced no summary at all. Steps 4 and 5 "
        "are individually try/excepted precisely so a PubMed outage degrades rather than "
        "aborts; if this fails, one of those handlers stopped catching"
    )
    assert profile.profile_version == 1
    await db_session.refresh(user)
    assert user.name and user.name.strip(), (
        "ORCID data did not land either — the pass-through route is broken and this test "
        "simulated a total outage, not a PubMed one"
    )

    # --- the control: measurably thinner -------------------------------------------------
    degraded_pubs = await publications(db_session, user.id)
    assert degraded_pubs == [], (
        f"{len(degraded_pubs)} Publication rows were stored while every NCBI host was "
        "unreachable — those rows came from somewhere other than PubMed"
    )
    assert baseline["pub_count"] > 0, (
        "the T4.1 baseline stored no publications either, so 'thinner' is not measurable"
    )
    assert profile.raw_abstracts_hash == hashlib.sha256(b"").hexdigest(), (
        "the abstract hash is not the hash of an empty corpus, so the pipeline thinks it "
        f"synthesized from abstracts it never fetched: {profile.raw_abstracts_hash}"
    )
    assert profile.raw_abstracts_hash != baseline["raw_abstracts_hash"]

    degraded_context = probe.contexts[0]
    full_context = baseline["context"]
    assert "## Publications" not in degraded_context, (
        "the degraded synthesis context still has a Publications section"
    )
    assert "## Publications" in full_context, (
        "the T4.1 baseline context had no Publications section either — the comparison "
        "below is meaningless"
    )
    assert len(degraded_context) < 0.2 * len(full_context), (
        f"the degraded context is {len(degraded_context)} chars against the baseline's "
        f"{len(full_context)} — not measurably thinner, so PubMed was contributing almost "
        "nothing to the prompt even when it was up"
    )

    # The sharpest statement of the degradation, and the reason this is worth a test:
    # with PubMed down the prompt contains none of the researcher's own subject matter,
    # because ORCID *works* only enter the context via their PubMed records. Whatever the
    # model then writes is not grounded in anything the pipeline fetched.
    corpus = baseline["corpus"]
    assert not mentioned(degraded_context, corpus), (
        "the degraded context still contains the corpus vocabulary "
        f"{mentioned(degraded_context, corpus)}, so ORCID works are reaching the prompt "
        "by some path and the claim below would be wrong"
    )
    assert mentioned(full_context, corpus), (
        "the baseline context contains none of the corpus vocabulary — the derivation is "
        "broken, not the pipeline"
    )

    # Still true, and still worth asserting: the profile synthesized from a name and a
    # department passes the same validator as the one synthesized from a dozen abstracts.
    # _validate_profile only measures SHAPE — a 150-250 word summary, three techniques, a
    # disease area — and a fluent model satisfies all three from prior knowledge. No
    # amount of tightening the validator finds this case, which is why the discriminator
    # below is a count of evidence and not a quality score.
    assert profile_pipeline._validate_profile(as_synthesized(profile)) is True, (
        "the evidence-free profile now FAILS _validate_profile. That is an improvement, "
        "not a regression, but it changes the pipeline's behaviour under a PubMed outage "
        "(step 8 would retry, then store the draft marked unvalidated) and this test "
        "needs updating"
    )
    assert profile.synthesis_validated is True, (
        f"synthesis_validated is {profile.synthesis_validated!r}; the assertion above says "
        "the stored fields pass the validator, so the recorded verdict disagrees with the "
        "validator applied to the same row"
    )

    # What USED to be the finding here: the fabricated profile was stored with the same
    # profile_version 1 and no marker of any kind, so nothing downstream — the agent
    # prompt builder, the public profile page, the monthly refresh — could tell it from a
    # profile grounded in a dozen abstracts. Migration 0023 closed that. The row now
    # carries what the synthesis was actually founded on, and this is the live proof of
    # it: ORCID gave the pipeline identifiers, PubMed gave it nothing, so the profile is
    # ungrounded AND says which of the two ungrounded cases it is.
    assert profile.evidence_pub_count == 0, (
        f"evidence_pub_count is {profile.evidence_pub_count!r} while every NCBI host was "
        "unreachable. No abstract can have reached the prompt, so a non-zero count means "
        "step 9 is recording something other than what it synthesized from"
    )
    assert (profile.evidence_pmid_count or 0) > 0, (
        f"evidence_pmid_count is {profile.evidence_pmid_count!r}. ORCID is up in this test "
        "and this record carries PMIDs directly (7 of 12 as of 2026-07-30), so zero means "
        "the ORCID leg failed too and this is a total outage, not a PubMed one — and the "
        "state below would then be 'lost' for the wrong reason"
    )
    assert profile.evidence_state == "evidence_lost", (
        f"the fabricated profile reports evidence_state {profile.evidence_state!r}. "
        "'no_evidence_available' would be a false negative — it is the answer reserved "
        "for a researcher who genuinely has nothing indexed, and it tells an operator "
        "NOT to regenerate, which is exactly wrong after an outage"
    )
    # The comparison that makes the discriminator meaningful: the grounded baseline and
    # this run are the same profile_version, so version cannot separate them and the
    # evidence counts must.
    assert profile.profile_version == baseline["profile_version"], (
        "the degraded and grounded runs no longer share a profile_version, so the claim "
        "that they are indistinguishable without the evidence counts is out of date"
    )
    assert baseline["evidence_state"] == "grounded" != profile.evidence_state, (
        f"the grounded baseline reports evidence_state {baseline['evidence_state']!r} and "
        f"this ungrounded run reports {profile.evidence_state!r} — the column does not "
        "separate the two cases it exists to separate"
    )

    # Thinness at the level of the profile text, not just its evidence base. The degraded
    # summary is NOT empty of the researcher's subject matter — the model recognises the
    # name — so the measurable claim is a strict subset, not an absence. Measured
    # 2026-07-30: the degraded summary reached 2 of the 7 corpus terms (chromatin,
    # remodeling) against 7 of 7 for the grounded run. If this ever came out equal, the
    # PubMed leg would be contributing nothing the model did not already know.
    degraded_hits = set(mentioned(profile.research_summary, corpus))
    baseline_hits = set(baseline["summary_hits"])
    assert degraded_hits < baseline_hits, (
        f"the degraded summary covers {sorted(degraded_hits)} of the corpus vocabulary "
        f"and the grounded one covers {sorted(baseline_hits)} — not a strict subset, so "
        "losing PubMed entirely cost the profile nothing measurable and the pipeline's "
        "whole PubMed leg is decorative for this researcher"
    )

    _OBSERVED["degraded"] = {
        "context_len": len(degraded_context),
        "baseline_context_len": len(full_context),
        "summary": profile.research_summary,
        "summary_hits": mentioned(profile.research_summary, corpus),
        "baseline_summary_hits": baseline["summary_hits"],
        "validated": profile_pipeline._validate_profile(as_synthesized(profile)),
        "ncbi_attempts": ncbi_attempts,
    }


# =========================================================================== T4.5


def _gm_claims() -> dict:
    """What the four mocked golden masters assert, restated as checkable claims.

    Read out of the snapshot file rather than retyped, so a `--snapshot-update` that
    changed a GM cannot leave this reconciliation silently describing the old one.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "characterization" / "__snapshots__" / "test_profile_pipeline_gm.ambr"
    )
    return {"text": path.read_text(encoding="utf-8"), "path": path}


async def test_t45_the_four_mocked_golden_masters_still_describe_the_live_shape(
    db_session, tmp_path, monkeypatch, api_budget
):
    """T4.5 — reconcile the live run against the four mocked golden masters.

    The GM suite is entirely fake-driven. Each of its four snapshots makes claims that a
    live run can confirm or refute; this test states which claim each one makes, checks
    it against live observations, and names the snapshot that is wrong when they differ.

      GM #1 test_profile_pipeline_golden_master
            one run -> profile_version 1, private_profile_md None, seed set, the six
            synthesized fields are list/str, publications carry
            pmid/doi/title/journal/year/pmcid/abstract, raw_abstracts_hash is the sha256
            of the joined abstracts, and exactly two LLM calls.
      GM #2 test_profile_pipeline_llm_failure_leaves_fields_unset
            synthesis raises -> version stays 0, fields stay None, hash still set.
            Reproduced live below with an unauthenticated Anthropic client: a real 401
            from api.anthropic.com, zero tokens.
      GM #3 test_profile_pipeline_doi_correction_stores_authoritative
            the stored DOI is the one PubMed has on file for that PMID, never an
            unverified ORCID candidate. Checked against live esummary.
      GM #4 test_profile_pipeline_rerun_increments_version_and_updates_pubs
            1 -> 2, same row, publication count stable, seed unchanged, 3 LLM calls.
    """
    single = require_observation("single_run", "T4.1")
    rerun = require_observation("rerun", "T4.2")
    snap = _gm_claims()
    assert "test_profile_pipeline_golden_master" in snap["text"], (
        f"{snap['path']} does not contain the golden master this test reconciles against"
    )

    # --- GM #1 -----------------------------------------------------------------------
    assert single["profile_version"] == 1, (
        "GM #1 snapshots profile_version == 1 after one run; live gave "
        f"{single['profile_version']}. GM #1 is wrong (or step 9's increment changed)"
    )
    assert "'profile_version': 1," in snap["text"]
    assert single["private_profile_md"] is None and "'private_profile_md': None," in snap["text"], (
        "GM #1 snapshots private_profile_md as None. Live disagrees, so GM #1 is wrong "
        "about which private column the pipeline writes"
    )
    assert single["private_profile_seed"], (
        "GM #1 snapshots a non-empty private_profile_seed; live produced none"
    )
    expected_types = {
        "research_summary": "str", "techniques": "list", "experimental_models": "list",
        "disease_areas": "list", "key_targets": "list", "keywords": "list",
    }
    assert single["field_types"] == expected_types, (
        "the live profile's field types differ from the ones GM #1's snapshot encodes "
        f"(str + five lists): {single['field_types']}. GM #1's _VALID_PROFILE fixture no "
        "longer matches what a real model returns through _extract_json"
    )
    # GM #1's snapshot enumerates seven columns per stored publication. A live row must
    # populate the identifying ones; pmcid and abstract are legitimately null for some
    # records (Watson & Crick has neither), so those are checked for presence-of-key only.
    gm_pub_keys = {"pmid", "doi", "title", "journal", "year", "pmcid", "abstract"}
    for row in single["stored_pubs"]:
        assert gm_pub_keys <= set(row), (
            "a live Publication row is missing columns GM #1's snapshot enumerates: "
            f"{sorted(gm_pub_keys - set(row))}"
        )
    populated = {
        k for k in gm_pub_keys
        if any(row.get(k) not in (None, "") for row in single["stored_pubs"])
    }
    assert populated >= {"pmid", "doi", "title", "journal", "year"}, (
        "GM #1's snapshot shows every publication carrying pmid/doi/title/journal/year. "
        f"Across {len(single['stored_pubs'])} live rows only {sorted(populated)} were ever "
        "populated, so the GM overstates what the real ingest produces"
    )
    # The hash rule, recomputed from the real records the real run synthesized from.
    expected_hash = hashlib.sha256(
        "\n".join(p.get("abstract", "") for p in single["pubs_for_synthesis"]).encode()
    ).hexdigest()
    assert single["raw_abstracts_hash"] == expected_hash, (
        "GM #1 pins raw_abstracts_hash as the sha256 of the newline-joined abstracts of "
        "the publications passed to synthesis. Recomputing that over the live corpus "
        f"gives {expected_hash[:12]}… but the pipeline stored "
        f"{single['raw_abstracts_hash'][:12]}… — the hashed set is not the synthesized set"
    )
    assert single["llm_calls"] == 2, (
        f"GM #1 asserts exactly two LLM calls on the happy path; the live run made "
        f"{single['llm_calls']}. Three means _validate_profile rejected a real model's "
        "output and the retry fired — the GM never sees that because its fixture is "
        "hand-tuned to pass validation, so the GM understates the real cost per profile"
    )

    # --- GM #4 -----------------------------------------------------------------------
    gm4_expected = {
        "first_version": 1,
        "second_version": 2,
        "same_profile_row": True,
        "seed_set_after_first_run": True,
        "seed_unchanged_on_rerun": True,
        "llm_calls_total": 3,
    }
    gm4_live = {k: rerun[k] for k in gm4_expected}
    assert gm4_live == gm4_expected, (
        "GM #4 (test_profile_pipeline_rerun_increments_version_and_updates_pubs) does not "
        f"describe the live rerun. Snapshot claims {gm4_expected}, live measured "
        f"{gm4_live}. If llm_calls_total differs, validation is rejecting real model "
        "output; anything else is a behaviour change the GM has not been updated for"
    )
    assert rerun["pub_count_after_two_runs"] == single["pub_count"], (
        "GM #4 pins the publication count as unchanged across two runs. Live: "
        f"{single['pub_count']} after one run, {rerun['pub_count_after_two_runs']} after two"
    )

    # --- GM #3, against live esummary --------------------------------------------------
    # GM #3 pins the mismatch branch with synthetic DOIs. What a live run can check is
    # the invariant that branch exists to protect: nothing is persisted that disagrees
    # with the DOI PubMed has on file for that exact PMID.
    stored = {p["pmid"]: p["doi"] for p in single["stored_pubs"] if p["doi"]}
    assert stored, "no stored DOIs to reconcile — GM #3's invariant is untestable here"
    api_budget.wait("ncbi")
    authoritative = await pubmed.fetch_authoritative_dois(sorted(stored))
    assert authoritative, (
        "esummary returned no authoritative DOIs; NCBI is degraded (the error is "
        "swallowed and {} returned), so GM #3 could not be reconciled this run"
    )
    disagreements = {
        pmid: (doi, authoritative[pmid])
        for pmid, doi in stored.items()
        if pmid in authoritative and doi.lower() != authoritative[pmid].lower()
    }
    assert not disagreements, (
        "the pipeline persisted DOIs that disagree with the DOI PubMed has on file for "
        f"the same PMID: {disagreements}. GM #3 asserts reconcile_pub_doi overwrites the "
        "candidate with the authoritative value in exactly this situation, so either the "
        "gate is no longer running in the pipeline or normalize_doi stopped canonicalising"
    )
    assert len(set(stored) & set(authoritative)) >= 3, (
        f"only {len(set(stored) & set(authoritative))} PMIDs could be reconciled against "
        "esummary — this leg proved almost nothing"
    )

    # --- GM #2, reproduced against the real API ------------------------------------------
    # Not a fake: the client below talks to api.anthropic.com and is rejected with a real
    # 401. That is the only way to see the pipeline's synthesis-failure path with the real
    # SDK's exception types, and it costs nothing.
    user, _agent = await seed_pi(db_session, tmp_path, monkeypatch)
    probe = PipelineProbe().install(monkeypatch)
    monkeypatch.setattr(
        "src.services.llm.get_anthropic_client",
        lambda: anthropic.Anthropic(api_key="sk-ant-t4-deliberately-invalid"),
    )
    api_budget.wait("orcid")
    failed = await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert probe.public_calls == 1 and probe.private_calls == 1, (
        "the failure path did not attempt both synthesis calls, so GM #2's shape is not "
        f"the one being reconciled: public={probe.public_calls} private={probe.private_calls}"
    )
    gm2_expected = {
        "profile_version": 0,
        "research_summary": None,
        "techniques": None,
        "disease_areas": None,
        "private_profile_seed": None,
        "raw_abstracts_hash_is_set": True,
    }
    gm2_live = {
        "profile_version": failed.profile_version,
        "research_summary": failed.research_summary,
        "techniques": failed.techniques,
        "disease_areas": failed.disease_areas,
        "private_profile_seed": failed.private_profile_seed,
        "raw_abstracts_hash_is_set": failed.raw_abstracts_hash is not None,
    }
    assert gm2_live == gm2_expected, (
        "GM #2 (test_profile_pipeline_llm_failure_leaves_fields_unset) does not describe "
        "what happens when the REAL Anthropic API rejects the call. Snapshot claims "
        f"{gm2_expected}, live measured {gm2_live}. GM #2 raises RuntimeError from a fake; "
        "if the live shape differs, the pipeline's `except Exception` is not catching what "
        "the real SDK raises"
    )
    # Control: the failure must be the one we engineered, not a network outage that would
    # have produced the same all-None row for a different reason.
    assert failed.raw_abstracts_hash != hashlib.sha256(b"").hexdigest(), (
        "the failed run also had an empty abstract corpus, so this reproduced 'PubMed was "
        "down' rather than 'the LLM call failed' and GM #2 was not actually reconciled"
    )
