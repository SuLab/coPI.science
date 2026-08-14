"""Star-topology menu exactness: each role's rendered menu is its whole menu."""
from src.agent.post_types import available_for
from src.agent.roles import load_role

_GATE = {"blackbird", "su"}
_ROLES = {"blackbird": "scout_hub", "su": "pi_lab"}


def _names(role, self_id):
    return [
        s.name
        for s in available_for(
            load_role(role).post_types, gate=_GATE, roles_by_agent=_ROLES,
            self_id=self_id,
        )
    ]


def test_pi_lab_menu_is_exactly_pitch():
    assert _names("pi_lab", "su") == ["pitch"]


def test_scout_hub_menu_is_empty():
    """The hub went reply-only (Option A relocation): its former sole post
    type, :mag: Opportunity Assessment, is not a post type at all anymore —
    it is the `<assessment_json>` sidecar carried inside its own Phase-4
    CONCLUDE reply (see simulation.py's `_reply_to_thread`). role.toml
    declares `post_types = []` explicitly (not an absent key, which would
    silently hand it DEFAULT_POST_TYPES/`pitch` instead — see post_types.py's
    `parse_post_types`), so the hub's menu is permanently empty."""
    assert _names("scout_hub", "blackbird") == []
