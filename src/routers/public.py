"""Public-facing routes: landing page, waitlist, access-pending."""

import asyncio
import json
import logging
import re
import secrets
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import VOTE_DOWN, VOTE_UP, ProposalVote, User, WaitlistSignup
from src.services.rate_limit import SlidingWindowRateLimiter, client_ip
from src.services.validators import is_valid_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Per-IP throttle for the anonymous proposal-feedback endpoints (defense in
# depth behind the nginx edge limits). Generous enough for a human clicking
# through the graph, tight enough to blunt scripted vote-spam (SEC-7).
_vote_limiter = SlidingWindowRateLimiter(max_events=30, window_seconds=60)

# Per-IP throttle for the public waitlist form. A real signup happens once;
# this caps scripted row-spam on the unauthenticated endpoint (SEC-17).
_waitlist_limiter = SlidingWindowRateLimiter(max_events=10, window_seconds=3600)

# Field caps for the public waitlist write, applied before persisting so
# oversized input is truncated rather than raising a DB DataError -> 500
# (name/institution are String(255); note is unbounded Text) (SEC-17).
_WAITLIST_NAME_MAX = 255
_WAITLIST_INSTITUTION_MAX = 255
_WAITLIST_NOTE_MAX = 2000


def _graph_csp(nonce: str) -> str:
    """Content-Security-Policy for the standalone collaboration-graph pages.

    These templates carry an inline <script> plus inline styles and load
    D3/marked/DOMPurify/Tailwind from CDNs, so the policy pins those hosts and
    requires a per-request nonce for the inline executable script (defense in
    depth behind the |tojson/escapeHtml output encoding). 'unsafe-eval' is
    present only because the Tailwind Play CDN JIT-compiles utilities in the
    browser; vendoring a compiled Tailwind build (SEC-18) lets it be dropped.
    """
    return "; ".join(
        [
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com",
            (
                f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval' "
                "https://cdn.tailwindcss.com https://d3js.org https://cdn.jsdelivr.net"
            ),
        ]
    )


def _render_graph(request: Request, context: dict) -> HTMLResponse:
    """Render cabo_graph.html with a fresh CSP nonce and the matching header.

    ``graph_json``/``color_map`` in ``context`` must be raw Python objects — the
    template serializes them with the |tojson filter, which escapes ``<``/``>``/
    ``&`` so attacker-controlled fields (PI name, institution, LLM summary text)
    cannot break out of the <script> block.
    """
    nonce = secrets.token_urlsafe(16)
    context["csp_nonce"] = nonce
    response = templates.TemplateResponse(request, "cabo_graph.html", context)
    response.headers["Content-Security-Policy"] = _graph_csp(nonce)
    return response

# Institution mapping for the Cabo collaboration graph. This is an independent
# hardcoded grouping of agent_ids by institution (the agent roster itself now
# lives in the AgentRegistry table).
_SCRIPPS = {
    # Active Cabo cohort
    "su", "wiseman", "grotjahn", "ward", "briney", "forli", "lairson",
    "badran", "kern", "lasker", "lippi", "maillie", "millar", "miller",
    "mravic", "paulson", "pwu", "seiple", "williamson", "wilson",
    "young",  # Calibr-Skaggs is part of Scripps
    # Scripps investigators not at the Cabo meeting (suspended/pending)
    "cravatt", "petrascheck", "lotz", "racki", "ken", "deniz", "saez", "wu",
    "macrae", "williams",
    "schultz",  # Peter Schultz — Scripps Research (Schultz alumni reunion host)
}
_UCSF = {
    "sali", "larabell", "zaro", "roe", "santi", "wells", "echeverria",
    "fraser", "craik", "stroud", "minor", "manglik", "susa", "capra",
}
_OTHER_INST = {
    "kim": "Stanford",
    "azumaya": "Genentech",
    "nomura": "UC Berkeley",
}

# Cohort cutover for the Cabo retreat graph: matches commit 0ef4741
# (the Cabo retreat roster reshape). All proposals to date share a single
# simulation_run_id, so date is the only way to isolate the new cohort.
CABO_COHORT_START = datetime(2026, 3, 1, tzinfo=timezone.utc)

# Schultz alumni pilot = the PIs seeded from newuserlist01.tsv + newuserlist02.tsv
# (formerly "cohort 001"). These are matched by ORCID (the identifier the lists
# are keyed on) rather than agent_id, since agent_id collision-prefixing
# (cliu/liu, schen/chen, ckim/kim, wliu/wu, achatterjee/chatterjee) makes
# hand-deriving IDs fragile. The last three entries of newuserlist02.tsv had a
# null ORCID in the TSV; their stored identifiers were resolved from the
# agents/users tables.
SCHULTZ_PILOT_ORCIDS = frozenset({
    # newuserlist01.tsv
    "0000-0001-9649-1892",  # Chan Hyuk Kim
    "0000-0003-2585-8268",  # Angad Mehta
    "0000-0001-5171-7982",  # Arnab Chatterjee
    "0000-0002-6294-5187",  # Shuibing Chen
    "0000-0002-3290-2880",  # Chang Liu
    "0000-0001-7456-2557",  # Priscilla Li-ning Yang
    "0000-0001-6701-996X",  # Luke Lairson
    "0000-0001-5661-1714",  # Linda Hsieh-Wilson
    "0000-0002-4311-971X",  # Han Xiao
    "0000-0001-6469-795X",  # Andrea Cochran
    "0000-0002-6231-5302",  # Abhishek Chatterjee
    "0000-0001-8590-7741",  # Kevan Shokat
    "0000-0002-8277-5226",  # Alice Ting
    "0000-0002-5859-2526",  # Lei Wang
    "0000-0002-0634-0503",  # Edward Lemke
    "0000-0002-7078-6534",  # Wenshe Liu
    "0000-0001-5354-7403",  # Nathanael Gray
    "0000-0002-4233-8945",  # John Pezacki
    "0000-0002-8002-1981",  # Kai Johnsson
    "0000-0002-2057-6934",  # Dehua Pei
    "0000-0003-1636-7766",  # Nicolas Winssinger
    "0000-0001-8973-493X",  # David Corey
    "0000-0001-9320-5512",  # Jonathan Ellman
    "0000-0001-9309-6141",  # Costas Lyssiotis
    "0000-0002-9859-4104",  # Andrew Su
    # newuserlist02.tsv
    "0000-0002-9943-7557",  # David Liu
    "0000-0003-1219-4757",  # Jason Chin
    "0000-0002-5676-4718",  # Virginia Cornish
    "0000-0001-8562-5736",  # Travis Young
    "0000-0002-7813-0302",  # Christian Diercks
    "0000-0002-0402-7417",  # Peng Chen
    "0000-0001-9439-1476",  # Michael Bollong
    "0000-0003-3188-1202",  # Peter Schultz   (null ORCID in TSV; resolved from DB)
    "0000-0002-3354-1263",  # Sheng Ding      (null ORCID in TSV; resolved from DB)
    "SPARSE-1527BF6A",      # Sida Shao       (null ORCID in TSV; resolved from DB)
})

# Three time-scoped cohorts of the same long-running simulation. Edges are
# bounded by proposal *decided_at* (window_end exclusive). The post-creation
# join boundary (cohort_start) sits EARLIER than the decision window, because a
# thread can be opened a couple days before its proposal lands (e.g. the group
# proposals decided Jun 6 came from posts created Jun 4). Bounding posts to the
# decision window would silently drop those edges. See memory
# project_graph_cohort_windows.
#
# Cabo cohort:            Apr 27 – May  7, 2026 (inlined in the /cabo-graph route)
# Schultz alumni pilot:   Jun  1 – Jun  4, 2026
# Schultz group alumni:   Jun  5 – Jun 10, 2026
JUNE_POST_START = datetime(2026, 6, 1, tzinfo=timezone.utc)  # post boundary for both June cohorts
SCHULTZ_PILOT_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
SCHULTZ_PILOT_END = datetime(2026, 6, 5, tzinfo=timezone.utc)  # exclusive: through Jun 4
SCHULTZ_GROUP_START = datetime(2026, 6, 5, tzinfo=timezone.utc)
SCHULTZ_GROUP_END = datetime(2026, 6, 11, tzinfo=timezone.utc)  # exclusive: through Jun 10


def _institution_for(agent_id: str) -> str:
    if agent_id in _SCRIPPS:
        return "Scripps"
    if agent_id in _UCSF:
        return "UCSF"
    return _OTHER_INST.get(agent_id, "Other")


# ---------------------------------------------------------------------------
# Institution canonicalization for the cohort views.
#
# Profiles store free-text institutions ("Scripps Research", "The Scripps
# Research Institute", "UCSF Medical Center", ...). We group them so the same
# institution gets one color/legend entry. The pipeline is, per raw value:
#   1. normalize  — lowercase, strip accents/punctuation, drop a leading "The"
#                   and trailing generic org words ("Institute", "University").
#   2. alias       — map known synonyms/abbreviations/campuses to a canonical
#                    label (handles cases normalization can't, e.g. "UCSF" vs
#                    "University of California San Francisco").
#   3. fuzzy merge — cluster remaining near-identical keys (typos, spacing,
#                    word-order variants) by string similarity.
# ---------------------------------------------------------------------------

# Trailing tokens that don't distinguish institutions; stripped from the end of
# the normalized name so "Scripps Research" == "Scripps Research Institute".
_INST_TRAILING_NOISE = {
    "institute", "institution", "university", "system", "inc", "llc", "ltd",
    "corporation", "corp", "co",
}

# Canonical label -> the (free-text) variants that should collapse onto it.
# Variants are matched after normalization, so list them in any common form.
_INSTITUTION_ALIASES: dict[str, tuple[str, ...]] = {
    "UCSF": (
        "ucsf", "uc san francisco", "university of california san francisco",
        "university of california, san francisco", "ucsf medical center",
    ),
    "UC Berkeley": (
        "uc berkeley", "university of california berkeley",
        "university of california, berkeley",
    ),
    "UC San Diego": (
        "ucsd", "uc san diego", "university of california san diego",
        "university of california, san diego",
    ),
    "UC Irvine": (
        "uc irvine", "university of california irvine",
        "university of california, irvine",
    ),
    "UCLA": (
        "ucla", "uc los angeles", "university of california los angeles",
        "university of california, los angeles",
    ),
    "Scripps Research": (
        "scripps", "scripps research", "scripps research institute",
        "the scripps research institute", "tsri",
    ),
    "Caltech": ("caltech", "california institute of technology"),
    "MIT": ("mit", "massachusetts institute of technology"),
    "Stanford": ("stanford", "stanford university"),
    "Harvard": ("harvard", "harvard university", "harvard medical school"),
    "Memorial Sloan Kettering": (
        "mskcc", "memorial sloan kettering",
        "memorial sloan kettering cancer center",
    ),
}

# Fuzzy-merge threshold for normalized names (typos / spacing / word order).
_INST_FUZZY_THRESHOLD = 0.88

# Distinct, high-contrast palette for per-institution coloring.
_INSTITUTION_PALETTE = [
    "#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B2",
    "#937860", "#DA8BC3", "#CCB974", "#64B5CD", "#1F77B4",
    "#FF7F0E", "#2CA02C", "#D62728", "#9467BD", "#8C564B",
    "#E377C2", "#BCBD22", "#17BECF", "#6366F1", "#A855F7",
    "#F59E0B", "#10B981", "#EF4444", "#3B82F6", "#EC4899",
    "#14B8A6", "#F472B6", "#84CC16",
]
_INSTITUTION_FALLBACK_COLOR = "#94a3b8"

# Reserved brand colors: these institutions always get their official color,
# regardless of node count or palette ordering. Keyed by canonical alias label
# (see ``_INSTITUTION_ALIASES``).
_INSTITUTION_BRAND_COLORS: dict[str, str] = {
    "Scripps Research": "#FFC951",
    "Stanford": "#8C1515",
    "UCSF": "#052049",
}

# Fixed Scripps/UCSF/Other scheme for the Cabo and Scripps views (which color
# by the hardcoded buckets in ``_institution_for``, not profile institutions).
# Reuses the reserved brand colors so the two views never drift apart.
_LEGACY_OTHER_COLOR = "#64748b"
_LEGACY_COLOR_MAP = {
    "Scripps": _INSTITUTION_BRAND_COLORS["Scripps Research"],
    "UCSF": _INSTITUTION_BRAND_COLORS["UCSF"],
    "Other": _LEGACY_OTHER_COLOR,
}
_LEGACY_LEGEND = [
    {"label": "Scripps Research", "color": _INSTITUTION_BRAND_COLORS["Scripps Research"]},
    {"label": "UCSF", "color": _INSTITUTION_BRAND_COLORS["UCSF"]},
    {"label": "Other", "color": _LEGACY_OTHER_COLOR},
]


def _normalize_inst(raw: str | None) -> str:
    """Lowercase, de-accent, de-punctuate, and strip generic org words."""
    if not raw or not raw.strip():
        return ""
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    if s.startswith("the "):
        s = s[4:]
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _INST_TRAILING_NOISE:
        tokens.pop()
    return " ".join(tokens)


# Normalized-variant -> canonical label, built once from the alias table.
_ALIAS_INDEX: dict[str, str] = {
    _normalize_inst(variant): label
    for label, variants in _INSTITUTION_ALIASES.items()
    for variant in variants
}

# Distinctive single-token aliases (abbreviations / unique names) -> label.
# Matched if the token appears anywhere in a name, so "The Scripps Research
# Institute (TSRI)" -> Scripps Research via the "scripps"/"tsri" tokens.
_ALIAS_SINGLE_TOKENS: dict[str, str] = {
    key: label for key, label in _ALIAS_INDEX.items() if " " not in key
}


def _alias_label(norm: str, tokens: set[str]) -> str | None:
    """Resolve a normalized institution name to a canonical alias label, via
    exact match, distinctive-token match, then fuzzy match against variants."""
    if not norm:
        return None
    if norm in _ALIAS_INDEX:
        return _ALIAS_INDEX[norm]
    for tok in tokens:
        if tok in _ALIAS_SINGLE_TOKENS:
            return _ALIAS_SINGLE_TOKENS[tok]
    best_label, best_ratio = None, 0.0
    for variant, label in _ALIAS_INDEX.items():
        ratio = SequenceMatcher(None, norm, variant).ratio()
        if ratio > best_ratio:
            best_label, best_ratio = label, ratio
    return best_label if best_ratio >= _INST_FUZZY_THRESHOLD else None


def _clean_display(raw: str) -> str:
    """Tidy a raw institution for display: drop a leading 'The' and a trailing
    corporate suffix, collapse whitespace. Preserves the distinguishing name."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = re.sub(r"^[Tt]he\s+", "", s)
    s = re.sub(r"[,\s]+(?:Inc|LLC|Ltd|Corp)\.?$", "", s).strip()
    return s


def _group_institutions(raws: list[str | None]) -> dict[str | None, str]:
    """Map each raw institution string to a canonical display label."""
    distinct = list({r for r in raws})
    norm = {r: _normalize_inst(r) for r in distinct}

    key_of_raw: dict[str | None, str] = {}
    label_of_key: dict[str, str] = {}

    # Pass 1: empties and known aliases (exact / token / fuzzy to canonical).
    for r in distinct:
        n = norm[r]
        if not n:
            key_of_raw[r] = "__unknown__"
            label_of_key["__unknown__"] = "Unknown"
            continue
        label = _alias_label(n, set(n.split()))
        if label is not None:
            key = "alias:" + label
            key_of_raw[r] = key
            label_of_key[key] = label

    # Pass 2: fuzzy-cluster the rest by normalized-string similarity.
    clusters: list[dict] = []  # {"rep": norm, "raws": [...]}
    for r in distinct:
        if r in key_of_raw:
            continue
        n = norm[r]
        best, best_ratio = None, 0.0
        for c in clusters:
            ratio = SequenceMatcher(None, n, c["rep"]).ratio()
            if ratio > best_ratio:
                best, best_ratio = c, ratio
        if best is not None and best_ratio >= _INST_FUZZY_THRESHOLD:
            best["raws"].append(r)
        else:
            clusters.append({"rep": n, "raws": [r]})

    for i, c in enumerate(clusters):
        key = f"grp:{i}"
        # Display the shortest original variant (the cleanest form, e.g.
        # "Scripps Research" over "The Scripps Research Institute").
        label = _clean_display(min(c["raws"], key=lambda x: (len(x or ""), x or "")))
        for r in c["raws"]:
            key_of_raw[r] = key
        label_of_key[key] = label or "Unknown"

    return {r: label_of_key[key_of_raw[r]] for r in distinct}


def _institution_legend(nodes: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Color nodes by institution and build a legend.

    Institutions with a single node are collapsed (in place) into a grey
    "Other" bucket so the legend stays to the meaningfully-shared institutions;
    each remaining institution gets its own palette color (count desc, "Other"
    last).
    """
    counts = Counter(n["institution"] for n in nodes)
    singletons = {label for label, count in counts.items() if count == 1}
    if singletons:
        for n in nodes:
            if n["institution"] in singletons:
                n["institution"] = "Other"
        counts = Counter(n["institution"] for n in nodes)

    shared = sorted(
        ((label, count) for label, count in counts.items() if label != "Other"),
        key=lambda kv: (-kv[1], kv[0].lower()),
    )
    # Palette colors not claimed by a reserved brand color, so brand and
    # palette institutions never collide on the same color.
    reserved = set(_INSTITUTION_BRAND_COLORS.values())
    palette = [c for c in _INSTITUTION_PALETTE if c not in reserved]
    legend, color_map = [], {}
    palette_i = 0
    for label, count in shared:
        brand = _INSTITUTION_BRAND_COLORS.get(label)
        if brand is not None:
            color = brand
        else:
            color = palette[palette_i % len(palette)]
            palette_i += 1
        color_map[label] = color
        legend.append({"label": label, "color": color, "count": count})
    if counts.get("Other"):
        color_map["Other"] = _INSTITUTION_FALLBACK_COLOR
        legend.append(
            {"label": "Other", "color": _INSTITUTION_FALLBACK_COLOR, "count": counts["Other"]}
        )
    return legend, color_map


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page. Logged-in users redirect to their profile."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return templates.TemplateResponse(request, "landing.html", {"request": request})


@router.post("/waitlist", response_class=HTMLResponse)
async def waitlist_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    institution: str = Form(""),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Accept a waitlist signup. Upserts on email."""
    if not _waitlist_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    email_clean = (email or "").strip().lower()
    if not is_valid_email(email_clean):
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "request": request,
                "waitlist_error": "Please enter a valid email address.",
                "form_values": {
                    "email": email,
                    "name": name,
                    "institution": institution,
                    "note": note,
                },
            },
            status_code=400,
        )

    # Truncate to the column limits before persisting: name/institution are
    # String(255) (oversized -> DataError -> 500) and note is unbounded Text
    # (an uncapped public write) (SEC-17).
    name_clean = name.strip()[:_WAITLIST_NAME_MAX]
    institution_clean = institution.strip()[:_WAITLIST_INSTITUTION_MAX]
    note_clean = note.strip()[:_WAITLIST_NOTE_MAX]

    result = await db.execute(
        select(WaitlistSignup).where(WaitlistSignup.email == email_clean)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.name = name_clean or existing.name
        existing.institution = institution_clean or existing.institution
        existing.note = note_clean or existing.note
    else:
        db.add(
            WaitlistSignup(
                email=email_clean,
                name=name_clean or None,
                institution=institution_clean or None,
                note=note_clean or None,
            )
        )
    await db.commit()
    logger.info("Waitlist signup: %s", email_clean)

    return templates.TemplateResponse(
        request,
        "landing.html",
        {"request": request, "waitlist_success": True},
    )


@router.get("/access-pending", response_class=HTMLResponse)
async def access_pending(request: Request):
    """Shown after ORCID login when the user is not yet approved."""
    pending_info = request.session.get("pending_access") or {}
    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
        },
    )


@router.post("/access-pending/email", response_class=HTMLResponse)
async def access_pending_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Capture an email for a pending-access user who didn't share one via ORCID."""
    pending_info = request.session.get("pending_access") or {}
    user_id = pending_info.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    email_clean = (email or "").strip().lower()
    if not is_valid_email(email_clean):
        return templates.TemplateResponse(
            request,
            "access_pending.html",
            {
                "request": request,
                "pending_info": pending_info,
                "email_error": "Please enter a valid email address.",
            },
            status_code=400,
        )

    import uuid as _uuid

    result = await db.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user and not user.email:
        user.email = email_clean
        await db.commit()
        pending_info["email"] = email_clean
        request.session["pending_access"] = pending_info

    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
            "email_saved": True,
        },
    )


