import logging

from src.agent import roles
from src.agent.roles import DEFAULT_TOOLS, RoleSpec, load_role


def _write_role(tmp_path, monkeypatch, name, toml_text):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    d = tmp_path / "roles" / name
    d.mkdir(parents=True)
    (d / "role.toml").write_text(toml_text, encoding="utf-8")


def test_resolve_falls_back_to_global_when_no_role_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == tmp_path / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "GLOBAL"


def test_resolve_prefers_role_override_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")
    role_dir = tmp_path / "roles" / "scout_hub"
    role_dir.mkdir(parents=True)
    (role_dir / "agent-system.md").write_text("HUB", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == role_dir / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "HUB"


def test_pi_lab_resolves_to_global_even_if_role_dir_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "phase5-new-post.md").write_text("DEFAULT", encoding="utf-8")

    p = roles.resolve_prompt_path("pi_lab", "phase5-new-post.md")

    assert p == tmp_path / "phase5-new-post.md"


def test_missing_manifest_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    spec = load_role("pi_lab")
    assert spec == RoleSpec(name="pi_lab", label="pi_lab", tools=DEFAULT_TOOLS)


def test_manifest_sets_label_and_tool_allow_list(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\n'
        'tools = ["retrieve_profile", "search_prior_art"]\n',
    )
    # search_prior_art must exist in TOOL_DEFINITIONS by the time this runs
    # (Task 7). Until then this asserts only the known tool survives.
    spec = load_role("scout_hub")
    assert spec.name == "scout_hub"
    assert spec.label == "Scout Hub"
    assert "retrieve_profile" in spec.tools


def test_unknown_tool_is_dropped_and_logged(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "weird",
        'tools = ["retrieve_profile", "does_not_exist"]\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("weird")
    assert "does_not_exist" not in spec.tools
    assert "retrieve_profile" in spec.tools
    assert any("does_not_exist" in r.message for r in caplog.records)


def test_malformed_toml_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    _write_role(tmp_path, monkeypatch, "broken", "tools = [not valid toml")
    with caplog.at_level(logging.ERROR):
        spec = load_role("broken")
    assert spec.tools == DEFAULT_TOOLS
    assert spec.label == "broken"


# ----------------------------------------------------------------------
# scout_hub role content (Task 9) — uses the REAL prompts/roles dir, no
# monkeypatch, so this exercises the actual shipped role.toml / phase5
# override on disk.
# ----------------------------------------------------------------------

from src.agent.roles import load_role as _load_role_real  # noqa: E402 (see above)


def test_scout_hub_ships_with_the_hub_tool_set():
    spec = _load_role_real("scout_hub")
    assert spec.label == "Scout Hub"
    assert "search_prior_art" in spec.tools


def test_scout_hub_phase4_override_renders_and_drops_the_tool_it_lacks():
    from pathlib import Path

    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    tokens = (
        "{channel_name}", "{other_agent_name}", "{other_agent_lab}",
        "{message_count}", "{thread_phase}", "{thread_history}",
        "{phase_guidance}", "{instructions}", "{foa_number}",
        "{funding_thread_context}",
    )

    # Pin the raw template on disk: every token must actually be present in the
    # source, and neither forbidden string may sneak in there either. Without
    # this half, the post-render absence checks below can't tell "substituted"
    # apart from "never written" — a token silently deleted from the template
    # would still make every `not in content` assertion pass.
    raw_template = Path("prompts/roles/scout_hub/phase4-thread-reply.md").read_text(
        encoding="utf-8"
    )
    for token in tokens:
        assert token in raw_template, f"template is missing substitution token {token!r}"
    assert "retrieve_foa" not in raw_template
    assert ":memo:" not in raw_template

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird Labs", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang", message_count=5
    )
    _, messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "WangBot", "content": "our CRISPR screen hit DBT"}],
        other_agent_name="WangBot",
        other_agent_lab="Wang",
    )
    content = messages[0]["content"]

    # The override rendered, not a silent fallback to the pi_lab template.
    assert "scouting interview" in content.lower()
    # retrieve_foa is withheld from this role by role.toml — the prompt must not
    # tell the agent it is required, or available at all.
    assert "retrieve_foa" not in content
    # This role never brokers or proposes collaborations.
    assert ":memo:" not in content
    assert "beats either lab working alone" not in content
    # search_prior_art IS in this role's tool set and must be documented.
    assert "search_prior_art" in content
    # Every substitution token was consumed.
    for token in tokens:
        assert token not in content, f"leftover token {token!r}"
    # The Task 5 DECIDE guidance landed in the rendered prompt.
    assert "Baltimore commitment" in content


