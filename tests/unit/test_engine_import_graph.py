"""Structure gates for the decomposed engine (spec §7.1, §7.2)."""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENGINE = ROOT / "src" / "agent" / "engine"
SIMULATION = ROOT / "src" / "agent" / "simulation.py"
LEAVES = ("constants", "helpers", "sidecar", "deps")
SEAM_CALLS = {"generate_with_tools", "generate_agent_response", "get_settings", "load_role",
              "get_build_info"}
SEAM_MODULES = {"time", "datetime"}
PATCHED_CONSTANTS = {"SEEDED_CHANNELS", "CHANNEL_POLL_INTERVAL", "HEADLINES_MAX_AT_SHUTDOWN",
                     "PROFILES_DIR", "_UNIVERSAL_CHANNELS", "_CHANNEL_KEYWORDS"}


def _engine_sources() -> dict[str, ast.Module]:
    files = {p.stem: p for p in sorted(ENGINE.glob("*.py")) if p.stem != "__init__"}
    files["simulation"] = SIMULATION
    return {name: ast.parse(path.read_text(encoding="utf-8")) for name, path in files.items()}


def test_the_four_leaves_exist():
    assert {p.stem for p in ENGINE.glob("*.py")} >= set(LEAVES)


def test_patch_seam_is_used_everywhere():
    """One patch on src.agent.engine.deps.X / constants.X must reach all engine code
    (spec §7.1 deps, §7.6): no function body calls a seam name bare, reads time. or
    datetime. off the bare module, or loads a patched constant by bare name."""
    offenders = []
    for name, tree in _engine_sources().items():
        if name in LEAVES:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in SEAM_CALLS:
                    offenders.append(f"{name}.{fn.name}:{n.lineno} calls {n.func.id}() bare")
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id in SEAM_MODULES and isinstance(n.ctx, ast.Load)):
                    offenders.append(f"{name}.{fn.name}:{n.lineno} reads {n.value.id}.{n.attr} bare")
                if isinstance(n, ast.Name) and n.id in PATCHED_CONSTANTS and isinstance(n.ctx, ast.Load):
                    offenders.append(f"{name}.{fn.name}:{n.lineno} reads {n.id} bare")
    assert offenders == [], "\n".join(offenders)