async def _build_graph_payload(
    db: AsyncSession,
    *,
    scripps_only: bool = False,
    all_agents: bool = False,
    orcids: frozenset[str] | None = None,
    cohort_start: datetime = CABO_COHORT_START,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    use_profile_institution: bool = False,
    largest_component_only: bool = True,
):
    """Shared data builder for the collaboration-network views.

    Node selection (mutually exclusive):
    - ``orcids`` given: restrict to agents whose user has one of those ORCIDs
      (used by /schultz-alumni-pilot to scope to a seeded PI list).
    - ``scripps_only``: include all Scripps agents regardless of status so we
      pick up investigators who weren't at the meeting (/scripps-graph).
    - ``all_agents``: every agent regardless of status. Use for a historical
      time window where the participants are no longer the current
      ``status='active'`` roster (e.g. the Cabo retreat week vs. the later
      reunion run). The ``degree > 0`` trim below keeps only agents that
      actually appear in an in-window edge.
    - otherwise: active agents only.

    ``cohort_start`` bounds which *posts* count (their ``created_at``), keeping
    edges inside the right cohort/run.

    ``window_start`` / ``window_end`` bound when a proposal was *decided*,
    letting a caller scope to an arbitrary date range (e.g. a single retreat
    week). ``window_start`` defaults to ``cohort_start``; ``window_end`` is
    exclusive and unbounded when ``None``. The window is applied to
    ``decided_at`` only — not to post creation — so a proposal decided in the
    window still counts even if its thread was opened earlier.

    ``use_profile_institution`` colors nodes by the user's profile institution
    (fuzzy-grouped via :func:`_group_institutions`) instead of the hardcoded
    Scripps/UCSF/Other buckets.

    ``largest_component_only`` (default True) trims the result to the single
    largest connected component — sensible for a dense graph, but it hides
    isolated proposal dyads, so pass False early in a cohort when proposals are
    still disconnected pairs.
    """
    if orcids is not None:
        nodes_result = await db.execute(
            text(
                "SELECT a.agent_id, a.pi_name, a.bot_name, u.institution "
                "FROM agents a JOIN users u ON u.id = a.user_id "
                "WHERE u.orcid = ANY(:orcids) ORDER BY a.pi_name"
            ),
            {"orcids": list(orcids)},
        )
        active_rows = nodes_result.fetchall()
    elif scripps_only:
        nodes_result = await db.execute(
            text("SELECT agent_id, pi_name, bot_name FROM agents ORDER BY pi_name")
        )
        active_rows = [r for r in nodes_result.fetchall() if r.agent_id in _SCRIPPS]
    elif all_agents:
        nodes_result = await db.execute(
            text(
                "SELECT a.agent_id, a.pi_name, a.bot_name, u.institution "
                "FROM agents a LEFT JOIN users u ON u.id = a.user_id "
                "ORDER BY a.pi_name"
            )
        )
        active_rows = nodes_result.fetchall()
    else:
        nodes_result = await db.execute(
            text(
                "SELECT a.agent_id, a.pi_name, a.bot_name, u.institution "
                "FROM agents a JOIN users u ON u.id = a.user_id "
                "WHERE a.status='active' ORDER BY a.pi_name"
            )
        )
        active_rows = nodes_result.fetchall()
    active_ids = {row.agent_id for row in active_rows}

    if use_profile_institution:
        inst_map = _group_institutions([row.institution for row in active_rows])
        institution_of = lambda row: inst_map[row.institution]  # noqa: E731
    else:
        institution_of = lambda row: _institution_for(row.agent_id)  # noqa: E731

    decided_floor = window_start or cohort_start
    params = {"cohort_start": cohort_start, "decided_floor": decided_floor}
    window_end_clause = ""
    if window_end is not None:
        window_end_clause = " AND decided_at < :window_end"
        params["window_end"] = window_end

    edges_result = await db.execute(
        text(
            f"""
            WITH cohort_posts AS (
                SELECT message_ts
                FROM agent_messages
                WHERE phase = 'new_post'
                  AND created_at >= :cohort_start
                  AND message_ts IS NOT NULL
            ),
            pairs AS (
                SELECT
                    id,
                    LEAST(agent_a, agent_b)    AS a,
                    GREATEST(agent_a, agent_b) AS b,
                    thread_id,
                    decided_at,
                    summary_text
                FROM thread_decisions
                WHERE outcome = 'proposal'
                  AND origin_visibility = 'public'
                  AND decided_at >= :decided_floor{window_end_clause}
                  AND thread_id IN (SELECT message_ts FROM cohort_posts)
            ),
            -- The agent-only proposal for a thread is the FIRST one the bots
            -- reached. Any later row on the same thread is a re-proposal after a
            -- PI reopened/refined the thread, so it carries human feedback; we
            -- keep only the earliest (by decided_at) per thread.
            thread_first AS (
                SELECT DISTINCT ON (a, b, thread_id)
                    id, a, b, thread_id, decided_at, summary_text
                FROM pairs
                ORDER BY a, b, thread_id, decided_at ASC
            )
            SELECT
                a, b,
                COUNT(DISTINCT thread_id) AS n,
                MAX(decided_at)           AS last_at,
                (ARRAY_AGG(summary_text ORDER BY decided_at DESC)
                    FILTER (WHERE summary_text IS NOT NULL))[1] AS latest_summary,
                (ARRAY_AGG(id ORDER BY decided_at DESC)
                    FILTER (WHERE summary_text IS NOT NULL))[1] AS latest_decision_id,
                (ARRAY_AGG(thread_id ORDER BY decided_at DESC)
                    FILTER (WHERE summary_text IS NOT NULL))[1] AS latest_thread_id
            FROM thread_first
            GROUP BY a, b
            """
        ),
        params,
    )

    # Compute degree (unique collaborators) and total proposals per node from edges.
    degree: dict[str, int] = {row.agent_id: 0 for row in active_rows}
    total_proposals: dict[str, int] = {row.agent_id: 0 for row in active_rows}
    links: list[dict] = []
    for r in edges_result:
        if r.a not in active_ids or r.b not in active_ids:
            continue
        links.append(
            {
                "source": r.a,
                "target": r.b,
                "weight": int(r.n),
                "summary": r.latest_summary or "",
                # The exact proposal shown in the modal — votes reference it.
                "decision_id": str(r.latest_decision_id) if r.latest_decision_id else None,
                "thread_id": r.latest_thread_id,
            }
        )
        degree[r.a] += 1
        degree[r.b] += 1
        total_proposals[r.a] += int(r.n)
        total_proposals[r.b] += int(r.n)

    nodes = [
        {
            "id": row.agent_id,
            "pi": row.pi_name,
            "bot": row.bot_name,
            "institution": institution_of(row),
            "degree": degree[row.agent_id],
            "proposals": total_proposals[row.agent_id],
        }
        for row in active_rows
        if degree[row.agent_id] > 0
    ]
    if largest_component_only:
        return _largest_component(nodes, links)
    return nodes, links