def test_scout_hub_phase5_override_renders_in_both_modes():
    """build_phase5_prompt loads phase5-new-post.md through the role-aware
    _load_prompt() (see src/agent/agent.py), so a scout_hub agent must pick up
    prompts/roles/scout_hub/phase5-new-post.md, not the global pi_lab template.

    This guards the byte-for-byte scaffolding that build_phase5_prompt's
    .replace()/regex substitution depends on:
      - the four substitution tokens are each replaced exactly once
      - the funding_only regexes (keyed to '## Your subscribed channels',
        '## Your recent posts', '## Prior conversations with other labs',
        and the 'Option C ... Option D' block) still find their targets
        in the scout_hub override, in both normal and funding_only mode.
    """
    from src.agent.agent import Agent

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird Labs", role="scout_hub")

    leftover_tokens = [
        "{interesting_posts}",
        "{subscribed_channels}",
        "{your_recent_posts}",
        "{prior_conversations}",
        "{post_type_menu}",
    ]

    for funding_only in (False, True):
        system_prompt, messages = agent.build_phase5_prompt(
            recent_posts=[{"channel": "general", "content_snippet": "an old post"}],
            foa_contexts={},
            thread_foa_contexts={"RFA-AI-27-019": "Example FOA text"},
            prior_threads={
                "wiseman": [
                    {"channel": "general", "outcome": "no_proposal", "summary": "n/a"}
                ]
            },
            funding_only=funding_only,
            funding_thread_summaries={},
        )
        assert isinstance(system_prompt, str)
        content = messages[0]["content"]

        # All four tokens were substituted — none survive as raw placeholders.
        for token in leftover_tokens:
            assert token not in content, (
                f"leftover token {token!r} in scout_hub phase5 prompt "
                f"(funding_only={funding_only})"
            )

        # Confirms the scout_hub override actually rendered (not a silent
        # fallback to the global pi_lab template).
        assert "As the Blackbird scouting hub" in content

    # funding_only=True must strip Option C (the regular new-post artifact)
    # while keeping Option D (skip) — this is the hardcoded regex in
    # agent.py keyed to these exact headings.
    _, funding_only_messages = agent.build_phase5_prompt(funding_only=True)
    funding_only_content = funding_only_messages[0]["content"]
    assert "### Option C: Make a new top-level post" not in funding_only_content
    assert "### Option D: Skip this turn" in funding_only_content
    assert "## Your subscribed channels" not in funding_only_content
    assert "## Your recent posts" not in funding_only_content
    assert "## Prior conversations with other labs" not in funding_only_content

    # Non-funding_only mode keeps the full option set, including the
    # opportunity-assessment artifact instructions.
    _, normal_messages = agent.build_phase5_prompt()
    normal_content = normal_messages[0]["content"]
    assert "### Option C: Make a new top-level post" in normal_content
    assert ":mag: **Opportunity Assessment**" in normal_content


def test_role_rate_override_is_read_when_positive(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 20\n',
    )
    assert load_role("scout_hub").calls_per_load_per_window == 20


def test_role_rate_override_defaults_to_none(tmp_path, monkeypatch):
    _write_role(tmp_path, monkeypatch, "scout_hub", 'label = "Scout Hub"\n')
    assert load_role("scout_hub").calls_per_load_per_window is None