def test_leaves_import_nothing_from_the_engine():
    for leaf in LEAVES:
        tree = ast.parse((ENGINE / f"{leaf}.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            mod = n.module if isinstance(n, ast.ImportFrom) else None
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            for m in [mod, *names]:
                assert not (m or "").startswith(("src.agent.engine", "src.agent.simulation")), (leaf, m)


def test_simulation_still_exports_every_leaf_name():
    import src.agent.simulation as sim
    from src.agent.engine import constants, helpers, sidecar

    for module in (constants, helpers, sidecar):
        for name in module.__all__:
            assert getattr(sim, name) is getattr(module, name), name


ALLOWED: dict[str, set[str]] = {
    "context": set(),
    "persistence": {"context"},
    "llm_log": {"context", "persistence"},
    "channel_directory": {"context"},
    "slack_io": {"context", "channel_directory", "persistence"},
    "panel": {"context", "slack_io"},
    "headlines": {"context", "slack_io", "channel_directory"},
    "memory": {"context"},
    "verdicts": {"context", "persistence", "panel", "headlines"},
    "threads": {"context", "headlines", "memory", "verdicts", "slack_io", "persistence"},
    "scheduler": {"context", "channel_directory"},
    "roster": {"context", "channel_directory", "slack_io"},
    "post_lane": {"context", "scheduler", "roster", "threads", "slack_io", "llm_log",
                  "channel_directory", "panel"},
    "reply_lane": {"context", "scheduler", "post_lane", "threads", "verdicts", "panel",
                   "slack_io", "channel_directory"},
    "rebuild": {"context", "threads", "verdicts", "slack_io", "panel", "post_lane",
                "channel_directory"},
    "run_announcer": {"context", "slack_io", "channel_directory", "scheduler"},
    "control": {"context"},
}
PORT_HOLDERS = {"headlines": {"_ledger"}}


def _imports_of(tree: ast.Module) -> set[str]:
    """Engine modules a module imports, TYPE_CHECKING imports included."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            if n.module == "src.agent.engine":
                out |= {a.name for a in n.names}
            elif n.module.startswith("src.agent.engine."):
                out.add(n.module.split(".")[3])
            elif n.module == "src.agent.simulation" or (
                n.module == "src.agent" and any(a.name == "simulation" for a in n.names)
            ):
                out.add("simulation")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("src.agent.engine."):
                    out.add(a.name.split(".")[3])
                elif a.name == "src.agent.simulation":
                    out.add("simulation")
    return out


def _unit_modules() -> dict[str, ast.Module]:
    return {name: tree for name, tree in _engine_sources().items() if name != "simulation"}


def test_every_engine_import_edge_is_allowed_and_acyclic():
    graph: dict[str, set[str]] = {}
    problems = []
    for name, tree in _unit_modules().items():
        edges = _imports_of(tree) - {name}
        allowed = set(LEAVES) | ALLOWED.get(name, set()) if name not in LEAVES else set()
        for e in sorted(edges - allowed):
            problems.append(f"{name} imports {e}")
        graph[name] = edges - set(LEAVES)
    assert problems == [], "\n".join(problems)
    state: dict[str, int] = {}

    def visit(v, path):
        state[v] = 1
        for w in graph.get(v, ()):
            assert state.get(w) != 1, f"import cycle: {' -> '.join(path + [w])}"
            if w not in state:
                visit(w, path + [w])
        state[v] = 2

    for v in graph:
        if v not in state:
            visit(v, [v])


def test_every_via_and_unit_reference_is_an_allowed_edge():
    problems = []
    for name, tree in _unit_modules().items():
        if name in LEAVES or name == "context":
            continue
        allowed = ALLOWED[name] | {"context"}
        for n in ast.walk(tree):
            holder = None
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "via":
                holder = n.args[0].value
            elif (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                  and isinstance(n.value.value, ast.Name) and n.value.value.id == "self"
                  and n.value.attr.lstrip("_") in ALLOWED):
                holder = n.value.attr
            if holder is None:
                continue
            if holder in PORT_HOLDERS.get(name, set()):
                continue
            unit = "context" if holder in ("ctx", "_run_state") else holder.lstrip("_")
            if unit != name and unit not in allowed:
                problems.append(f"{name}:{n.lineno} reaches {holder}")
    assert problems == [], "\n".join(problems)


def test_every_engine_module_logs_as_src_agent_simulation():
    """Spec §7.2 rule 5: 18 tests filter caplog on this name, and the production log
    format prints %(name)s."""
    for path in sorted(ENGINE.glob("*.py")):
        names = re.findall(r"getLogger\(([^)]*)\)", path.read_text(encoding="utf-8"))
        assert all(n == '"src.agent.simulation"' for n in names), (path.name, names)
    assert re.findall(r"getLogger\(([^)]*)\)", SIMULATION.read_text(encoding="utf-8")) == ["__name__"]


EXPECTED_METHOD_OWNER: dict[str, str] = {
    "__init__": "orchestrator", "start": "orchestrator", "_run_main_loop": "orchestrator",
    "_drain_and_flush": "orchestrator", "stop": "orchestrator", "_sleep": "orchestrator",
    "_idle_backoff": "orchestrator", "_terminal_stall_reason": "orchestrator",
    "_record_run_end_state": "orchestrator", "_recover_reply_less_pitches": "orchestrator",
    "request_stop": "run_state",
    "_enqueue_persist": "persistence", "_flush_persisted": "persistence",
    "_recover_rows_individually": "persistence", "_report_flush_failure": "persistence",
    "_on_llm_call": "llm_log", "_on_flush_done": "llm_log", "_llm_log_record": "llm_log",
    "_flush_llm_logs": "llm_log", "_unbooked_calls": "llm_log",
    "_resolve_channel_visibility": "channel_directory", "_client_for_channel": "channel_directory",
    "_ensure_seeded_channels": "channel_directory",
    "_ensure_assessments_summary_channel": "channel_directory",
    "_persist_seeded_channels": "channel_directory",
    "mint_ts": "slack_io", "_post_message": "slack_io", "_mirrored_messages": "slack_io",
    "_slack_parent_ts": "slack_io", "_next_poll_client": "slack_io",
    "_poll_slack_for_bot_messages": "slack_io", "_log_poll_error": "slack_io",
    "_seed_slack_cursors_without_ingest": "slack_io",
    "_record_specialist_consult": "panel", "_post_panel_note": "panel",
    "_seed_consults_from_db": "panel", "_record_consult": "panel", "_note_consult": "panel",
    "_consulted_domains": "panel", "_computed_score_and_band": "panel",
    "_specialist_floor_gap": "panel", "_floor_unverifiable_reason": "panel",
    "_floor_verifiable": "panel",
    "_post_assessment_summary": "headlines", "_mark_summary_posted": "headlines",
    "_announce_owed_headline": "headlines", "_drain_pending_headlines": "headlines",
    "_claim_headline": "headlines", "_release_headline_claim": "headlines",
    "_post_claimed_headline": "headlines",
    "_capture_hub_assessment": "verdicts", "_persist_assessment": "verdicts",
    "_verdict_is_terminal": "verdicts", "_sidecar_refusal": "verdicts",
    "upsert": "verdicts", "_assessment_row": "verdicts",
    "_prune_queued_for_thread": "verdicts", "_rehydrate_assessed_threads": "verdicts",
    "_record_assessment_drop": "verdicts", "_flush_pending_assessments": "verdicts",
    "_record_unwritable_assessment": "verdicts",
    "_warn_if_hub_conclude_missing_assessment": "verdicts",
    "_close_thread": "threads", "_evict_dead_thread": "threads",
    "_get_prior_threads_for_agent": "threads",
    "_drain_memory_events": "memory", "_update_agent_memory": "memory",
    "_pending_reply_pairs": "reply_lane", "_service_reply": "reply_lane",
    "_dispatch_reply_lane": "reply_lane", "_reply_to_thread": "reply_lane",
    "_check_thread_outcome": "reply_lane",
    "_run_post_turn": "post_lane", "_phase1_channel_discovery": "post_lane",
    "_phase3_activate_threads": "post_lane", "_auto_activate_lab_posts": "post_lane",
    "_phase5_new_post": "post_lane", "_parse_phase5_response": "post_lane",
    "_available_post_types": "post_lane", "_normalize_tagged_agent": "post_lane",
    "_post_type_rejection": "post_lane", "_post_types_for_role": "post_lane",
    "_roles_by_agent": "post_lane", "_rehydrate_proposal_count": "post_lane",
    "_open_interview_count": "post_lane", "_proposal_target_drained": "post_lane",
    "is_within_time_limit": "scheduler", "_agent_within_budget": "scheduler",
    "_agent_load": "scheduler", "_calls_per_load": "scheduler", "_allowance_for": "scheduler",
    "_within_rate_limit": "scheduler", "_active_thread_count": "scheduler",
    "_count_today_posts": "scheduler", "_turn_eligible": "scheduler", "_select_agent": "scheduler",
    "_poll_control_plane": "control",
    "_sync_profiles_from_disk": "roster", "_sync_roster_from_db": "roster",
    "_build_lab_directories": "roster", "refresh_lab_directories": "roster",
    "_infer_agent_id": "roster", "_disable_all_gates": "roster",
    "_validate_star_topology": "roster", "_recompute_allowed_sender_ids": "roster",
    "_apply_cohort_gate_to_state": "roster", "cohort_topology_snapshot": "roster",
    "_record_topology_snapshot": "roster", "_strip_disallowed_tags": "roster",
    "_rebuild_state_from_db": "rebuild", "_rebuild_agent_state": "rebuild",
    "_restore_slack_state": "rebuild",
    "_run_start_announcement_values": "run_announcer", "_announce_overrides": "run_announcer",
    "_announce_run_start": "run_announcer", "_record_run_start_announcement": "run_announcer",
}
EXPECTED_STATE_OWNER: dict[str, str] = {
    "agents": "ctx", "slack_clients": "ctx", "message_log": "ctx", "session_factory": "ctx",
    "simulation_run_id": "ctx", "slack_enabled": "ctx", "_agent_locks": "ctx",
    "_thread_locks": "ctx",
    "_running": "run_state", "_stop_event": "run_state", "_end_reason": "run_state",
    "_pending_persist": "persistence", "_last_run_stats_update": "persistence",
    "_persist_flush_lock": "persistence",
    "_llm_log_buffer": "llm_log", "_llm_log_flush_size": "llm_log", "_flush_tasks": "llm_log",
    "_channel_id_map": "channel_directory", "_channel_visibility": "channel_directory",
    "_assessments_summary_channel_id": "channel_directory",
    "_poll_cursors": "slack_io", "_poll_client_cursor": "slack_io",
    "_last_channel_poll": "slack_io", "_poll_error_last_logged": "slack_io",
    "_ts_minter": "slack_io",
    "_specialist_consults": "panel", "_consult_signal_counts": "panel",
    "_consult_signal_counts_by_domain": "panel", "_panel_notes_posted": "panel",
    "_panel_notes_clipped": "panel", "_panel_note_clip_warned": "panel",
    "_pending_headlines": "headlines", "_in_doubt_headlines": "headlines",
    "_unclaimed_headlines": "headlines", "_announced": "headlines",
    "_assessed_threads": "verdicts", "_pending_assessments": "verdicts",
    "_closed_thread_ids": "threads", "_prior_threads": "threads",
    "_pending_memory_events": "memory", "_memory_drain_lock": "memory",
    "_reply_in_flight": "reply_lane", "_reply_sem": "reply_lane",
    "max_proposals": "post_lane", "_proposals_posted": "post_lane",
    "_proposal_drain_streak": "post_lane", "_role_post_types_cache": "post_lane",
    "_post_type_rejections": "post_lane",
    "max_runtime_minutes": "scheduler", "budget_cap": "scheduler", "_start_time": "scheduler",
    "_role_rate_cache": "scheduler",
    "_last_control_poll": "control",
    "_bot_name_to_id": "roster", "_profile_mtimes": "roster", "_last_roster_poll": "roster",
    "_cohort_gate_active": "roster", "_cohort_preflight_error": "roster",
    "_cohort_log_signature": "roster", "_cohort_tags_stripped": "roster",
    "_fresh_start": "rebuild", "_reset_cursors": "rebuild",
}
#: Members whose name differs on the owner (spec §7.1 table, this plan's Reference section).
RENAMED = {"_rehydrate_assessed_threads": "rehydrate", "_agent_locks": "agent_locks",
           "_thread_locks": "thread_locks", "_running": "running",
           "_stop_event": "stop_event", "_end_reason": "end_reason"}


def _moved_units() -> set[str]:
    import src.agent.simulation as sim

    return set(sim._UNIT_CLASSES)


def test_every_member_lives_with_its_owner_and_nowhere_else():
    import src.agent.simulation as sim

    engine_members = set(vars(sim.SimulationEngine))
    moved = _moved_units()
    problems = []
    for name, owner in EXPECTED_METHOD_OWNER.items():
        target = RENAMED.get(name, name)
        if owner == "orchestrator":
            if name not in engine_members:
                problems.append(f"{name} left the orchestrator")
            continue
        if owner == "run_state":
            if sim._FORWARD.get(name) != ("run_state", target):
                problems.append(f"{name} is not forwarded to run_state")
            continue
        if owner in moved:
            cls = sim._UNIT_CLASSES[owner]
            if not callable(vars(cls).get(target)) and not isinstance(vars(cls).get(target), (property, staticmethod)):
                problems.append(f"{name} is not defined on {cls.__name__} as {target}")
            wrapper_ok = name == "_rehydrate_assessed_threads"
            if name in engine_members and not wrapper_ok:
                problems.append(f"{name} is defined on both SimulationEngine and {owner}")
        elif name not in engine_members:
            problems.append(f"{name} is neither on SimulationEngine nor on a moved unit")
    for name, owner in EXPECTED_STATE_OWNER.items():
        target = RENAMED.get(name, name)
        if owner in ("ctx", "run_state"):
            if sim._FORWARD.get(name) != (owner, target):
                problems.append(f"state {name} is not forwarded to {owner}.{target}")
        elif owner in moved:
            if name not in sim._UNIT_CLASSES[owner].OWNED_STATE:
                problems.append(f"state {name} missing from {owner}.OWNED_STATE")
    assert problems == [], "\n".join(problems)


def test_owned_state_matches_each_unit_init():
    import src.agent.simulation as sim

    for holder, cls in sim._UNIT_CLASSES.items():
        tree = ast.parse((ENGINE / f"{holder}.py").read_text(encoding="utf-8"))
        klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls.__name__)
        init = next(n for n in klass.body if getattr(n, "name", "") == "__init__")
        stored = {
            n.attr for n in ast.walk(init)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "self" and isinstance(n.ctx, ast.Store)
        }
        deps = {a for a in stored if a == "ctx" or a.lstrip("_") in ALLOWED or a in (
            "_run_state", "_ledger", "_on_thread_gone", "_strip_disallowed_tags", "_rejection_counts")}
        assert stored - deps == set(cls.OWNED_STATE), (holder, stored - deps, cls.OWNED_STATE)


ORCHESTRATOR = {"__init__", "start", "_run_main_loop", "_drain_and_flush", "stop", "_sleep",
                "_idle_backoff", "_terminal_stall_reason", "_rehydrate_assessed_threads",
                "_record_run_end_state", "_recover_reply_less_pitches",
                "__getattr__", "__setattr__", "__delattr__"}
ALL_UNITS = {"persistence", "llm_log", "channel_directory", "slack_io", "panel", "headlines",
             "verdicts", "threads", "memory", "reply_lane", "post_lane", "scheduler", "control",
             "roster", "rebuild", "run_announcer"}


def test_every_unit_is_wired_and_the_orchestrator_is_only_wiring():
    import types

    import src.agent.simulation as sim

    assert set(sim._UNIT_CLASSES) == ALL_UNITS
    members = {
        n for n, v in vars(sim.SimulationEngine).items()
        if isinstance(v, (types.FunctionType, staticmethod, classmethod, property))
    }
    assert members == ORCHESTRATOR, sorted(members ^ ORCHESTRATOR)


SHARED_STATE_OWNERS = {
    "_closed_thread_ids": "threads",
    "_pending_headlines": "headlines",
    "_pending_memory_events": "memory",
    "_prior_threads": "threads",
    "_poll_cursors": "slack_io",
    "_assessed_threads": "verdicts",
}
MUTATING_METHODS = {
    "add", "discard", "remove", "pop", "popitem", "clear", "update",
    "append", "appendleft", "extend", "insert", "setdefault",
}


def test_shared_state_is_changed_only_by_its_owner():
    """Spec §7.2 rule 1: other units change these six only through the owner's
    methods (``mark_closed``, ``restore_prior``, ``enqueue``, ``drop_pending``,
    ``seed_cursor``, ``mark_announced``). ``via`` would let any unit write through, so
    this pins it. The orchestrator (``simulation.py``) is scanned too."""
    offenders = []
    for path in [*sorted(ENGINE.glob("*.py")), SIMULATION]:
        unit = path.stem
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            touched = None
            if isinstance(node, (ast.Assign, ast.Delete)):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                targets = []
            for target in targets:
                base = target.value if isinstance(target, ast.Subscript) else target
                if isinstance(base, ast.Attribute) and base.attr in SHARED_STATE_OWNERS:
                    touched = base.attr
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATING_METHODS
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in SHARED_STATE_OWNERS
            ):
                touched = node.func.value.attr
            if touched and SHARED_STATE_OWNERS[touched] != unit:
                offenders.append(f"{path}:{node.lineno} changes {touched}")
    assert not offenders, offenders