def _largest_component(nodes, links):
    """Return nodes and links restricted to the largest connected component."""
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for link in links:
        union(link["source"], link["target"])

    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n["id"]), []).append(n["id"])
    if not groups:
        return nodes, links

    keep = set(max(groups.values(), key=len))
    filtered_nodes = [n for n in nodes if n["id"] in keep]
    filtered_links = [l for l in links if l["source"] in keep and l["target"] in keep]
    return filtered_nodes, filtered_links


# ---------------------------------------------------------------------------
# Graph-payload cache. The four public graph routes take only Depends(get_db)
# — no auth — and each builds its payload by sequential-scanning the two
# largest tables and running O(V^2 * L^2) institution clustering. Left uncached
# that is an unauthenticated DB-backed DoS (SEC-15). We memoize the payload per
# parameter set for a short TTL, and serialize concurrent misses under a lock
# so a burst of N requests triggers at most one DB build per TTL window (the
# per-worker complement to the nginx edge limits in nginx.conf).
# ---------------------------------------------------------------------------
_GRAPH_CACHE: dict[tuple, tuple[float, tuple]] = {}
_GRAPH_CACHE_TTL = 60.0  # seconds
_GRAPH_CACHE_LOCK = asyncio.Lock()


async def _cached_graph_payload(db: AsyncSession, **kwargs):
    """TTL-cached wrapper around :func:`_build_graph_payload`.

    The cache key is the (keyword-only, hashable) parameter set; there are only
    a handful of distinct combinations across the four routes.
    """
    key = tuple(sorted(kwargs.items()))
    now = time.monotonic()
    cached = _GRAPH_CACHE.get(key)
    if cached and now - cached[0] < _GRAPH_CACHE_TTL:
        return cached[1]
    async with _GRAPH_CACHE_LOCK:
        # Re-check under the lock: a concurrent request may have just filled it.
        cached = _GRAPH_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _GRAPH_CACHE_TTL:
            return cached[1]
        payload = await _build_graph_payload(db, **kwargs)
        _GRAPH_CACHE[key] = (time.monotonic(), payload)
        return payload