def test_role_rate_override_rejects_non_positive(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 0\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None
    assert "calls_per_load_per_window" in caplog.text


def test_role_rate_override_rejects_non_int(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = "lots"\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None


def test_missing_manifest_yields_no_rate_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    assert load_role("pi_lab").calls_per_load_per_window is None


def test_scout_hub_prompts_state_the_title_only_limitation():
    """The hub's whole novelty read rests on this tool. The prompt must not let it
    describe a title search as though it covered claims, and must not name the
    decommissioned PatentsView endpoint.

    Beyond the vocabulary checks, the assertions below bind the *meaning* of the
    caveat rather than just the presence of the word "title" -- a prompt that uses
    "title" for an unrelated reason, or that states the US-only limitation without
    the title-only one, must still fail. Each phrase is copied verbatim (modulo
    markdown emphasis and line-wrap whitespace, which the normalizer below erases)
    from the current prompts, so it fails loudly against a plausible wrong version:
    one that drops the abstracts/claims exclusion, the broadened-query case, the
    empty-result "not novelty, not FTO" framing, the 2-4-term query guidance with
    its concrete contrast, or -- the regression that motivated this -- a citation
    instruction that carries only the US-only half of the caveat.
    """
    from pathlib import Path

    def _normalize(text: str) -> str:
        # Strip markdown emphasis and collapse whitespace/line-wraps so these
        # assertions aren't brittle to reflowing or bold/italic-only edits.
        return " ".join(text.replace("*", "").split())

    for name in ("agent-system.md", "phase5-new-post.md"):
        path = Path("prompts/roles/scout_hub") / name
        body = path.read_text(encoding="utf-8")
        assert "PatentsView" not in body, f"{name} still names the dead endpoint"
        assert "title" in body.lower(), f"{name} omits the title-only limitation"

    system = (Path("prompts/roles/scout_hub") / "agent-system.md").read_text(encoding="utf-8")
    phase5 = (Path("prompts/roles/scout_hub") / "phase5-new-post.md").read_text(encoding="utf-8")
    norm_system = _normalize(system)
    norm_phase5 = _normalize(phase5)

    assert "freedom-to-operate" in system.lower()
    assert "2-4" in system

    # The title-only limitation must be stated in terms that exclude abstracts and
    # claims, not merely that the word "title" appears somewhere in the file.
    assert "not abstracts, not claims" in norm_system, (
        "agent-system.md no longer excludes abstracts/claims from the title-only limitation"
    )
    assert "differently-titled patent" in norm_phase5, (
        "phase5-new-post.md no longer distinguishes a title match from a claims/differently-"
        "titled match"
    )

    # An empty/no-hit result must be described as neither novelty nor freedom-to-operate.
    assert "is never novelty and never freedom-to-operate" in norm_system, (
        "agent-system.md no longer states that an empty title search is neither novelty nor FTO"
    )

    # The broadened-search case must be addressed in both files.
    assert "reports it broadened your query" in norm_system
    assert "if the tool broadened your query, say so" in norm_phase5.lower()

    # The 2-4-specific-terms guidance must come with a concrete good/bad contrast,
    # not just the bare "2-4" token.
    assert "TFEB melanoma" in norm_system
    assert "TFEB inhibitor nuclear translocation melanoma BRAF resistance" in norm_system

    # The citation instruction (Citing Papers) must itself carry the title-only
    # limitation. It's the more specific instruction a model follows when writing
    # an actual citation, so fixing the caveat only in the principles/Tools
    # sections and leaving this one stale would still reproduce the omission this
    # task exists to remove.
    assert (
        "cite the patent ID and filing date, and always attach the caveat: "
        "title-only, US-only"
    ) in norm_system, "Citing Papers section still omits the title-only limitation"


def test_scout_hub_assessment_follows_the_blackbird_rubric():
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "phase5-new-post.md").read_text(
        encoding="utf-8"
    )
    # C.1 gating, C.2 funnel, C.3 scores, C.5 red flags, C.6 verdict.
    for required in (
        "Funnel stage", "Gating criteria", "Red flags", "Recommendation",
        "route-to-incubation", "<assessment_json>", "weighted_score",
        "suggested_derisking_milestones",
    ):
        assert required in body, f"assessment template omits {required!r}"
    # The Baltimore gate is asked, never inferred from the institution.
    assert "JHU address is not" in body
    # Maryland non-dilutive leverage, not a generic NIH-mechanism frame.
    assert "TEDCO" in body and "BIITC" in body
    # The sidecar must NOT be fenced — _parse_phase5_response takes the last
    # ```json``` block as the ACTION, so a fenced sidecar would hijack it.
    # rsplit: the tag name also appears in the prose above the real block, and
    # only the real block's contents are the thing under test.
    sidecar = body.rsplit("<assessment_json>", 1)[1].split("</assessment_json>")[0]
    assert "```" not in sidecar
    assert '"funnel_stage"' in sidecar
    # Scaffolding the existing renderer depends on must survive the rewrite.
    for anchor in (
        "### Option C: Make a new top-level post", "### Option D: Skip this turn",
        "## Your subscribed channels", "## Your recent posts",
        "## Prior conversations with other labs", ":mag: **Opportunity Assessment**",
        "As the Blackbird scouting hub", "{interesting_posts}",
        "{subscribed_channels}", "{your_recent_posts}", "{prior_conversations}",
        "{post_type_menu}",
    ):
        assert anchor in body, f"rewrite broke the renderer anchor {anchor!r}"


def test_baltimore_is_a_question_not_an_inference():
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "agent-system.md").read_text(
        encoding="utf-8"
    )
    assert "Baltimore" in body
    assert "is not a Baltimore commitment" in body


