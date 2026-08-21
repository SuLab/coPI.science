"""Reachability gate: nothing in this repo asserted that routes, templates and
imports are actually *reachable*, and four live defects grew in that blind spot.
All four are now repaired, so the ``KNOWN_*`` suppression sets below are empty and
this file has no strict xfails left: every finding is a real failure.

What "reachable" means here, precisely:

  * A **template** is reachable if a handler in ``src/`` renders it by name, or if a
    reachable template ``extends``/``include``s/``import``s it. Reachability is
    transitive, which is the whole point: a link inside an orphaned template must not
    launder the route it points at into "referenced".
  * A **route** is reachable if a *reachable* template links to it (``href``/``action``/
    ``location.href``/``fetch``), or a string in ``src/`` names it (redirect target,
    email link, Slack message), or it is on ``ROUTE_ALLOWLIST`` with a reason.
  * An **import** is live if it resolves. Every ``from src.* import name`` in ``src/``
    is checked, plus every import nested in a ``try``. A lazy import inside a function
    body is never exercised until that branch runs, and when the branch is wrapped in
    ``except Exception: pass`` a stale symbol is invisible forever.

Design notes that keep this gate from crying wolf (a noisy gate gets deleted):

  * Two matchers, deliberately asymmetric. ``_link_can_reach`` is permissive — a Jinja
    expression is allowed to stand in for a literal route segment — so we never call a
    working link broken. ``_link_credits`` is strict — a Jinja expression only fills a
    ``{path_param}`` slot — so a route only counts as referenced by a link that really
    addresses it. Being permissive in one direction and strict in the other trades
    false positives (which get the gate deleted) for false negatives (which just leave
    a future orphan for the next reader).
  * Broken links are only reported for *reachable* templates. Deleting one orphaned
    template would otherwise cascade into a pile of derived findings.
  * ``ROUTE_ALLOWLIST`` entries carry a written reason and are themselves gated:
    ``test_route_allowlist_has_no_stale_entries`` fails if an allowlisted route becomes
    referenced. A stale suppression is the same bug wearing a disguise.

Known false negative, recorded because it is not hypothetical. This gate is static: a
link counts as a credit if it appears in a reachable template, and nothing here
evaluates the Jinja condition the link sits under. A control behind a branch that never
holds is therefore invisible to it. There is a live instance:
``POST /onboarding/retry`` (src/routers/onboarding.py:317) has exactly one control in
the app — the "Try Again" form at templates/onboarding/profile_review.html:53 — and it
sits inside ``{% elif job_status == 'failed' %}``. ``job_status`` is
``Job.status``, and src/worker/main.py only ever writes 'processing', 'completed',
'dead' or 'pending'; ``'failed'`` is permitted by the enum (src/models/job.py:23) and
assigned by nothing in src/. So the retry button is unreachable at runtime while this
gate reports the route as referenced. Closing it would mean evaluating template
conditions against the values src/ can actually produce — a different and much larger
analysis, and one that would cry wolf. Left as a false negative on purpose (the same
trade recorded above: false negatives leave a future orphan, false positives get the
gate deleted), but recorded so the next reader does not mistake this gate's silence for
proof that every control is live.
  * The ``KNOWN_*`` sets are the escape hatch, and they are empty. While a defect was
    live its entry was subtracted from the aggregate assertions (so those stayed green
    and still failed loudly on a *new* orphan) and it carried a paired
    ``xfail(strict=True)`` test asserting the defect was fixed — which is what turned
    this file red the moment it *was* fixed, forcing the entry out. That mechanism is
    still wired up (``test_every_known_defect_entry_is_paired_with_a_strict_xfail``)
    and is the only sanctioned way to record a finding you are not fixing today. It
    was chosen over plain characterization asserts: an equality assert on today's
    broken value passes forever and never notices the repair.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import src
from src.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_DIR = REPO_ROOT / "static"

# Stand-in for "a Jinja expression / f-string hole was here". A NUL byte cannot occur
# in a URL or a template, so it can never collide with real content.
HOLE = "\x00"


# ---------------------------------------------------------------------------
# Known-live defects: none. All four are repaired, so every set here is empty and the
# aggregate gates below subtract nothing.
#
# Do NOT add to these to silence a new finding — fix the finding. If you genuinely
# cannot fix it today, an entry is subtracted from its aggregate gate and MUST come
# with a paired xfail(strict=True) test asserting the defect is fixed, so the repair
# turns this file red and forces the entry back out. That pairing is enforced by
# test_every_known_defect_entry_is_paired_with_a_strict_xfail (and you will need to
# re-add `import pytest`, dropped when the last defect test went).
# ---------------------------------------------------------------------------

KNOWN_ORPHAN_TEMPLATES: set[str] = set()

KNOWN_UNREACHABLE_ROUTES: set[tuple[str, str]] = set()

KNOWN_BROKEN_LINKS: set[tuple[str, str, str]] = set()

KNOWN_DEAD_IMPORTS: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Allowlist: routes that are legitimately referenced by neither a template nor src/.
# Every entry needs a reason naming the real caller. Kept honest by
# test_route_allowlist_has_no_stale_entries.
# ---------------------------------------------------------------------------

ROUTE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("GET", "/docs"): "FastAPI-generated Swagger UI; entered by typing the URL.",
    ("GET", "/docs/oauth2-redirect"): "FastAPI-generated; used by Swagger UI's own JS.",
    ("GET", "/redoc"): "FastAPI-generated ReDoc UI; entered by typing the URL.",
    ("GET", "/openapi.json"): "FastAPI-generated schema; fetched by /docs and /redoc JS.",
    # /api/health is no longer allowlisted here (issue #25 P1, badge-middleware
    # short-circuit): AgentBadgeMiddleware.dispatch now compares request.url.path
    # against the literal string "/api/health", which makes src_referenced_paths()
    # pick it up as src/-referenced — genuinely so, not a false positive.
    ("GET", "/admin"): (
        "Bare-URL alias an admin types by hand: src/routers/admin.py stacks "
        "@router.get('') and @router.get('/users') on the same admin_users handler, and "
        "the nav links the /admin/... children rather than the bare prefix. Nothing to "
        "reach — deleting it would only break typed URLs and bookmarks."
    ),
    ("GET", "/auth/callback"): (
        "ORCID OAuth redirect_uri — the caller is orcid.org. The value we register with "
        "ORCID is settings.orcid_redirect_uri (src/config.py), not a link in this app."
    ),
    ("GET", "/cabo-graph"): (
        "Public collaboration-graph page shared by URL (retreat handout / email), "
        "deliberately unlinked from the nav. nginx/nginx.conf:111 whitelists it "
        "alongside the other three graph URLs, which is the external caller."
    ),
    ("GET", "/scripps-graph"): (
        "Same as /cabo-graph: hand-shared public graph URL, whitelisted in "
        "nginx/nginx.conf:111, intentionally not in the nav."
    ),
    ("GET", "/schultz-alumni-pilot"): (
        "Same as /cabo-graph: hand-shared public graph URL for the Schultz alumni "
        "pilot cohort, whitelisted in nginx/nginx.conf:111."
    ),
    ("GET", "/schultz-group-alumni"): (
        "Same as /cabo-graph: hand-shared public graph URL for the Schultz group "
        "alumni cohort, whitelisted in nginx/nginx.conf:111."
    ),
}

# Optional third-party imports that are allowed to be absent at test time. Empty
# today: every `try:`-guarded import in src/ resolves in the app image.
GUARDED_IMPORT_ALLOWLIST: dict[str, str] = {}

# Targets of any template reference we cannot resolve statically (a dynamic
# `{% include some_var %}`). Empty today — test_no_dynamic_template_references keeps it
# that way, because a dynamic include is a hole the orphan-template gate cannot see
# through. If one is genuinely needed, list its possible targets here.
DYNAMIC_TEMPLATE_TARGETS: set[str] = set()


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    name: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


def _walk_routes(routes, prefix: str = "") -> list[Route]:
    """Flatten the assembled app's route table into (method, full path) pairs.

    FastAPI >= 0.140 stores an included router as a ``_IncludedRouter`` wrapper that
    keeps the original router plus its mount prefix, so ``app.routes`` is a tree, not a
    list. Older versions nest via ``.routes``. Both shapes are handled; ``Mount``s
    (``/static``) are recorded as opaque prefixes rather than descended into.
    """
    out: list[Route] = []
    for route in routes:
        ctx = getattr(route, "include_context", None)
        if ctx is not None:
            out += _walk_routes(ctx.included_router.routes, prefix + (ctx.prefix or ""))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        sub = getattr(route, "routes", None)
        if sub is not None and type(route).__name__ == "Mount":
            out.append(Route("MOUNT", prefix + path, getattr(route, "name", "") or ""))
            continue
        if sub is not None:
            out += _walk_routes(sub, prefix + path)
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            out.append(Route(method, prefix + path, getattr(route, "name", "") or ""))
    return out


@functools.lru_cache(maxsize=1)
def route_table() -> tuple[Route, ...]:
    return tuple(_walk_routes(create_app().routes))


@functools.lru_cache(maxsize=1)
def http_routes() -> tuple[Route, ...]:
    """Routes a browser can address. HEAD is dropped: Starlette adds it alongside GET,
    so treating it separately would double every finding."""
    return tuple(r for r in route_table() if r.method not in {"MOUNT", "HEAD"})


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    stripped = path.strip("/")
    return stripped.split("/") if stripped else []


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _link_can_reach(link_path: str, route_path: str) -> bool:
    """Permissive: could this link ever hit this route? Used to decide whether a link
    is broken, so a Jinja expression is allowed to render into a literal segment."""
    link_segs, route_segs = _segments(link_path), _segments(route_path)
    if len(link_segs) != len(route_segs):
        return False
    for link_seg, route_seg in zip(link_segs, route_segs, strict=True):
        if _is_param(route_seg) or link_seg == route_seg or HOLE in link_seg:
            continue
        return False
    return True


def _link_credits(link_path: str, route_path: str) -> bool:
    """Strict: does this link actually address this route? Used to decide whether a
    route is referenced, so a Jinja expression may only fill a ``{path_param}``."""
    link_segs, route_segs = _segments(link_path), _segments(route_path)
    if len(link_segs) != len(route_segs):
        return False
    for link_seg, route_seg in zip(link_segs, route_segs, strict=True):
        if _is_param(route_seg):
            continue
        if link_seg == route_seg:
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Template graph
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def template_names() -> tuple[str, ...]:
    """Every file under templates/, named the way Jinja2Templates(directory=...) does."""
    return tuple(
        sorted(
            p.relative_to(TEMPLATES_DIR).as_posix()
            for p in TEMPLATES_DIR.rglob("*")
            if p.is_file()
        )
    )


# {% extends "x" %} / {% include "x" %} / {% import "x" as y %} / {% from "x" import y %}
_TEMPLATE_REF_RE = re.compile(
    r"{%-?\s*(extends|include|import|from)\s+(?P<rest>.+?)\s*-?%}", re.S
)
_QUOTED_RE = re.compile(r"""(?:"([^"]*)"|'([^']*)')""")