@router.get("/cabo-graph", response_class=HTMLResponse)
async def cabo_graph(request: Request, db: AsyncSession = Depends(get_db)):
    """PI collaboration network for the Cabo retreat: all active PIs.

    Scoped to proposals decided during the retreat week (Apr 27 – May 7, 2026).
    """
    nodes, links = await _cached_graph_payload(
        db,
        all_agents=True,  # Cabo roster is no longer status='active' (reunion run flipped it)
        window_start=datetime(2026, 4, 27, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 8, tzinfo=timezone.utc),  # exclusive: through May 7
    )
    return _render_graph(
        request,
        {
            "request": request,
            "graph_json": {"nodes": nodes, "links": links},
            "node_count": len(nodes),
            "edge_count": len(links),
            "proposal_count": sum(link["weight"] for link in links),
            "page_title": "Cabo collaboration network",
            "pi_label": "PIs",
            "legend": _LEGACY_LEGEND,
            "color_map": _LEGACY_COLOR_MAP,
            "since_label": "during April 27 – May 7, 2026",
        },
    )


@router.get("/scripps-graph", response_class=HTMLResponse)
async def scripps_graph(request: Request, db: AsyncSession = Depends(get_db)):
    """Scripps-only slice of the collaboration network."""
    nodes, links = await _cached_graph_payload(db, scripps_only=True)
    return _render_graph(
        request,
        {
            "request": request,
            "graph_json": {"nodes": nodes, "links": links},
            "node_count": len(nodes),
            "edge_count": len(links),
            "proposal_count": sum(link["weight"] for link in links),
            "page_title": "Scripps Research collaboration network",
            "pi_label": "Scripps PIs",
            "legend": [],
            "color_map": _LEGACY_COLOR_MAP,
            "since_label": "since March 1, 2026",
        },
    )


