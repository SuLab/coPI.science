"""`_validate_star_topology` — the startup fail-fast check for hub-and-spoke
cohorts.

Design: docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md §5 —
cohort rows must be star-shaped: `{lab, hub}` per lab, never a lab-to-lab
cohort. Task 10 of
docs/superpowers/plans/2026-08-12-pr34-branch2-engine-reconciliation.md.

`_validate_star_topology` is a pure read of `self.agents` (role +
`allowed_sender_ids`) with no DB/session_factory involvement, so gates are set
directly on the constructed agents rather than routed through
`compute_gates`/a real cohort recompute.
"""

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine(roles: dict[str, str], gates: dict[str, set[str] | None]) -> SimulationEngine:
    agents = [
        Agent(agent_id=aid, bot_name=f"{aid.capitalize()}Bot", pi_name=f"PI {aid}", role=role)
        for aid, role in roles.items()
    ]
    eng = SimulationEngine(agents=agents, slack_clients={}, budget_cap=0)
    for aid, gate in gates.items():
        eng.agents[aid].allowed_sender_ids = gate
    return eng


class TestValidateStarTopology:
    def test_star_gates_pass(self):
        """Every lab cohorted only with the hub — the design's intended shape."""
        eng = _engine(
            roles={"su": "pi_lab", "wiseman": "pi_lab", "blackbird": "scout_hub"},
            gates={
                "su": {"su", "blackbird"},
                "wiseman": {"wiseman", "blackbird"},
                "blackbird": {"su", "wiseman", "blackbird"},
            },
        )
        assert eng._validate_star_topology() == []

    def test_lab_to_lab_gate_is_a_violation_naming_both_agents(self):
        """A lab reachable from another lab directly breaks the hub-only design.

        Both su and wiseman can see the violation from their own gate, but it is
        reported once, not once per side.
        """
        eng = _engine(
            roles={"su": "pi_lab", "wiseman": "pi_lab", "blackbird": "scout_hub"},
            gates={
                "su": {"su", "wiseman", "blackbird"},
                "wiseman": {"su", "wiseman", "blackbird"},
                "blackbird": {"su", "wiseman", "blackbird"},
            },
        )
        violations = eng._validate_star_topology()
        assert len(violations) == 1, violations
        assert "su" in violations[0]
        assert "wiseman" in violations[0]

    def test_lab_with_no_hub_in_gate_is_a_violation(self):
        """A lab that can't reach the hub has nowhere to land a pitch."""
        eng = _engine(
            roles={"su": "pi_lab", "blackbird": "scout_hub"},
            gates={"su": {"su"}, "blackbird": {"blackbird"}},
        )
        violations = eng._validate_star_topology()
        assert len(violations) == 1, violations
        assert "su" in violations[0]
        assert "unreachable" in violations[0]

    def test_gate_none_passes_vacuously(self):
        """Isolation off (gate is None) is not a violation — an ungated agent can
        always reach the hub (and everyone else)."""
        eng = _engine(
            roles={"su": "pi_lab", "wiseman": "pi_lab"},
            gates={"su": None, "wiseman": None},
        )
        assert eng._validate_star_topology() == []


class TestStartupWiring:
    """Pins that the raise lives at the start() call site, not inside the shared
    recompute method (which mid-run callers also use and must never raise from).
    """

    def test_start_raises_immediately_after_the_first_recompute(self):
        import inspect

        src = inspect.getsource(SimulationEngine.start)
        idx_recompute = src.index("await self._recompute_allowed_sender_ids()")
        idx_validate = src.index("self._validate_star_topology()")
        idx_raise = src.index("raise RuntimeError")
        assert idx_recompute < idx_validate < idx_raise
        assert "Star-topology validation failed" in src

    def test_recompute_never_raises_on_a_violation(self):
        import inspect

        src = inspect.getsource(SimulationEngine._recompute_allowed_sender_ids)
        assert "_validate_star_topology" in src
        assert "logger.error" in src
        assert "raise RuntimeError" not in src