def test_visible_body_hides_the_verdict_the_sidecar_still_carries():
    """F5: the bot is a member of every lab's cohort, so a :mag: Opportunity
    Assessment (a top-level post) is a workspace-wide broadcast, not a private
    note. The visible `<slack_message>` must therefore read as a courtesy
    summary and never carry the funnel stage, the four gating statuses, the
    red-flag list, or the advance/conditional/pass/route-to-incubation
    recommendation — those belong only in the staff-only `<assessment_json>`
    sidecar, which must still require all of them so staff lose nothing.
    """
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "phase5-new-post.md").read_text(
        encoding="utf-8"
    )

    # Anchors bounding the visible-body instructions and the sidecar
    # instructions within Option C. If any of these move, the slice below
    # would silently cover the wrong text, so pin their relative order.
    visible_start = body.index("Label it :mag: **Opportunity Assessment**")
    sidecar_start = body.index("**Also emit the machine-readable verdict.**")
    option_d_start = body.index("### Option D: Skip this turn")
    assert visible_start < sidecar_start < option_d_start

    visible_instructions = body[visible_start:sidecar_start]
    sidecar_instructions = body[sidecar_start:option_d_start]

    # The PI-facing instructions must not ask for (or even name) the internal
    # verdict machinery.
    for forbidden in (
        "Funnel stage", "Gating criteria", "Red flags", "route-to-incubation",
        "not_met", "not met",
    ):
        assert forbidden not in visible_instructions, (
            f"{forbidden!r} leaked into the visible-body instructions — this "
            "would surface the internal rubric in a workspace-wide post"
        )
    for forbidden_word in ("advance", "conditional", "pass"):
        assert forbidden_word not in visible_instructions.lower(), (
            f"{forbidden_word!r} leaked into the visible-body instructions"
        )

    # The sidecar instructions must still require every one of them — staff
    # must lose nothing.
    for required in (
        "Funnel stage", "Gating criteria", "Red flags", "route-to-incubation",
        "not_met",
    ):
        assert required in sidecar_instructions, (
            f"sidecar instructions dropped {required!r} — staff would lose "
            "part of the verdict"
        )
    for required_word in ("advance", "conditional", "pass"):
        assert required_word in sidecar_instructions.lower(), (
            f"sidecar instructions dropped {required_word!r} — staff would "
            "lose part of the recommendation"
        )