@router.get("/schultz-alumni-pilot", response_class=HTMLResponse)
async def schultz_alumni_pilot(request: Request, db: AsyncSession = Depends(get_db)):
    """Schultz alumni pilot collaboration network (June 1 – June 4, 2026).

    Scoped to the PIs seeded from the two pilot user lists (by ORCID), with
    edges limited to proposals decided June 1–4, 2026. Nodes are colored by
    profile institution. (Formerly /cohort001-graph.)
    """
    nodes, links = await _cached_graph_payload(
        db,
        orcids=SCHULTZ_PILOT_ORCIDS,
        cohort_start=JUNE_POST_START,
        window_start=SCHULTZ_PILOT_START,
        window_end=SCHULTZ_PILOT_END,
        use_profile_institution=True,
        largest_component_only=False,
    )
    legend, color_map = _institution_legend(nodes)
    return _render_graph(
        request,
        {
            "request": request,
            "graph_json": {"nodes": nodes, "links": links},
            "node_count": len(nodes),
            "edge_count": len(links),
            "proposal_count": sum(link["weight"] for link in links),
            "page_title": "Schultz Alumni Pilot collaboration network",
            "pi_label": "PIs",
            "legend": legend,
            "color_map": color_map,
            "since_label": "during June 1 – June 4, 2026",
        },
    )


