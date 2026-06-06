"""Generate users + profiles for researchers with sparse/no ORCID data.

Input TSV (tab-separated, no header):

    Name<TAB>ORCID-or-blank<TAB>Aff1|Aff2|...[<TAB>FacultyURL]

For each row this script:
  1. Resolves identity (real ORCID name-check OR synthetic placeholder).
  2. Searches PubMed by name+affiliation, disambiguates by author/affiliation
     token-Jaccard against the input affiliations.
  3. Optionally fetches a faculty/lab page for additional context.
  4. Calls the LLM (prompts/profile-synthesis-sparse.md) to synthesize fields.
  5. Persists User + ResearcherProfile + AgentRegistry rows, writes
     profiles/public/{agent_id}.md. The private (behavioral) seed is left NULL
     on purpose — PIs author that via onboarding, not the LLM.

Floor on evidence: ≥3 disambiguated papers OR a fetched faculty page. Rows
below the floor are audited but not persisted, so a human can review.

Usage (runs inside the app container — needs DB + prompts + profiles):

    docker cp scripts/generate_sparsedata_user.py copi-python-app-1:/app/scripts/
    docker exec copi-python-app-1 python scripts/generate_sparsedata_user.py \\
        --file newuserlist02.tsv --force

Outputs:
  - DB rows
  - profiles/public/{agent_id}.md  (no private seed — see step 5)
  - audit CSV at scripts/_sparse_run_{timestamp}.csv
  - PILOT_LABS snippet printed to stdout
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.llm import _extract_json, get_anthropic_client
from src.services.orcid import fetch_orcid_profile, fetch_orcid_works
from src.services.profile_export import export_profile_to_markdown
from src.services.pubmed import convert_dois_to_pmids, fetch_pubmed_records, normalize_doi

PRIVATE_DIR = Path("profiles/private")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sparse_users")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PROMPT_PATH = Path("prompts/profile-synthesis-sparse.md")

# Disambiguation thresholds
EVIDENCE_FLOOR_PAPERS = 3        # need this many papers OR a faculty page
NAME_MATCH_TOKEN_MIN = 0.50      # min name-token overlap when checking ORCID identity
ORCID_STALE_AFTER_YEARS = 5      # ORCID whose newest work predates (this year - N) is
                                 # treated as stale: drop its PMIDs, use PubMed search
PUBMED_FETCH_CAP = 50            # max PMIDs to pull from a single ESearch
FACULTY_PAGE_MAX_CHARS = 3000    # truncate scraped page text to this

# Generic institution terms that don't disambiguate one university from another.
# Distinctive matching strips these before substring-checking.
INSTITUTION_STOPWORDS: frozenset[str] = frozenset({
    "university", "universite", "universidad", "of", "the", "institute",
    "institution", "research", "school", "department", "dept", "and", "for",
    "center", "centre", "college", "laboratory", "lab", "labs", "medical",
    "national", "technology", "technologies", "science", "sciences",
    "biology", "biological", "chemistry", "chemical", "engineering",
    "graduate", "program", "programs", "division", "faculty", "studies",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InputRow:
    name: str
    orcid: str | None
    affiliations: list[str]
    faculty_url: str | None = None


@dataclass
class AuditRow:
    name: str
    agent_id: str | None = None
    institution: str | None = None
    placeholder_orcid: bool = False
    n_pubmed_hits: int = 0
    n_papers_kept: int = 0
    faculty_page_chars: int = 0
    persisted: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def _name_tokens(s: str) -> set[str]:
    return {t.lower() for t in re.split(r"\W+", s) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _distinctive_aff_tokens(affiliation: str) -> list[str]:
    """Return institution-distinctive tokens from an affiliation string.

    Drops generic stopwords ("university", "institute", etc.). If everything
    is generic (e.g. "UCSF"), falls back to all alpha tokens of length ≥3.
    """
    tokens = re.findall(r"[a-z]+", affiliation.lower())
    distinctive = [t for t in tokens if t not in INSTITUTION_STOPWORDS and len(t) >= 4]
    if distinctive:
        return distinctive
    return [t for t in tokens if len(t) >= 3]


def _aff_match(input_aff: str, paper_aff: str) -> bool:
    """True if any distinctive token from `input_aff` appears in `paper_aff`.

    Asymmetric on purpose: paper affiliation strings are long (author address,
    dept, city, state) and the input is just the institution name, so we want
    "needle in haystack" recall, not symmetric overlap.
    """
    if not paper_aff:
        return False
    paper_lower = paper_aff.lower()
    for tok in _distinctive_aff_tokens(input_aff):
        if tok in paper_lower:
            return True
    return False


def _slugify_agent_id(name: str) -> str:
    last = name.strip().split()[-1].lower()
    return "".join(c for c in last if c.isalpha()) or "lab"


async def _resolve_agent_id(name: str, db: AsyncSession) -> str:
    base = _slugify_agent_id(name)
    candidate = base
    coll = await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == candidate))
    if coll.scalar_one_or_none() is None:
        return candidate
    initial = name.strip()[0].lower() if name.strip() else "x"
    candidate = f"{initial}{base}"
    coll = await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == candidate))
    if coll.scalar_one_or_none() is None:
        return candidate
    # Last resort: numeric suffix
    for i in range(2, 20):
        candidate = f"{base}{i}"
        coll = await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == candidate)
        )
        if coll.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError(f"Could not generate unique agent_id for {name!r}")


def _synthetic_orcid(name: str, affiliations: list[str]) -> str:
    seed = name + "|" + "|".join(affiliations)
    digest = hashlib.sha1(seed.encode()).hexdigest()[:8].upper()
    return f"SPARSE-{digest}"


async def _validate_orcid_name(orcid: str, expected_name: str) -> tuple[bool, str]:
    """Returns (matches, fetched_name). Network call best-effort."""
    try:
        prof = await fetch_orcid_profile(orcid)
    except Exception as exc:
        logger.warning("ORCID lookup failed for %s: %s", orcid, exc)
        return False, ""
    fetched = prof.get("name", "")
    score = _jaccard(_name_tokens(expected_name), _name_tokens(fetched))
    return score >= NAME_MATCH_TOKEN_MIN, fetched


# ---------------------------------------------------------------------------
# PubMed search + disambiguation
# ---------------------------------------------------------------------------

async def _ncbi_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> httpx.Response:
    settings = get_settings()
    if settings.ncbi_api_key:
        params = {**params, "api_key": settings.ncbi_api_key}
    resp = await client.get(f"{EUTILS}/{path}", params=params)
    resp.raise_for_status()
    await asyncio.sleep(0.12)  # respect ~8 req/s with key
    return resp


def _build_pubmed_query(name: str, affiliations: list[str]) -> str:
    """Build a PubMed esearch term. Uses full first name when available
    ("Liu David[Author]") so PubMed's own indexing pre-filters the results
    set, leaving fewer wrong-author candidates for the local disambiguator.
    Falls back to "Liu D[Author]" if first name is initial-only.
    """
    parts = name.strip().split()
    if not parts:
        return ""
    last = parts[-1]
    first = parts[0] if len(parts) > 1 else ""
    if first and len(first.replace(".", "")) > 1:
        # Full first name available — use it for tighter PubMed-side match
        author_term = f"{last} {first}[Author]"
    elif first:
        # Initial-only first name
        author_term = f"{last} {first[0]}[Author]"
    else:
        author_term = f"{last}[Author]"
    if affiliations:
        aff_terms = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations[:2])
        return f"({author_term}) AND ({aff_terms})"
    return author_term


async def _esearch_pmids(client: httpx.AsyncClient, query: str) -> list[str]:
    if not query:
        return []
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": PUBMED_FETCH_CAP,
        "sort": "pub date",
    }
    try:
        resp = await _ncbi_get(client, "esearch.fcgi", params)
        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])
    except Exception as exc:
        logger.error("PubMed ESearch failed (%s): %s", query, exc)
        return []


def _author_affiliations_from_xml(xml_text: str, pmid: str) -> list[tuple[str, list[str]]]:
    """Return [(author_last, [aff_strings])] for the article with this PMID."""
    out: list[tuple[str, list[str]]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None or pmid_el.text != pmid:
            continue
        for author in article.findall(".//Author"):
            last_el = author.find("LastName")
            if last_el is None or not last_el.text:
                continue
            affs = [
                (aff.text or "").strip()
                for aff in author.findall(".//AffiliationInfo/Affiliation")
                if aff.text
            ]
            out.append((last_el.text.strip(), affs))
        break
    return out


def _author_first_name_matches(
    fore_name: str | None,
    initials: str | None,
    expected_first: str,
) -> bool:
    """True if author's ForeName/Initials match the input first name.

    Match priority (strict → permissive):
      1. ForeName starts with input first name (e.g., "David R" matches "David")
         — strong match
      2. ForeName is initial-only (single letter or "DR" style) and matches first
         initial — weaker accept (PubMed sometimes lacks ForeName)
      3. Initials element starts with first initial — fallback

    Crucially, when ForeName IS present but DOES NOT start with the input first
    name, we REJECT. This stops "Liu D[Author]" from matching Daniel/Dan Liu
    papers when we want David Liu.
    """
    if not expected_first:
        return False
    expected_first = expected_first.strip()
    expected_initial = expected_first[0].lower() if expected_first else ""
    fore = (fore_name or "").strip()
    inits = (initials or "").strip()

    if fore:
        fore_lower = fore.lower()
        # Long ForeName: must match the full input first name
        if len(fore.replace(".", "").replace(" ", "")) > 1:
            return fore_lower.startswith(expected_first.lower())
        # Short ForeName (single letter): fall through to initial check
        if fore_lower.startswith(expected_initial):
            return True
        return False
    # No ForeName at all — fall back to Initials
    if inits and inits[0].lower() == expected_initial:
        return True
    return False


async def _disambiguate(
    pmids: list[str],
    name: str,
    affiliations: list[str],
) -> tuple[list[str], int]:
    """Return (pmids_that_match, n_discarded). Match requires:
      1. surname match on at least one author
      2. that author's first initial matches the input name's first initial
      3. that author's affiliation matches one of the input affiliations

    The first-initial requirement (added 2026-06-03) is critical for common
    surnames like Chen/Liu where surname + affiliation alone matched many
    distinct researchers.
    """
    if not pmids:
        return [], 0
    settings = get_settings()
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
    }
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(f"{EUTILS}/efetch.fcgi", params=params)
    resp.raise_for_status()
    xml_text = resp.text

    parts = name.strip().split()
    last_name = parts[-1].lower() if parts else ""
    first_name = parts[0] if len(parts) > 1 else ""

    kept: list[str] = []
    discarded = 0
    sample_rejected: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("Failed to parse disambiguation XML: %s", exc)
        return [], len(pmids)
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pmid = pmid_el.text
        matched = False
        rejected_affs: list[str] = []
        for author in article.findall(".//Author"):
            last_el = author.find("LastName")
            if last_el is None or not last_el.text:
                continue
            if last_el.text.strip().lower() != last_name:
                continue
            # First-name leg: require ForeName/Initials match input first name
            if first_name:
                fore_el = author.find("ForeName")
                init_el = author.find("Initials")
                fore = fore_el.text if fore_el is not None else None
                inits = init_el.text if init_el is not None else None
                if not _author_first_name_matches(fore, inits, first_name):
                    continue
            affs = [
                (a.text or "")
                for a in author.findall(".//AffiliationInfo/Affiliation")
                if a.text
            ]
            for aff in affs:
                if any(_aff_match(needle, aff) for needle in affiliations):
                    matched = True
                    break
                rejected_affs.append(aff[:120])
            if matched:
                break
        if matched:
            kept.append(pmid)
        else:
            discarded += 1
            if rejected_affs and len(sample_rejected) < 3:
                sample_rejected.append(f"PMID {pmid}: {rejected_affs[0]}")
    if not kept and sample_rejected:
        logger.info(
            "Disambiguation kept 0 of %d for %s; sample rejected affiliations:\n  %s",
            len(pmids), name, "\n  ".join(sample_rejected),
        )
    return kept, discarded


# ---------------------------------------------------------------------------
# Faculty page fetch
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


async def _fetch_faculty_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "copi-sparse/1.0"})
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Faculty page fetch failed (%s): %s", url, exc)
        return ""
    # Strip scripts/styles
    text = re.sub(r"<script\b.*?</script>", " ", resp.text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:FACULTY_PAGE_MAX_CHARS]


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

def _build_context(
    name: str,
    affiliations: list[str],
    faculty_text: str,
    publications: list[dict[str, Any]],
) -> str:
    parts = [
        "## Researcher",
        f"- Name: {name}",
        f"- Affiliations: {' | '.join(affiliations)}",
    ]
    if faculty_text:
        parts.extend(["", "## Faculty/Lab Page", faculty_text])
    if publications:
        parts.extend(["", "## Publications"])
        sorted_pubs = sorted(publications, key=lambda p: p.get("year") or 0, reverse=True)[:25]
        for pub in sorted_pubs:
            parts.append(
                f"\n### {pub.get('title', '(no title)')} "
                f"({pub.get('journal') or 'unknown journal'}, {pub.get('year') or 'n.d.'})"
            )
            if pub.get("abstract"):
                parts.append(f"Abstract: {pub['abstract'][:1500]}")
    return "\n".join(parts)


def _load_sparse_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Sparse prompt missing at %s; using minimal fallback", PROMPT_PATH)
        return (
            "Synthesize a researcher profile JSON from sparse inputs. Output only "
            "valid JSON with keys research_summary, techniques, experimental_models, "
            "disease_areas, key_targets, keywords. Do not hallucinate."
        )


def _synthesize(context_text: str, name: str) -> dict[str, Any]:
    settings = get_settings()
    system_prompt = _load_sparse_prompt()
    user_message = (
        f"Please synthesize a researcher profile for {name} from the following "
        f"information:\n\n{context_text}\n\nReturn JSON only."
    )
    client = get_anthropic_client()
    message = client.messages.create(
        model=settings.llm_profile_model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = message.content[0].text
    try:
        return _extract_json(response_text)
    except ValueError:
        logger.error("Sparse synthesis JSON parse failed for %s; raw:\n%s", name, response_text)
        raise


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _existing_user(
    db: AsyncSession,
    name: str,
    orcid: str | None,
) -> User | None:
    """Find an existing user matching by ORCID first (most reliable) then name.

    Institution is not used as a filter — DB rows often have NULL institution
    or a shortened form that won't string-match the TSV's full institution.
    """
    if orcid:
        result = await db.execute(select(User).where(User.orcid == orcid))
        row = result.scalars().first()
        if row:
            return row
    result = await db.execute(select(User).where(User.name == name))
    return result.scalars().first()


async def _persist(
    db: AsyncSession,
    row: InputRow,
    institution: str,
    orcid: str,
    placeholder_orcid: bool,
    synthesized: dict[str, Any],
    publications: list[dict[str, Any]],
    audit: AuditRow,
    existing_user: User | None = None,
) -> None:
    """Upsert path: when `existing_user` is provided (force re-run), update
    that row's institution + replace publications + bump ResearcherProfile
    in place. Otherwise insert new User + Profile + AgentRegistry.
    """
    audit.placeholder_orcid = placeholder_orcid

    if existing_user is not None:
        # Update path
        user = existing_user
        if institution and not user.institution:
            user.institution = institution
        # Wipe existing publications — we're replacing with the fresh set.
        from sqlalchemy import delete
        await db.execute(delete(Publication).where(Publication.user_id == user.id))
        await db.flush()
    else:
        # Insert path
        user = User(
            orcid=orcid,
            name=row.name,
            institution=institution,
            access_status="allowed",
        )
        db.add(user)
        await db.flush()
    audit.institution = user.institution

    # Publications (fresh insert in both paths)
    seen_pmids: set[str] = set()
    for pub in publications:
        pmid = pub.get("pmid")
        if not pmid or pmid in seen_pmids:
            continue
        seen_pmids.add(pmid)
        # DOIs here come from each PMID's own PubMed record, so they already
        # match the PMID; normalize to strip prefix/format junk before storing.
        db.add(Publication(
            user_id=user.id,
            pmid=pmid,
            doi=normalize_doi(pub.get("doi")),
            pmcid=pub.get("pmcid"),
            title=pub.get("title", ""),
            abstract=pub.get("abstract", ""),
            journal=pub.get("journal"),
            year=pub.get("year"),
        ))
    await db.flush()

    # ResearcherProfile (upsert)
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile is None:
        profile = ResearcherProfile(user_id=user.id, profile_version=0)
        db.add(profile)
    profile.research_summary = synthesized.get("research_summary", "")
    profile.techniques = synthesized.get("techniques", [])
    profile.experimental_models = synthesized.get("experimental_models", [])
    profile.disease_areas = synthesized.get("disease_areas", [])
    profile.key_targets = synthesized.get("key_targets", [])
    profile.keywords = synthesized.get("keywords", [])
    profile.profile_version = (profile.profile_version or 0) + 1
    profile.profile_generated_at = datetime.now(timezone.utc)
    await db.flush()

    # AgentRegistry (upsert)
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        agent_id = await _resolve_agent_id(row.name, db)
        last_name = row.name.strip().split()[-1]
        last_alpha = "".join(c for c in last_name if c.isalpha())
        if agent_id == _slugify_agent_id(row.name):
            bot_name = f"{last_alpha.capitalize()}Bot"
        else:
            bot_name = f"{agent_id[0].upper()}{last_alpha.capitalize()}Bot"
        agent = AgentRegistry(
            agent_id=agent_id,
            user_id=user.id,
            bot_name=bot_name,
            pi_name=row.name,
            status="pending",
        )
        db.add(agent)
        await db.flush()
    agent_id = agent.agent_id
    audit.agent_id = agent_id

    # Refresh user with publications for export
    pubs_result = await db.execute(select(Publication).where(Publication.user_id == user.id))
    user_pubs = pubs_result.scalars().all()
    export_profile_to_markdown(user, profile, agent_id, publications=user_pubs)

    # Private profile seed is intentionally NOT synthesized for generated
    # profiles. The private seed encodes behavioral preferences (collaboration
    # style, topic priorities) that should come from the PI via the onboarding
    # flow — not be invented by an LLM. Left NULL so the agent uses defaults
    # until the PI authors their own.

    audit.persisted = True


# ---------------------------------------------------------------------------
# Per-row pipeline
# ---------------------------------------------------------------------------

async def _process_row(row: InputRow, db: AsyncSession, force: bool) -> AuditRow:
    audit = AuditRow(name=row.name)
    institution = row.affiliations[0] if row.affiliations else None

    existing = await _existing_user(db, row.name, row.orcid)
    if existing and not force:
        audit.reasons.append(
            f"skipped: user already exists ({existing.orcid}) — use --force to override"
        )
        return audit
    if existing and force:
        audit.reasons.append(
            f"force-update: replacing publications + profile for existing user ({existing.orcid})"
        )

    # 1. Identity
    if row.orcid:
        ok, fetched = await _validate_orcid_name(row.orcid, row.name)
        if not ok:
            audit.reasons.append(
                f"orcid_name_mismatch: ORCID {row.orcid} returned {fetched!r}"
            )
            # Continue with synthetic to avoid attaching wrong publications to an
            # unverified ORCID. Real ORCID can be backfilled by the PI on first login.
            orcid = _synthetic_orcid(row.name, row.affiliations)
            placeholder = True
        else:
            orcid = row.orcid
            placeholder = False
    else:
        orcid = _synthetic_orcid(row.name, row.affiliations)
        placeholder = True

    # 2. Publication discovery — prefer ORCID-curated works when a real ORCID
    # is present. PI-curated ORCID lists avoid the same-name same-institution
    # disambiguation problem (which is severe for Liu/Chen/Wang/Ding at major
    # research universities). PubMed name+affiliation search is the fallback.
    orcid_pmids: list[str] = []
    if not placeholder:
        try:
            orcid_works = await fetch_orcid_works(orcid)
        except Exception as exc:
            logger.warning("ORCID works fetch failed for %s: %s", row.name, exc)
            orcid_works = []
        orcid_pmids = [w["pmid"] for w in orcid_works if w.get("pmid")]
        doi_only = [w["doi"] for w in orcid_works if w.get("doi") and not w.get("pmid")]
        if doi_only:
            try:
                doi_map = await convert_dois_to_pmids(doi_only)
                orcid_pmids.extend(doi_map.values())
            except Exception as exc:
                logger.warning("DOI→PMID for %s failed: %s", row.name, exc)
        # Dedup
        orcid_pmids = list(dict.fromkeys(orcid_pmids))[:PUBMED_FETCH_CAP]
        if orcid_pmids:
            logger.info("ORCID-curated works for %s: %d papers", row.name, len(orcid_pmids))

        # Staleness gate: a populated-but-ancient ORCID must NOT short-circuit the
        # recency-focused PubMed search. If the newest ORCID work predates the
        # cutoff, discard the (old) ORCID PMIDs and fall through to a pub-date-
        # sorted name+affiliation PubMed search, where the "last 25" filter applies.
        orcid_years = [w["year"] for w in orcid_works if w.get("year")]
        orcid_newest_year = max(orcid_years) if orcid_years else None
        cutoff_year = datetime.now(timezone.utc).year - ORCID_STALE_AFTER_YEARS
        if orcid_pmids and orcid_newest_year is not None and orcid_newest_year < cutoff_year:
            logger.info(
                "ORCID for %s is STALE (newest work %d < cutoff %d) — discarding "
                "%d ORCID PMIDs, falling back to PubMed search",
                row.name, orcid_newest_year, cutoff_year, len(orcid_pmids),
            )
            audit.reasons.append(f"orcid_stale_newest_{orcid_newest_year}")
            orcid_pmids = []

    # If ORCID gave us enough (and is not stale), use it directly and skip the
    # affiliation search. Otherwise, supplement with name+affiliation PubMed search.
    if len(orcid_pmids) >= EVIDENCE_FLOOR_PAPERS:
        kept_pmids = orcid_pmids
        audit.n_pubmed_hits = len(orcid_pmids)
    else:
        query = _build_pubmed_query(row.name, row.affiliations)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            aff_pmids = await _esearch_pmids(client, query)
        audit.n_pubmed_hits = len(aff_pmids)
        kept_pmids, _ = await _disambiguate(aff_pmids, row.name, row.affiliations)
        # Merge in any ORCID-curated PMIDs we did find (they bypass disambig)
        if orcid_pmids:
            merged = list(dict.fromkeys(orcid_pmids + kept_pmids))[:PUBMED_FETCH_CAP]
            logger.info(
                "Combining for %s: %d ORCID + %d aff-search = %d unique",
                row.name, len(orcid_pmids), len(kept_pmids), len(merged),
            )
            kept_pmids = merged
    audit.n_papers_kept = len(kept_pmids)

    # 3. Faculty page (optional)
    faculty_text = ""
    if row.faculty_url:
        faculty_text = await _fetch_faculty_page(row.faculty_url)
        audit.faculty_page_chars = len(faculty_text)

    # 4. Evidence floor
    if audit.n_papers_kept < EVIDENCE_FLOOR_PAPERS and not faculty_text:
        audit.reasons.append(
            f"below_floor: {audit.n_papers_kept} papers, no faculty page "
            f"(need ≥{EVIDENCE_FLOOR_PAPERS} papers or a page)"
        )
        return audit

    # 5. Fetch full publication records for the kept set
    pubmed_records: list[dict[str, Any]] = []
    if kept_pmids:
        pubmed_records = await fetch_pubmed_records(kept_pmids)

    # 6. LLM synthesis
    context_text = _build_context(row.name, row.affiliations, faculty_text, pubmed_records)
    try:
        synthesized = _synthesize(context_text, row.name)
    except Exception as exc:
        audit.reasons.append(f"synthesis_failed: {exc}")
        return audit

    # 7. Persist
    inst = institution or ""
    await _persist(
        db, row, inst, orcid, placeholder, synthesized, pubmed_records, audit,
        existing_user=existing if force else None,
    )
    return audit


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_NULL_TOKENS = {"", "null", "none", "n/a", "na", "-"}


def _cell(raw: list[str], idx: int) -> str | None:
    if idx >= len(raw):
        return None
    val = raw[idx].strip()
    if val.lower() in _NULL_TOKENS:
        return None
    return val


def _parse_tsv(path: Path) -> list[InputRow]:
    rows: list[InputRow] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        for raw in reader:
            if not raw or not raw[0].strip() or raw[0].startswith("#"):
                continue
            name = raw[0].strip()
            orcid = _cell(raw, 1)
            affs_raw = _cell(raw, 2) or ""
            affs = [a.strip() for a in affs_raw.split("|") if a.strip()]
            faculty = _cell(raw, 3)
            rows.append(InputRow(name=name, orcid=orcid, affiliations=affs, faculty_url=faculty))
    return rows


def _write_audit_csv(path: Path, audits: list[AuditRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name", "agent_id", "institution", "placeholder_orcid",
            "n_pubmed_hits", "n_papers_kept", "faculty_page_chars",
            "persisted", "reasons",
        ])
        for a in audits:
            w.writerow([
                a.name, a.agent_id or "", a.institution or "",
                "1" if a.placeholder_orcid else "0",
                a.n_pubmed_hits, a.n_papers_kept, a.faculty_page_chars,
                "1" if a.persisted else "0",
                "; ".join(a.reasons),
            ])


def _print_pilot_labs_snippet(audits: list[AuditRow]) -> None:
    persisted = [a for a in audits if a.persisted and a.agent_id]
    if not persisted:
        return
    print("\n# Paste into src/agent/simulation.py PILOT_LABS:")
    for a in persisted:
        last = a.name.strip().split()[-1]
        last_alpha = re.sub(r"[^A-Za-z]", "", last)
        if a.agent_id.endswith(last.lower()):
            bot = f"{last_alpha.capitalize()}Bot"
        else:
            # Collision-prefixed agent_id (e.g. pwu / PWuBot)
            bot = f"{a.agent_id[0].upper()}{last_alpha.capitalize()}Bot"
        print(f'    {{"id": "{a.agent_id}", "name": "{bot}", "pi": "{a.name}"}},')
    print("\n# And add a SLACK_BOT_TOKEN_<AGENT_ID> for each to .env once Slack apps exist.")


async def _run(tsv_path: Path, force: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    input_rows = _parse_tsv(tsv_path)
    logger.info("Parsed %d input rows from %s", len(input_rows), tsv_path)

    audits: list[AuditRow] = []
    async with factory() as db:
        for row in input_rows:
            logger.info("--- Processing: %s ---", row.name)
            try:
                audit = await _process_row(row, db, force=force)
                await db.commit()
            except Exception as exc:
                logger.exception("Row failed: %s", row.name)
                await db.rollback()
                audit = AuditRow(name=row.name, reasons=[f"exception: {exc}"])
            audits.append(audit)

    await engine.dispose()

    # Audit CSV
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    audit_path = Path("scripts") / f"_sparse_run_{ts}.csv"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _write_audit_csv(audit_path, audits)
    logger.info("Audit CSV written to %s", audit_path)

    # Summary
    persisted = sum(1 for a in audits if a.persisted)
    skipped = len(audits) - persisted
    logger.info("Persisted: %d, skipped/failed: %d, total: %d", persisted, skipped, len(audits))

    _print_pilot_labs_snippet(audits)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Path to TSV (inside the container)")
    parser.add_argument("--force", action="store_true",
                        help="Process rows even if a User with the same name already exists")
    args = parser.parse_args()

    tsv_path = Path(args.file)
    if not tsv_path.exists():
        logger.error("TSV not found: %s", tsv_path)
        sys.exit(1)

    sys.exit(asyncio.run(_run(tsv_path, force=args.force)))


if __name__ == "__main__":
    main()
