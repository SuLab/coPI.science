"""Bidirectional template <-> builder token-contract test (design invariant ii).

For each phase-2/4/5 builder in `src.agent.agent.Agent`, every bare `{token}`
in its covered template file(s) must have a matching `.replace("{token}", ...)`
substitution somewhere in the builder's own source, AND every `.replace(
"{token}", ...)` call in the builder's source must target a token that
actually appears in at least one of its covered templates. A token on only
one side is either a template that will render with a literal `{unfilled}`
placeholder, or a `.replace(...)` call left behind after a template was
edited (dead code, orphaned substitution) — this test pins both failure
modes. This is the invariant that would have caught four audit findings
during the pitch-only reconciliation.

Token regex is deliberately narrow: `{[a-z_]+}` only. Templates also contain
JSON example blocks like `{"action": "skip"}` and `{}` — those never match
because the character immediately after `{` is `"` (not `[a-z_]`), or because
`{}` has zero characters between the braces. Verified by direct inspection
(see task-14-report.md) that this regex extracts exactly the substitution
tokens and nothing from the JSON examples.

Identity tokens `{bot_name}`, `{pi_name}`, `{agent_id}` are excluded: they are
rendered by `Agent._render_identity`, not by these builders, and never appear
in the covered templates anyway.
"""
import inspect
import re
from pathlib import Path

from src.agent.agent import Agent

ROOT = Path(__file__).resolve().parents[2]

TOKEN_RE = re.compile(r"\{[a-z_]+\}")
REPLACE_RE = re.compile(r'\.replace\(\s*"(\{[a-z_]+\})"')
IDENTITY_TOKENS = {"{bot_name}", "{pi_name}", "{agent_id}"}

# Builder -> its covered templates (pi_lab default + scout_hub override, where
# a scout_hub variant exists). {new_posts}/{interesting_posts} live in the
# phase-2 builders, which are dormant in the running simulation (Task 8) but
# retained on disk with matching "disabled in code" preambles in their
# templates — still real contracts, so they're covered here, not excluded.
BUILDER_TEMPLATES: dict[str, list[str]] = {
    "build_phase2_scan_prompt": [
        "prompts/phase2-scan-filter.md",
        "prompts/roles/scout_hub/phase2-scan-filter.md",
    ],
    "build_phase2_prune_prompt": [
        "prompts/phase2-prune.md",
        "prompts/roles/scout_hub/phase2-prune.md",
    ],
    "build_phase4_prompt": [
        "prompts/phase4-thread-reply.md",
        "prompts/roles/scout_hub/phase4-thread-reply.md",
    ],
    "build_phase5_prompt": [
        "prompts/phase5-new-post.md",
        "prompts/roles/scout_hub/phase5-new-post.md",
    ],
}


def _tokens_in_file(relpath: str) -> set[str]:
    text = (ROOT / relpath).read_text(encoding="utf-8")
    return set(TOKEN_RE.findall(text)) - IDENTITY_TOKENS


def _per_file_tokens() -> dict[str, set[str]]:
    """All covered template files -> the tokens the regex matched in each.

    Used only to build a readable error message / report listing; the
    contract itself is checked per builder (role-set union) below.
    """
    files: set[str] = set()
    for paths in BUILDER_TEMPLATES.values():
        files.update(paths)
    return {f: _tokens_in_file(f) for f in sorted(files)}


def test_token_regex_matches_no_json_example_braces():
    """Guard against the regex accidentally matching JSON example blocks.

    Most covered templates contain a fenced ` ```json ` example — an
    `{"action": ...}` action block, or a bare `{}` — that a careless token
    regex could mistake for a substitution placeholder. `r"\\{[a-z_]+\\}"`
    never matches inside one: the character after `{` in a JSON example is
    always `"` (a quoted key) or the brace is empty, and the regex requires
    one or more bare lowercase/underscore characters between the braces.
    """
    json_block_re = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
    probed_at_least_one_block = False

    for relpath in sorted({p for paths in BUILDER_TEMPLATES.values() for p in paths}):
        text = (ROOT / relpath).read_text(encoding="utf-8")
        for block in json_block_re.findall(text):
            probed_at_least_one_block = True
            matches = TOKEN_RE.findall(block)
            assert not matches, (
                f"{relpath}: token regex matched inside a JSON example block: {matches}"
            )

    # Sanity: we actually exercised the JSON-example case above, so a clean
    # pass isn't just "there was nothing to probe."
    assert probed_at_least_one_block, (
        "expected at least one covered template to contain a ```json example "
        "block — none found; the templates may have changed shape"
    )


def test_builder_token_contract_is_bidirectional():
    """Invariant (ii): template tokens <-> builder `.replace(...)` calls.

    For each builder: every template token has a substitution, and every
    substitution targets a real template token. Failures list the exact
    offending tokens per builder for direct actionability.
    """
    failures: list[str] = []

    for builder_name, template_paths in BUILDER_TEMPLATES.items():
        method = getattr(Agent, builder_name)
        source = inspect.getsource(method)
        replace_targets = set(REPLACE_RE.findall(source)) - IDENTITY_TOKENS

        template_tokens: set[str] = set()
        for relpath in template_paths:
            template_tokens |= _tokens_in_file(relpath)

        missing_substitution = template_tokens - replace_targets
        if missing_substitution:
            failures.append(
                f"{builder_name}: template token(s) {sorted(missing_substitution)} "
                f"appear in {template_paths} but have no `.replace(\"{{token}}\", ...)` "
                f"call in {builder_name}'s source"
            )

        orphaned_replace = replace_targets - template_tokens
        if orphaned_replace:
            failures.append(
                f"{builder_name}: `.replace(...)` target(s) {sorted(orphaned_replace)} "
                f"in {builder_name}'s source do not appear in any of {template_paths} "
                "(orphaned substitution — the token was removed from the template "
                "but the .replace(...) call was left behind)"
            )

    assert not failures, "\n".join(failures)