@router.get("/schultz-group-alumni", response_class=HTMLResponse)
async def schultz_group_alumni(request: Request, db: AsyncSession = Depends(get_db)):
    """Schultz group alumni reunion collaboration network (June 5 – June 10, 2026).

    Edges limited to proposals decided June 5–10, 2026 — the discussions
    produced by the resumed reunion run. Uses ``all_agents`` (not
    ``status='active'``) so the graph stays correct as a historical window even
    after a later run flips the active roster. Nodes are colored by profile
    institution. (Formerly /schultz-alumni-graph.)
    """
    nodes, links = await _cached_graph_payload(
        db,
        all_agents=True,
        cohort_start=JUNE_POST_START,
        window_start=SCHULTZ_GROUP_START,
        window_end=SCHULTZ_GROUP_END,
        use_profile_institution=True,
        largest_component_only=False,
    )
    legend, color_map = _institution_legend(nodes)
    return _render_graph(
        request,
        {
            "request": request,
            "graph_json": {"nodes": nodes, "links": links},
            "node_count": len(nodes),
            "edge_count": len(links),
            "proposal_count": sum(link["weight"] for link in links),
            "page_title": "Schultz Group Alumni collaboration network",
            "pi_label": "PIs",
            "legend": legend,
            "color_map": color_map,
            "since_label": "during June 5 – June 10, 2026",
        },
    )


