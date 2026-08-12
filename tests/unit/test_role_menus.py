"""Star-topology menu exactness: each role's rendered menu is its whole menu."""
from src.agent.post_types import available_for
from src.agent.roles import load_role

_GATE = {"blackbird", "su"}
_ROLES = {"blackbird": "scout_hub", "su": "pi_lab"}


def _names(role, self_id, terminal_only=False):
    return [
        s.name
        for s in available_for(
            load_role(role).post_types, gate=_GATE, roles_by_agent=_ROLES,
            self_id=self_id, terminal_only=terminal_only,
        )
    ]


def test_pi_lab_menu_is_exactly_pitch():
    assert _names("pi_lab", "su") == ["pitch"]


def test_scout_hub_menu_is_exactly_opportunity_assessment():
    assert _names("scout_hub", "blackbird") == ["opportunity_assessment"]


def test_blocked_lab_menu_is_empty_blocked_hub_keeps_assessment():
    assert _names("pi_lab", "su", terminal_only=True) == []
    assert _names("scout_hub", "blackbird", terminal_only=True) == ["opportunity_assessment"]