def test_gating_values_in_assessment_skeleton_are_tristate_strings():
    """An unasked gate is not a failed gate: gating.* must round-trip through the
    sidecar as one of three strings, never a bare boolean, so a downstream reader
    (and the staff triage page) can tell "never asked" apart from "asked and no".
    """
    import json
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "phase5-new-post.md").read_text(
        encoding="utf-8"
    )
    # Same rsplit as the rubric test above: the tag name also appears in the prose
    # describing the skeleton, so only the LAST occurrence opens the real block.
    sidecar = body.rsplit("<assessment_json>", 1)[1].split("</assessment_json>")[0]
    skeleton = json.loads(sidecar)
    gating = skeleton["gating"]

    allowed_states = {"met", "not_met", "unconfirmed"}
    for key, value in gating.items():
        assert isinstance(value, str), (
            f"gating.{key} is {value!r} ({type(value).__name__}) — must be a string, "
            "never a bare true/false"
        )
        assert value in allowed_states, f"gating.{key}={value!r} not in {allowed_states}"

    # The skeleton actually demonstrates all three states, not one value repeated
    # across all four keys.
    assert set(gating.values()) == allowed_states

    # The prose gating item uses the same three words the JSON keys expect.
    assert "met" in body and "not met" in body and "unconfirmed" in body
    assert '"met"' in body and '"not_met"' in body and '"unconfirmed"' in body


def test_missing_manifest_yields_default_post_types():
    from src.agent.post_types import DEFAULT_POST_TYPES

    spec = load_role("definitely_not_a_role_dir")
    assert spec.post_types == DEFAULT_POST_TYPES


def test_manifest_post_types_are_parsed(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "opportunity_assessment"\n'
        '[[post_types]]\nname = "pitch"\ntargets = ["scout_hub"]\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["opportunity_assessment", "pitch"]
    assert dict((s.name, s.targets) for s in spec.post_types)["pitch"] == frozenset(
        {"scout_hub"}
    )


def test_manifest_unknown_post_type_is_dropped(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "opportunity_assessment"\n'
        '[[post_types]]\nname = "nonsense"\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["opportunity_assessment"]
    assert "nonsense" in caplog.text


def test_malformed_toml_still_yields_default_post_types(tmp_path, monkeypatch):
    from src.agent.post_types import DEFAULT_POST_TYPES

    _write_role(tmp_path, monkeypatch, "broken", "label = = =\n")
    assert load_role("broken").post_types == DEFAULT_POST_TYPES


def test_scout_hub_declares_only_the_assessment():
    spec = load_role("scout_hub")
    assert {s.name for s in spec.post_types} == {"opportunity_assessment"}
    assert dict((s.name, s.targets) for s in spec.post_types)[
        "opportunity_assessment"
    ] == frozenset()


def test_scout_hub_cannot_post_a_cross_lab_idea():
    """The hub is not a party to the science — brokering is explicitly not its
    job (prompts/roles/scout_hub/agent-system.md)."""
    assert "idea_crosslab" not in {s.name for s in load_role("scout_hub").post_types}
    assert "pitch" not in {s.name for s in load_role("scout_hub").post_types}


def test_pi_lab_phase5_template_renders_in_both_modes():
    """The global template's tokens and funding_only surgeries were pinned
    nowhere — only the scout_hub override was. This rewrite is exactly the kind
    of change that needs the pin."""
    from src.agent.agent import Agent

    agent = Agent("gill", "GillBot", "Gill PI")  # role defaults to pi_lab

    for funding_only in (False, True):
        _, messages = agent.build_phase5_prompt(
            recent_posts=[{"channel": "general", "content_snippet": "an old post"}],
            foa_contexts={},
            thread_foa_contexts={"RFA-AI-27-019": "Example FOA text"},
            prior_threads={
                "pearce": [
                    {"channel": "general", "outcome": "no_proposal", "summary": "n/a"}
                ]
            },
            funding_only=funding_only,
            funding_thread_summaries={},
        )
        content = messages[0]["content"]
        for token in (
            "{interesting_posts}", "{subscribed_channels}", "{your_recent_posts}",
            "{prior_conversations}", "{post_type_menu}",
        ):
            assert token not in content, (
                f"leftover token {token!r} (funding_only={funding_only})"
            )

    _, fo = agent.build_phase5_prompt(funding_only=True)
    fo_content = fo[0]["content"]
    assert "### Option C: Make a new top-level post" not in fo_content
    assert "### Option D: Skip this turn" in fo_content
    assert "## Your subscribed channels" not in fo_content
    assert "## Your recent posts" not in fo_content
    assert "## Prior conversations with other labs" not in fo_content

    _, normal = agent.build_phase5_prompt()
    assert "### Option C: Make a new top-level post" in normal[0]["content"]