@functools.lru_cache(maxsize=1)
def template_to_template_refs() -> dict[str, frozenset[str]]:
    """parent template -> templates it pulls in (literal names only)."""
    refs: dict[str, frozenset[str]] = {}
    for name in template_names():
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8", errors="replace")
        found: set[str] = set()
        for m in _TEMPLATE_REF_RE.finditer(text):
            rest = m.group("rest")
            # `{% from "x" import y %}` — only the leading quoted token is the template.
            head = rest.split(" import ")[0] if m.group(1) == "from" else rest
            found.update(q for pair in _QUOTED_RE.findall(head) for q in pair if q)
        refs[name] = frozenset(found)
    return refs


@functools.lru_cache(maxsize=1)
def dynamic_template_refs() -> tuple[tuple[str, int, str], ...]:
    """(template, line, directive) for every include/extends whose target is not a
    quoted literal — a hole this gate cannot see through."""
    out: list[tuple[str, int, str]] = []
    for name in template_names():
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8", errors="replace")
        for m in _TEMPLATE_REF_RE.finditer(text):
            rest = m.group("rest")
            head = rest.split(" import ")[0] if m.group(1) == "from" else rest
            if not _QUOTED_RE.search(head):
                out.append((name, text[: m.start()].count("\n") + 1, m.group(0).strip()))
    return tuple(out)