# ---------------------------------------------------------------------------
# Public proposal feedback (no login). Captures lightweight "Great idea" / "Pass"
# votes — plus optional free-text — on the proposal shown when a graph edge is
# clicked. A browser-stored ``voter_token`` lets a visitor change their vote and
# attach details to the same row; see src/models/proposal_vote.py.
# ---------------------------------------------------------------------------


class ProposalVoteIn(BaseModel):
    decision_id: uuid.UUID
    vote: str  # "up" (Great idea) | "down" (Pass)
    voter_token: str | None = None
    details: str | None = None


class ProposalVoteDetailsIn(BaseModel):
    details: str | None = None
    voter_token: str | None = None


def _clean_token(token: str | None) -> str | None:
    return (token or "").strip()[:64] or None


def _clean_details(details: str | None) -> str | None:
    return (details or "").strip()[:4000] or None


@router.post("/api/proposal-vote")
async def submit_proposal_vote(
    request: Request, payload: ProposalVoteIn, db: AsyncSession = Depends(get_db)
):
    """Record (or update) an anonymous vote on a proposal. Returns the vote id."""
    if not _vote_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    if payload.vote not in (VOTE_UP, VOTE_DOWN):
        raise HTTPException(status_code=422, detail="vote must be 'up' or 'down'")

    # Require a browser token: without one, every request inserts a fresh row
    # (the unique (decision, token) constraint can't dedup NULL tokens), which
    # is an unbounded-storage vector on this public endpoint (SEC-7).
    token = _clean_token(payload.voter_token)
    if token is None:
        raise HTTPException(status_code=422, detail="voter_token required")

    # Only allow votes on proposals that are actually shown on the public graph
    # (the same outcome/visibility predicate _build_graph_payload uses). This
    # keeps the endpoint from writing rows for — or confirming the existence of —
    # private or non-proposal thread_decisions.
    decision = (
        await db.execute(
            text(
                "SELECT thread_id, agent_a, agent_b FROM thread_decisions "
                "WHERE id = :id "
                "  AND outcome = 'proposal' "
                "  AND origin_visibility = 'public'"
            ),
            {"id": str(payload.decision_id)},
        )
    ).first()
    if decision is None:
        raise HTTPException(status_code=404, detail="unknown proposal")

    details = _clean_details(payload.details)

    # One vote per (proposal, browser token): update in place if they revote.
    existing = None
    if token:
        existing = (
            await db.execute(
                select(ProposalVote).where(
                    ProposalVote.thread_decision_id == payload.decision_id,
                    ProposalVote.voter_token == token,
                )
            )
        ).scalar_one_or_none()

    if existing is not None:
        existing.vote = payload.vote
        if details:
            existing.details = details
        vote_obj = existing
    else:
        vote_obj = ProposalVote(
            thread_decision_id=payload.decision_id,
            thread_id=decision.thread_id,
            agent_a=decision.agent_a,
            agent_b=decision.agent_b,
            vote=payload.vote,
            details=details,
            voter_token=token,
        )
        db.add(vote_obj)

    try:
        await db.commit()
    except IntegrityError:
        # Lost a race on the unique (decision, token) constraint — fetch & update.
        await db.rollback()
        vote_obj = (
            await db.execute(
                select(ProposalVote).where(
                    ProposalVote.thread_decision_id == payload.decision_id,
                    ProposalVote.voter_token == token,
                )
            )
        ).scalar_one()
        vote_obj.vote = payload.vote
        if details:
            vote_obj.details = details
        await db.commit()

    await db.refresh(vote_obj)
    return {"id": str(vote_obj.id)}


@router.post("/api/proposal-vote/{vote_id}/details")
async def update_proposal_vote_details(
    request: Request,
    vote_id: uuid.UUID,
    payload: ProposalVoteDetailsIn,
    db: AsyncSession = Depends(get_db),
):
    """Attach / update the optional free-text details on an existing vote."""
    if not _vote_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    vote_obj = (
        await db.execute(select(ProposalVote).where(ProposalVote.id == vote_id))
    ).scalar_one_or_none()
    if vote_obj is None:
        raise HTTPException(status_code=404, detail="unknown vote")

    # Light ownership check: if the row has a token, a provided one must match.
    token = _clean_token(payload.voter_token)
    if vote_obj.voter_token and token and vote_obj.voter_token != token:
        raise HTTPException(status_code=403, detail="token mismatch")

    vote_obj.details = _clean_details(payload.details)
    await db.commit()
    return {"ok": True}
