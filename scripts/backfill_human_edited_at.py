"""Re-run the D55 human_edited_at backfill (migration 0062; spec 2026-10-05 D55, §6.3).

0062 runs it once. Run it again after the new code serves (it catches edits saved through
the old app between the migration and `up -d`) and before the regeneration of every PI.
Idempotent: it never moves human_edited_at backward. Preview by default.

It works one profile at a time: each row is locked with FOR UPDATE SKIP LOCKED, updated
and committed on its own, so it never waits on a profile a worker or a request holds (and
cannot hold up the worker's long transaction). A profile that was locked is SKIPPED and
named at the end; the exit status is 2 while any was skipped, so re-run it until it
reports none.

  $DC run --rm --no-deps -T blackbird-app python scripts/backfill_human_edited_at.py           # preview
  $DC run --rm --no-deps -T blackbird-app python scripts/backfill_human_edited_at.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from src.database import get_session_factory  # noqa: E402

#: Spec 2026-10-05 D55: human_edited_at from web / web_impersonated public revisions newer
#: than the profile's generation whose Research Summary or tag sections differ from the
#: preceding public revision. The compared text is the persona between its header and the
#: first of Recent Publications / Active Grants / Past Grants. Never moves the value
#: backward, so it is safe to re-run.
HUMAN_EDITED_AT_BACKFILL_SQL = r"""
WITH bodies AS (
    SELECT id, agent_registry_id, mechanism, created_at,
           regexp_replace(content, E'\n## (Recent Publications|Active Grants|Past Grants).*$', '')
               AS body
      FROM profile_revisions
     WHERE profile_type = 'public'
), cores AS (
    SELECT id, agent_registry_id, mechanism, created_at,
           CASE WHEN strpos(body, E'\n## ') > 0 THEN substr(body, strpos(body, E'\n## '))
                ELSE '' END AS core
      FROM bodies
), seq AS (
    SELECT cores.*,
           lag(core) OVER (PARTITION BY agent_registry_id ORDER BY created_at, id) AS prev_core
      FROM cores
), edits AS (
    SELECT a.user_id, max(s.created_at) AS edited_at
      FROM seq s
      JOIN agents a ON a.id = s.agent_registry_id
      JOIN researcher_profiles p ON p.user_id = a.user_id
     WHERE s.mechanism IN ('web', 'web_impersonated')
       AND s.created_at > coalesce(p.profile_generated_at, '-infinity')
       AND s.prev_core IS DISTINCT FROM s.core
     GROUP BY a.user_id
)
UPDATE researcher_profiles p
   SET human_edited_at = e.edited_at
  FROM edits e
 WHERE p.user_id = e.user_id
   AND (p.human_edited_at IS NULL OR p.human_edited_at < e.edited_at)
"""


#: The migration's statement narrowed to one profile. The base text ends in a WHERE clause,
#: so the narrowing is one more AND.
_ONE_PROFILE_SQL = HUMAN_EDITED_AT_BACKFILL_SQL + "   AND p.user_id = :uid\n"


async def main(apply: bool) -> int:
    """Stamp profile by profile; return 2 if any profile was locked and skipped, else 0."""
    factory = get_session_factory()
    async with factory() as db:
        user_ids = list(
            (
                await db.execute(
                    text("SELECT user_id FROM researcher_profiles ORDER BY user_id")
                )
            ).scalars()
        )
    stamped = 0
    skipped: list = []
    for uid in user_ids:
        async with factory() as db:
            locked = (
                await db.execute(
                    text(
                        "SELECT user_id FROM researcher_profiles "
                        "WHERE user_id = :uid FOR UPDATE SKIP LOCKED"
                    ),
                    {"uid": uid},
                )
            ).first()
            if locked is None:
                skipped.append(uid)
                await db.rollback()
                continue
            result = await db.execute(text(_ONE_PROFILE_SQL), {"uid": uid})
            stamped += result.rowcount
            if apply:
                await db.commit()
            else:
                await db.rollback()
    print(f"{stamped} profile(s) {'stamped' if apply else 'would be stamped'}")
    if skipped:
        print(f"{len(skipped)} profile(s) skipped (row locked); re-run until none: ")
        for uid in skipped:
            print(f"  {uid}")
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write; default is a preview")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