# ---------------------------------------------------------------------------
# src/ scan: rendered template names, path-ish strings, imports
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _src_files() -> tuple[Path, ...]:
    return tuple(sorted(p for p in SRC_DIR.rglob("*.py") if "__pycache__" not in p.parts))


def _flatten_fstring(node: ast.AST) -> str | None:
    """Reconstruct a str constant or f-string, with HOLE for each interpolation.
    Returns None for anything that is not a string expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(HOLE)
        return "".join(parts)
    return None


_HTTP_DECORATORS = {
    "get", "post", "put", "patch", "delete", "head", "options", "trace",
    "api_route", "route", "websocket",
}


def _excluded_string_nodes(tree: ast.AST) -> set[int]:
    """Strings that mention a path without *calling* it, and must not credit a route.

    Two kinds, and both matter:

      * A route decorator's own path (``@router.get("/auth/callback")``). Routers
        mounted without a prefix declare their full path there, so without this every
        such route would credit itself and the unreachable-route gate would be blind
        to exactly half the app. (Prefixed routers accidentally escape that, which is
        the only reason the /onboarding orphans were visible at all.)
      * A router's own mount ``prefix=``. ``include_router(admin.router,
        prefix="/admin")`` is the definition of ``GET /admin``, not a link to it.
      * Docstrings. "``/login, /auth/callback, /logout``" in a module docstring is
        documentation, not a caller.
    """
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "prefix":
                    excluded.add(id(kw.value))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in _HTTP_DECORATORS
                    and dec.args
                ):
                    excluded.add(id(dec.args[0]))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                excluded.add(id(body[0].value))
    return excluded


@functools.lru_cache(maxsize=1)
def src_strings() -> tuple[tuple[str, str], ...]:
    """(file, reconstructed string) for every string literal / f-string in src/, minus
    route-decorator paths and docstrings (see _excluded_string_nodes)."""
    out: list[tuple[str, str]] = []
    for path in _src_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        excluded = _excluded_string_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Constant, ast.JoinedStr)) and id(node) not in excluded:
                text = _flatten_fstring(node)
                if text is not None:
                    out.append((rel, text))
    return tuple(out)


@functools.lru_cache(maxsize=1)
def templates_rendered_by_src() -> frozenset[str]:
    known = set(template_names())
    return frozenset(text for _, text in src_strings() if text in known)


@functools.lru_cache(maxsize=1)
def dynamic_template_names_in_src() -> tuple[tuple[str, str], ...]:
    """f-strings in src/ that look like a computed template name — another blind spot."""
    return tuple(
        (rel, text)
        for rel, text in src_strings()
        if HOLE in text and text.endswith(".html")
    )


_PATH_TOKEN_SPLIT = re.compile(r"""[\s"'`<>()\[\]{},;|\\]+""")


def _candidate_paths(text: str) -> set[str]:
    """Pull every URL path out of an arbitrary string (redirect target, email body,
    Slack message). Query and fragment are dropped; ``{}`` are kept only when they came
    from an f-string hole, which ``_flatten_fstring`` already turned into HOLE."""
    out: set[str] = set()
    for token in _PATH_TOKEN_SPLIT.split(text):
        if "/" not in token:
            continue
        candidate = token[token.index("/") :]
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]
        if not candidate.startswith("/") or candidate == "/":
            out.add("/") if candidate == "/" else None
            continue
        out.add(candidate.rstrip("/") or "/")
    if text.strip() == "/":
        out.add("/")
    return out


@functools.lru_cache(maxsize=1)
def src_referenced_paths() -> frozenset[str]:
    """Paths named by any non-docstring, non-decorator string in src/: redirect targets,
    email links, Slack message bodies."""
    out: set[str] = set()
    for _, text in src_strings():
        out |= _candidate_paths(text)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Template links
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    template: str
    method: str
    raw: str
    path: str  # normalized: query/fragment stripped, Jinja expressions -> HOLE
    # False for a URL found in JavaScript, where the verb lives in an options object we
    # do not parse. Such a link still *credits* a route (any verb), but is never used to
    # call a link broken — we would only be guessing at the method.
    method_known: bool = True


_FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.I | re.S)
_ANCHOR_ATTR_RE = re.compile(r"""\b(?:href)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ACTION_ATTR_RE = re.compile(r"""\baction\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_METHOD_ATTR_RE = re.compile(r"""\bmethod\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# Client-side navigation: always a GET.
_JS_NAV_RE = re.compile(
    r"""(?:location\.href\s*=|location\.assign\(|location\.replace\(|window\.open\()\s*"""
    r"""(?:"([^"]*)"|'([^']*)')""",
    re.I,
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)
# `"/a/" + id + "/b"` -> `"/a/\x00/b"`, so a URL assembled by concatenation still
# resolves to a path pattern. Without this, every JS-called API with an id in the middle
# would look unreachable.
_JS_CONCAT_RE = re.compile(r"""["']\s*\+\s*[^+"'()]{1,60}\+\s*["']""")
_JS_STRING_RE = re.compile(r"""(?:"([^"\n]*)"|'([^'\n]*)')""")
_JINJA_EXPR_RE = re.compile(r"{{.*?}}", re.S)
_JINJA_TAG_RE = re.compile(r"{%.*?%}", re.S)

_EXTERNAL_PREFIXES = ("http://", "https://", "//", "mailto:", "tel:", "javascript:", "data:")


def _normalize_link(raw: str) -> list[str]:
    """Turn one attribute value into zero or more candidate route paths.

    A value may hold several: ``{% if x %}/a{% else %}/b{% endif %}`` really is two
    links, and both should resolve. Statement tags split the value into chunks; each
    chunk is then classified. Anything we cannot pin to a local path (an external URL,
    a bare ``{{ var }}``, an anchor, a same-page ``?query=`` link) yields nothing —
    those are counted as unresolvable rather than guessed at.
    """
    out: list[str] = []
    for chunk in _JINJA_TAG_RE.split(raw):
        value = chunk.strip()
        if not value or value.startswith(("#", "?")):
            continue
        if value.lower().startswith(_EXTERNAL_PREFIXES):
            continue
        value = value.split("#", 1)[0].split("?", 1)[0]
        value = _JINJA_EXPR_RE.sub(HOLE, value).strip()
        if not value.startswith("/"):
            continue
        if value.startswith("/static/"):
            continue
        out.append(value.rstrip("/") or "/")
    return out


@functools.lru_cache(maxsize=1)
def template_links() -> tuple[Link, ...]:
    """Every navigable target in every template, with the HTTP method it will use."""
    links: list[Link] = []
    for name in template_names():
        if not name.endswith((".html", ".htm", ".jinja", ".j2")):
            continue
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8", errors="replace")

        # Forms first, so their action= is attributed to the declared method.
        form_spans: list[tuple[int, int]] = []
        for tag in _FORM_TAG_RE.finditer(text):
            form_spans.append(tag.span())
            action = _ACTION_ATTR_RE.search(tag.group(0))
            if action is None:
                continue  # no action -> submits to the current URL
            method_m = _METHOD_ATTR_RE.search(tag.group(0))
            method = (method_m.group(1) or method_m.group(2)).upper() if method_m else "GET"
            raw = action.group(1) if action.group(1) is not None else action.group(2)
            for path in _normalize_link(raw):
                links.append(Link(name, method, raw, path))

        for m in _ANCHOR_ATTR_RE.finditer(text):
            if any(start <= m.start() < end for start, end in form_spans):
                continue  # href inside a <form ...> tag is not a navigation target
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            for path in _normalize_link(raw):
                links.append(Link(name, "GET", raw, path))

        for m in _JS_NAV_RE.finditer(text):
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            for path in _normalize_link(raw):
                links.append(Link(name, "GET", raw, path))

        # Any local-looking path in a <script> block: an API the page's JS calls. The
        # verb is unknown, so these only ever credit a route.
        for block in _SCRIPT_BLOCK_RE.finditer(text):
            body = _JS_CONCAT_RE.sub(HOLE, block.group(1))
            for m in _JS_STRING_RE.finditer(body):
                raw = m.group(1) if m.group(1) is not None else m.group(2)
                for path in _normalize_link(raw):
                    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        links.append(Link(name, method, raw, path, method_known=False))
    return tuple(links)


@functools.lru_cache(maxsize=1)
def static_js_paths() -> frozenset[str]:
    """Local paths named by shipped JavaScript under static/ — same blind spot as an
    inline <script>, same treatment."""
    out: set[str] = set()
    if not STATIC_DIR.exists():
        return frozenset()
    for path in STATIC_DIR.rglob("*.js"):
        body = _JS_CONCAT_RE.sub(HOLE, path.read_text(encoding="utf-8", errors="replace"))
        for m in _JS_STRING_RE.finditer(body):
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            out.update(_normalize_link(raw))
    return frozenset(out)


@functools.lru_cache(maxsize=1)
def link_attr_values() -> tuple[tuple[str, str], ...]:
    """(template, raw value) for every href/action, resolvable or not — the denominator
    for the "what fraction can we check" number."""
    out: list[tuple[str, str]] = []
    attr_re = re.compile(r"""\b(?:href|action)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
    for name in template_names():
        if not name.endswith((".html", ".htm", ".jinja", ".j2")):
            continue
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8", errors="replace")
        for m in attr_re.finditer(text):
            out.append((name, m.group(1) if m.group(1) is not None else m.group(2)))
    return tuple(out)


# ---------------------------------------------------------------------------
# Reachability closure
# ---------------------------------------------------------------------------


def compute_reachable_templates(
    rendered_by_src: frozenset[str],
    refs: dict[str, frozenset[str]],
    extra_roots: set[str] | None = None,
) -> set[str]:
    """Transitive closure from the templates src/ renders, over extends/include."""
    reachable = set(rendered_by_src) | set(extra_roots or ())
    frontier = list(reachable)
    while frontier:
        current = frontier.pop()
        for child in refs.get(current, ()):  # include/extends/import targets
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)
    return reachable


@functools.lru_cache(maxsize=1)
def reachable_templates() -> frozenset[str]:
    return frozenset(
        compute_reachable_templates(
            templates_rendered_by_src(),
            template_to_template_refs(),
            extra_roots=set(DYNAMIC_TEMPLATE_TARGETS),
        )
    )


def compute_orphan_templates(all_names, reachable) -> set[str]:
    return {
        name
        for name in all_names
        if name not in reachable and name.endswith((".html", ".htm", ".jinja", ".j2"))
    }


def compute_broken_links(links, routes, reachable) -> set[tuple[str, str, str]]:
    """Links in *reachable* templates whose (method, path) hits no route."""
    broken = set()
    for link in links:
        if link.template not in reachable or not link.method_known:
            continue
        if any(
            link.method == r.method and _link_can_reach(link.path, r.path)
            for r in routes
        ):
            continue
        broken.add((link.template, link.method, link.path))
    return broken


def compute_stale_allowlist_entries(
    routes, links, reachable, src_paths, allowlist
) -> dict[tuple[str, str], str]:
    """Allowlist entries that no longer suppress anything: the route was deleted, or a
    real caller appeared. Either way the entry must go, or it will keep hiding the next
    orphan that lands on the same path."""
    unreachable_without_allowlist = compute_unreachable_routes(
        routes, links, reachable, src_paths, allowlist={}
    )
    registered = {r.key for r in routes}
    stale: dict[tuple[str, str], str] = {}
    for key in allowlist:
        if key not in registered:
            stale[key] = "route no longer exists; delete the entry"
        elif key not in unreachable_without_allowlist:
            stale[key] = "now referenced by a template or src/; delete the entry"
    return stale


def compute_unreachable_routes(
    routes, links, reachable, src_paths, allowlist
) -> set[tuple[str, str]]:
    """Routes credited by no reachable template, no src/ string, and no allowlist."""
    live_links = [link for link in links if link.template in reachable]
    unreachable = set()
    for route in routes:
        if route.key in allowlist:
            continue
        if any(
            link.method == route.method and _link_credits(link.path, route.path)
            for link in live_links
        ):
            continue
        if any(_link_credits(path, route.path) for path in src_paths):
            continue
        unreachable.add(route.key)
    return unreachable


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportSite:
    file: str
    line: int
    source: str
    module: str
    names: tuple[str, ...]
    in_try: bool


def _resolve_relative(path: Path, level: int, module: str | None) -> str:
    """Turn `from ..x import y` into an absolute dotted module name.

    ``level`` is 1 for ``from .x``, 2 for ``from ..x``. Level 1 is relative to the
    file's own package, so a file at src/services/foo.py has package ``src.services``.
    """
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    package = parts[:-1] if parts else []
    base = package[: len(package) - (level - 1)] if level > 1 else package
    return ".".join([*base, *([module] if module else [])])


@functools.lru_cache(maxsize=1)
def import_sites() -> tuple[ImportSite, ...]:
    """Every `from ... import ...` / `import ...` in src/, flagged if nested in a try."""
    sites: list[ImportSite] = []
    for path in _src_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    for sub in ast.walk(stmt):
                        guarded.add(id(sub))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = (
                    _resolve_relative(path, node.level, node.module)
                    if node.level
                    else (node.module or "")
                )
                sites.append(
                    ImportSite(
                        rel,
                        node.lineno,
                        ast.unparse(node),
                        module,
                        tuple(a.name for a in node.names),
                        id(node) in guarded,
                    )
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    sites.append(
                        ImportSite(
                            rel,
                            node.lineno,
                            ast.unparse(node),
                            alias.name,
                            (),
                            id(node) in guarded,
                        )
                    )
    return tuple(sites)


def resolve_import(site: ImportSite) -> str | None:
    """None if the import resolves, else a human-readable reason."""
    try:
        module = importlib.import_module(site.module)
    except Exception as exc:  # ImportError, or anything raised at module import time
        return f"cannot import {site.module!r}: {type(exc).__name__}: {exc}"
    for name in site.names:
        if name == "*" or hasattr(module, name):
            continue
        try:
            importlib.import_module(f"{site.module}.{name}")
        except Exception:
            return f"{site.module!r} has no attribute {name!r}"
    return None


def compute_dead_imports(sites, allowlist) -> set[tuple[str, str, str]]:
    """(file, source, reason) for first-party or try-guarded imports that don't resolve."""
    dead = set()
    for site in sites:
        first_party = site.module == "src" or site.module.startswith("src.")
        if not first_party and not site.in_try:
            continue  # a plain third-party import failing would break collection anyway
        if not first_party and site.module.split(".")[0] in allowlist:
            continue
        reason = resolve_import(site)
        if reason:
            dead.add((site.file, site.source, reason))
    return dead


# ===========================================================================
# Tests
# ===========================================================================


def test_gate_analyzes_the_repo_checkout_not_an_installed_copy():
    """The app image carries a stale `src` in site-packages; if `import src` picked that
    up, every finding below would describe code nobody edits. Fail loudly instead."""
    imported = Path(src.__file__).resolve().parent
    assert imported == SRC_DIR, (
        f"`import src` resolved to {imported}, not the checkout at {SRC_DIR}. "
        "Run pytest from the repo root so the working tree wins over site-packages."
    )


def test_route_table_is_fully_enumerated():
    """A route-table walker that silently misses a nested router would make every
    'unreachable route' finding meaningless. Cross-check against the OpenAPI schema,
    which FastAPI builds by its own traversal."""
    app = create_app()
    from_openapi = set(app.openapi()["paths"])
    from_walk = {r.path for r in http_routes()}
    missing = from_openapi - from_walk
    assert not missing, f"walker missed routes that OpenAPI knows about: {sorted(missing)}"
    assert len(http_routes()) > 50, f"suspiciously few routes: {len(http_routes())}"


def test_no_dynamic_template_references():
    """`{% include some_var %}` is a hole the orphan gate cannot see through. If one is
    added, list its possible targets in DYNAMIC_TEMPLATE_TARGETS."""
    assert not dynamic_template_refs(), (
        "template pulled in by a computed name — the orphan-template gate cannot "
        "follow it. Add its possible targets to DYNAMIC_TEMPLATE_TARGETS:\n"
        + "\n".join(f"  {t}:{ln}  {d}" for t, ln, d in dynamic_template_refs())
    )
    assert not dynamic_template_names_in_src(), (
        "src/ renders a computed template name; the gate cannot follow it:\n"
        + "\n".join(f"  {rel}  {text!r}" for rel, text in dynamic_template_names_in_src())
    )


def test_no_unreferenced_templates():
    """Every template is rendered by a handler or pulled in by a reachable template."""
    orphans = compute_orphan_templates(template_names(), reachable_templates())
    assert orphans - KNOWN_ORPHAN_TEMPLATES == set(), (
        "template referenced by no route and no other template:\n"
        + "\n".join(f"  templates/{n}" for n in sorted(orphans - KNOWN_ORPHAN_TEMPLATES))
    )


def test_template_links_resolve_to_a_real_route():
    """Every href/action in a reachable template hits a registered (method, path)."""
    broken = compute_broken_links(template_links(), http_routes(), reachable_templates())
    assert broken - KNOWN_BROKEN_LINKS == set(), (
        "link/form action resolving to no route:\n"
        + "\n".join(
            f"  templates/{t}: {m} {p}" for t, m, p in sorted(broken - KNOWN_BROKEN_LINKS)
        )
    )


def test_no_unreachable_routes():
    """Every registered route is addressed by a reachable template, a src/ string, or
    an allowlist entry with a reason."""
    unreachable = compute_unreachable_routes(
        http_routes(),
        template_links(),
        reachable_templates(),
        src_referenced_paths() | static_js_paths(),
        ROUTE_ALLOWLIST,
    )
    assert unreachable - KNOWN_UNREACHABLE_ROUTES == set(), (
        "route referenced by no template and no src/ string. If a caller exists that "
        "this gate cannot see (JS, an OAuth callback, an email link), add it to "
        "ROUTE_ALLOWLIST with the reason:\n"
        + "\n".join(
            f"  {m} {p}" for m, p in sorted(unreachable - KNOWN_UNREACHABLE_ROUTES)
        )
    )


def test_route_allowlist_has_no_stale_entries():
    """A suppression that is no longer needed is the same bug in a different place."""
    stale = compute_stale_allowlist_entries(
        http_routes(),
        template_links(),
        reachable_templates(),
        src_referenced_paths() | static_js_paths(),
        ROUTE_ALLOWLIST,
    )
    assert not stale, "ROUTE_ALLOWLIST entries that are no longer needed:\n" + "\n".join(
        f"  {m} {p} — {why}" for (m, p), why in sorted(stale.items())
    )


def test_every_allowlist_entry_has_a_reason():
    for key, reason in ROUTE_ALLOWLIST.items():
        assert reason and len(reason) > 20, f"{key} needs a real reason, got {reason!r}"


def test_every_known_defect_entry_is_paired_with_a_strict_xfail():
    """The ``KNOWN_*`` sets are suppressions: each subtracts a finding from an aggregate
    gate. The docstring says "Do NOT add to this list to silence a new finding", and
    until now nothing enforced it — a sixth entry would have gone in silently and the
    gate would have gone quiet with it.

    What makes a ``KNOWN_*`` entry legitimate is the paired ``xfail(strict=True)`` test:
    that is what turns the file red the moment the defect is repaired, forcing the entry
    out. So the invariant is a one-to-one count. Adding an entry without a paired
    defect test fails here; deleting a defect test but leaving its entry behind fails
    here too.

    Deliberately a count and not a name-matching scheme: a mapping keyed on test names
    would itself need maintaining, and the thing worth protecting is that the two never
    drift apart in size.
    """
    entries = (
        [("KNOWN_ORPHAN_TEMPLATES", e) for e in KNOWN_ORPHAN_TEMPLATES]
        + [("KNOWN_UNREACHABLE_ROUTES", e) for e in KNOWN_UNREACHABLE_ROUTES]
        + [("KNOWN_BROKEN_LINKS", e) for e in KNOWN_BROKEN_LINKS]
        + [("KNOWN_DEAD_IMPORTS", e) for e in KNOWN_DEAD_IMPORTS]
    )
    defect_tests = []
    for name, obj in sorted(globals().items()):
        if not (name.startswith("test_defect_") and callable(obj)):
            continue
        marks = [m for m in getattr(obj, "pytestmark", []) if m.name == "xfail"]
        assert marks, f"{name} must carry an xfail marker pinning the live defect"
        for m in marks:
            assert m.kwargs.get("strict") is True, (
                f"{name} carries a NON-STRICT xfail. A non-strict xfail rots silently: "
                "it keeps reporting 'expected failure' after the defect is fixed, so the "
                "KNOWN_* entry it justifies never gets removed."
            )
            reason = m.kwargs.get("reason") or ""
            assert len(reason) > 40, f"{name}'s xfail needs a reason naming the defect"
        defect_tests.append(name)

    assert len(entries) == len(defect_tests), (
        f"{len(entries)} KNOWN_* suppression(s) but {len(defect_tests)} strict-xfail "
        "defect test(s). Every suppression needs one, or it is an unexplained "
        f"suppression.\n  entries: {sorted(entries)}\n  tests:   {defect_tests}"
    )


def test_guarded_and_first_party_imports_resolve():
    """The highest-value check here. A `from src... import x` inside
    `try: ... except Exception: pass` that no longer resolves is invisible forever —
    the feature is simply gone, silently."""
    dead = compute_dead_imports(import_sites(), GUARDED_IMPORT_ALLOWLIST)
    known = {(f, s) for f, s in KNOWN_DEAD_IMPORTS}
    remaining = {(f, s, r) for f, s, r in dead if (f, s) not in known}
    assert remaining == set(), "import that does not resolve:\n" + "\n".join(
        f"  {f}: {s}\n      {r}" for f, s, r in sorted(remaining)
    )


def test_import_gate_actually_checked_the_lazy_imports():
    """Guard against the gate quietly checking nothing (e.g. an ast change that stops
    finding nested imports)."""
    sites = import_sites()
    guarded = [s for s in sites if s.in_try]
    first_party = [s for s in sites if s.module.startswith("src")]
    assert len(guarded) > 30, f"only {len(guarded)} try-guarded imports found"
    assert len(first_party) > 150, f"only {len(first_party)} first-party imports found"


def test_static_link_resolution_coverage_is_reported():
    """We cannot resolve a URL built entirely from a variable. Pin how much we *can*
    check so a future change that guts the matcher shows up as a coverage drop."""
    values = link_attr_values()
    resolvable = [v for _, v in values if _normalize_link(v)]
    assert len(values) > 100, f"only {len(values)} href/action attrs found"
    fraction = len(resolvable) / len(values)
    assert fraction >= 0.80, (
        f"only {len(resolvable)}/{len(values)} ({fraction:.0%}) of href/action values "
        "resolve to a checkable local path — the matcher probably regressed"
    )


# ---------------------------------------------------------------------------
# Teeth. The detectors are pure functions of collected data, so we can feed them
# synthetic trees and prove each finds a fresh orphan — without touching a repo file.
# ---------------------------------------------------------------------------


def test_teeth_orphan_template_detector_catches_a_new_orphan():
    names = ("base.html", "landing.html", "admin/_partial.html", "ghost/leftover.html")
    refs = {"landing.html": frozenset({"base.html", "admin/_partial.html"})}
    reachable = compute_reachable_templates(frozenset({"landing.html"}), refs)
    assert compute_orphan_templates(names, reachable) == {"ghost/leftover.html"}


def test_teeth_orphan_detector_is_transitive_not_just_one_hop():
    """A template reachable only through two includes must not be flagged, and one
    reachable only *from* an orphan must be."""
    names = ("a.html", "b.html", "c.html", "x.html", "y.html")
    refs = {
        "a.html": frozenset({"b.html"}),
        "b.html": frozenset({"c.html"}),
        "x.html": frozenset({"y.html"}),  # x is an orphan, so y is unreachable too
    }
    reachable = compute_reachable_templates(frozenset({"a.html"}), refs)
    assert compute_orphan_templates(names, reachable) == {"x.html", "y.html"}


def test_teeth_broken_link_detector_catches_a_form_pointing_at_nothing():
    routes = (Route("POST", "/profile/save", "profile_save"),)
    links = (
        Link("profile/edit.html", "POST", "/profile/save", "/profile/save"),
        Link("profile/edit.html", "POST", "/profile/vanished", "/profile/vanished"),
    )
    broken = compute_broken_links(links, routes, frozenset({"profile/edit.html"}))
    assert broken == {("profile/edit.html", "POST", "/profile/vanished")}


def test_teeth_broken_link_detector_catches_a_method_mismatch():
    """A form POSTing to a GET-only route 405s; path equality alone would miss it."""
    routes = (Route("GET", "/admin/discussions", "admin_discussions"),)
    links = (Link("admin/discussions.html", "POST", "/admin/discussions", "/admin/discussions"),)
    broken = compute_broken_links(links, routes, frozenset({"admin/discussions.html"}))
    assert broken == {("admin/discussions.html", "POST", "/admin/discussions")}


def test_teeth_unreachable_route_detector_catches_a_new_orphan_route():
    routes = (
        Route("GET", "/agent/{agent_id}/dashboard", "agent_dashboard"),
        Route("GET", "/agent/{agent_id}/ghost", "agent_ghost"),
    )
    links = (
        Link(
            "agent/listing.html",
            "GET",
            "/agent/{{ a.agent_id }}/dashboard",
            f"/agent/{HOLE}/dashboard",
        ),
    )
    unreachable = compute_unreachable_routes(
        routes, links, frozenset({"agent/listing.html"}), src_paths=frozenset(), allowlist={}
    )
    assert unreachable == {("GET", "/agent/{agent_id}/ghost")}


def test_teeth_a_link_inside_an_orphaned_template_does_not_launder_a_route():
    """The exact shape of the defect this gate was built for: the only caller of a route
    sits in a template nothing renders. A non-transitive gate would call the route
    reachable. Kept synthetic on purpose — the real instance (add_texts.html's Skip
    button, the sole caller of POST /onboarding/complete) has since been deleted, and
    this is what would catch the next one."""
    routes = (Route("POST", "/onboarding/complete", "complete_onboarding"),)
    links = (
        Link(
            "onboarding/add_texts.html", "POST", "/onboarding/complete", "/onboarding/complete"
        ),
    )
    reachable: frozenset[str] = frozenset()  # add_texts.html is rendered by nothing
    unreachable = compute_unreachable_routes(
        routes, links, reachable, src_paths=frozenset(), allowlist={}
    )
    assert unreachable == {("POST", "/onboarding/complete")}


def test_teeth_dead_import_detector_catches_a_vanished_symbol():
    live = ImportSite(
        "src/fake.py", 1, "from src.main import create_app", "src.main", ("create_app",), True
    )
    dead = ImportSite(
        "src/fake.py",
        2,
        "from src.main import _never_existed",
        "src.main",
        ("_never_existed",),
        True,
    )
    missing_module = ImportSite(
        "src/fake.py", 3, "from src.no_such_mod import x", "src.no_such_mod", ("x",), True
    )
    found = compute_dead_imports((live, dead, missing_module), {})
    assert {(f, s) for f, s, _ in found} == {
        ("src/fake.py", "from src.main import _never_existed"),
        ("src/fake.py", "from src.no_such_mod import x"),
    }


def test_teeth_path_param_routes_match_by_pattern_not_string_equality():
    assert _link_can_reach(f"/agent/{HOLE}/dashboard", "/agent/{agent_id}/dashboard")
    assert _link_credits(f"/agent/{HOLE}/dashboard", "/agent/{agent_id}/dashboard")
    # A literal in the link must still match a literal in the route.
    assert not _link_credits("/agent/smith/ghost", "/agent/{agent_id}/dashboard")
    # Strict matcher must not credit a sibling literal route via a Jinja expression:
    # /admin/cohorts/{{ c.id }} does not address /admin/cohorts/topology.
    assert not _link_credits(f"/admin/cohorts/{HOLE}", "/admin/cohorts/topology")
    # ...but the permissive matcher tolerates it, so we never cry "broken link".
    assert _link_can_reach(f"/admin/cohorts/{HOLE}", "/admin/cohorts/topology")
    # Segment count must match.
    assert not _link_can_reach("/agent/smith", "/agent/{agent_id}/dashboard")


def test_teeth_stale_allowlist_detector_catches_both_ways_an_entry_goes_stale():
    routes = (
        Route("GET", "/kept", "kept"),
        Route("GET", "/now-linked", "now_linked"),
    )
    links = (Link("page.html", "GET", "/now-linked", "/now-linked"),)
    allowlist = {
        ("GET", "/kept"): "still unreferenced — legitimately entered by hand",
        ("GET", "/now-linked"): "was unreferenced when this was written",
        ("GET", "/deleted-route"): "route has since been removed",
    }
    stale = compute_stale_allowlist_entries(
        routes, links, frozenset({"page.html"}), frozenset(), allowlist
    )
    assert set(stale) == {("GET", "/now-linked"), ("GET", "/deleted-route")}
    assert "now referenced" in stale[("GET", "/now-linked")]
    assert "no longer exists" in stale[("GET", "/deleted-route")]


def test_teeth_a_route_does_not_credit_itself_via_its_own_decorator():
    """Routers mounted without a prefix declare their full path in the decorator. If
    that literal counted as a reference, an orphan in public.py/auth.py/invite.py would
    be permanently invisible — half the app."""
    source = (
        '"""Module docstring mentioning /docstring-only."""\n'
        "app.include_router(r, prefix='/prefix-only')\n"
        "@router.get('/decorator-only')\n"
        "async def handler():\n"
        '    """Docstring mentioning /nested-docstring-only."""\n'
        "    return RedirectResponse(url='/a-real-redirect')\n"
    )
    tree = ast.parse(source)
    excluded = _excluded_string_nodes(tree)
    kept = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in excluded
    }
    assert "/a-real-redirect" in kept
    for suppressed in ("/decorator-only", "/prefix-only"):
        assert suppressed not in kept, f"{suppressed} would credit its own route"
    assert not any("docstring-only" in k for k in kept), "docstrings are not callers"


def test_teeth_normalizer_handles_the_jinja_shapes_in_this_repo():
    # url-with-query
    assert _normalize_link("/admin/discussions?run_id={{ x }}") == ["/admin/discussions"]
    # branching value yields both arms
    assert _normalize_link(
        "{% if r %}/agent/{{ r.agent_id }}/profile/edit{% else %}/agent{% endif %}"
    ) == [f"/agent/{HOLE}/profile/edit", "/agent"]
    # unresolvable: whole URL comes from a variable
    assert _normalize_link("{{ slack_invite_url }}") == []
    # external, anchor, same-page query, static asset
    assert _normalize_link("https://orcid.org/{{ u.orcid }}") == []
    assert _normalize_link("#top") == []
    assert _normalize_link("?page={{ page + 1 }}") == []
    assert _normalize_link("/static/js/markdown.js") == []
