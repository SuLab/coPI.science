"""Every error code a PI-detail route can redirect with has its own sentence
(spec docs/specs/2026-10-14-help-and-onboarding-design.md §7.2): an AST scan of the
routers plus the service codes, rendered through the macro the page uses.

The scan cannot see a code added only inside one of the services below; SERVICE_CODES is
kept in step with them by hand."""
import ast
from functools import cache
from pathlib import Path

import pytest

from src.web.templating import make_templates

ROOT = Path(__file__).resolve().parents[2]
ROUTERS = [ROOT / "src/routers/workspace" / n for n in
           ("pi_profile.py", "pi_evidence.py", "pi_bots.py", "pi_directory.py")]
GENERIC = "Something went wrong saving changes."
#: The literal codes the four routers send to workspace_pi_detail, through a redirect
#: helper or a direct page_url call. The scan below must find nothing outside this set.
ROUTER_CODES = frozenset({
    "pubmed_unreachable", "pubmed_not_found", "persona_busy", "regenerate_refused",
    "invalid_reporter_id", "reporter_unreachable", "no_profile_selected", "orcid_busy",
    "industry_busy", "no_agent",
})
#: Codes returned by the services the PI-detail routes pass a refusal through:
#: profile_edit.apply_profile_edits (without email_required, which only onboarding
#: sets), profile_review.accept_draft, agent_mute.set_agent_mute_state and
#: email_verification.mark_email_verified. /manager/pis/{id} forwards only what these
#: routes produce, since its POST aliases are the same handlers.
SERVICE_CODES = frozenset({
    # profile_edit.apply_profile_edits
    "field_too_long", "invalid_name", "profile_generating", "invalid_tenure_year",
    "profile_version_missing", "profile_changed", "summary_too_long", "too_many_tags",
    "tag_too_long", "tag_invalid", "nothing_to_save", "invalid_email", "email_taken",
    # profile_review.accept_draft
    "no_draft", "draft_stale",
    # agent_mute.set_agent_mute_state
    "agent_not_mutable", "no_token", "activation_blocked", "no_spoke",
    # email_verification.mark_email_verified
    "no_email", "email_changed",
})
KNOWN_CODES = ROUTER_CODES | SERVICE_CODES
#: Codes pi_directory.py sends to the PI list, not to PI detail.
DIRECTORY_CODES = frozenset({"create_failed", "exists", "agent_conflict", "name_required"})


def _is_pi_detail_url(call: ast.Call) -> bool:
    return (
        (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) == "page_url"
        and any(isinstance(a, ast.Constant) and a.value == "workspace_pi_detail"
                for a in call.args)
    )


def _error_value(call: ast.Call) -> ast.expr | None:
    """The value under the "error" key of a page_url call's literal query dict."""
    for kw in call.keywords:
        if kw.arg == "query" and isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                if isinstance(k, ast.Constant) and k.value == "error":
                    return v
    return None


def _trees() -> list[ast.Module]:
    return [ast.parse(path.read_text(encoding="utf-8")) for path in ROUTERS]


def _pi_detail_helpers(trees) -> dict[str, tuple[int, str]]:
    """Functions that take the code as an argument: a parameter passed straight into a
    workspace_pi_detail URL's ``error``. Maps name -> (positional index, parameter name).
    A route handler that redirects with a local variable (a service's return) is not one."""
    helpers = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [a.arg for a in node.args.posonlyargs + node.args.args]
            for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
                value = _error_value(call) if _is_pi_detail_url(call) else None
                if isinstance(value, ast.Name) and value.id in params:
                    helpers[node.name] = (params.index(value.id), value.id)
    return helpers


@cache
def _router_codes() -> frozenset[str]:
    trees = _trees()
    helpers = _pi_detail_helpers(trees)
    codes = set()
    for tree in trees:
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name in helpers:
                index, param = helpers[name]
                args = [call.args[index]] if len(call.args) > index else []
                args += [kw.value for kw in call.keywords if kw.arg == param]
                codes |= {a.value for a in args
                          if isinstance(a, ast.Constant) and isinstance(a.value, str)}
            if _is_pi_detail_url(call):
                value = _error_value(call)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    codes.add(value.value)
    return frozenset(codes)


def _text(code: str) -> str:
    module = make_templates().env.get_template("pis/_detail_errors.html").module
    return str(module.pi_detail_error_text(code)).strip()


def test_scan_finds_the_redirect_helpers():
    """Guards the scan itself: if the helpers stop being found it would pass vacuously."""
    helpers = _pi_detail_helpers(_trees())
    assert {"_review_redirect", "_grants_redirect"} <= helpers.keys()
    assert not any(name.startswith("workspace_") for name in helpers)


def test_scan_skips_the_pi_list_codes():
    codes = _router_codes()
    assert {"industry_busy", "no_agent", "persona_busy"} <= codes
    assert not codes & DIRECTORY_CODES


def test_router_codes_are_all_known():
    assert _router_codes() <= KNOWN_CODES, sorted(_router_codes() - KNOWN_CODES)


@pytest.mark.parametrize("code", sorted(KNOWN_CODES))
def test_every_known_code_has_a_sentence(code):
    assert _text(code) not in ("", GENERIC)


def test_unknown_code_falls_back():
    assert _text("definitely_not_a_code") == GENERIC
