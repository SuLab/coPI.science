"""Channel visibility vocabulary — dependency-free.

Lives outside ``src/models`` so modules that must stay free of DB/ORM imports can
still speak the vocabulary. ``src.models.agent_activity`` re-exports both names, so
every existing importer keeps working unchanged.

See specs/privacy-and-channel-visibility.md:
- ``public``          — all bots and PIs; seeded and agent-created thematic channels.
- ``collab_private``  — 2 bots + up to 2 PIs; Slack ``is_private=true``.

``src/agent/message_log.py`` needs ``VISIBILITY_COLLAB_PRIVATE`` for the cohort
gate's private-channel exemption (.notes/cohort-system-v2.md §7) and is otherwise
dependency-free; importing the ORM module there would couple the in-memory log to
SQLAlchemy for the sake of one string.
"""

VISIBILITY_PUBLIC = "public"
VISIBILITY_COLLAB_PRIVATE = "collab_private"

__all__ = ["VISIBILITY_PUBLIC", "VISIBILITY_COLLAB_PRIVATE"]
