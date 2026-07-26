"""One-time repair of the ``agent_messages.slack_ts`` mirror mapping.

Rows written before Stage 6 (and, until the poller's bot branch was fixed, any
message polled from another workspace bot) came from Slack but were stored with
``slack_ts`` NULL. ``_restored_slack_ts`` used to paper over that by *inferring*
the mapping — "a row in a real Slack channel was born on Slack, so its canonical
id is its Slack ts" — which is wrong for a DB-origin message that also carries a
real Slack channel id: a PI message written through the web inbox, or an agent
post whose Slack mirror failed. Inferring there fabricates a timestamp Slack
never issued, and the engine then hands it to chat.postMessage as a thread_ts.

So the guess is gone and this script repairs the data instead, by asking Slack
which timestamps actually exist. Only confirmed ones are written; anything Slack
does not recognise is left NULL, which is now the truthful value.

Run it once per deployment that has pre-Stage-6 history, BEFORE relying on the
no-inference behaviour:

    docker compose exec app python scripts/backfill_slack_ts.py            # report only
    docker compose exec app python scripts/backfill_slack_ts.py --apply    # write

Read-only against Slack; the only DB writes are ``slack_ts`` on rows Slack
confirmed. Safe to re-run.
"""

from __future__ import annotations

import asyncio
import sys

from slack_sdk import WebClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.services.slack_tokens import get_any_bot_token

CANDIDATES = text(
    """
    SELECT message_ts, channel_id, channel_name, sender_name
    FROM agent_messages
    WHERE slack_ts IS NULL AND channel_id NOT LIKE 'local:%'
    ORDER BY message_ts
    """
)

APPLY = text(
    """
    UPDATE agent_messages SET slack_ts = message_ts
    WHERE slack_ts IS NULL AND message_ts = :ts AND channel_id = :ch
    """
)


def _exists_on_slack(client: WebClient, channel_id: str, ts: str) -> bool | None:
    """True/False if Slack answered, None if the lookup itself failed."""
    try:
        resp = client.conversations_history(
            channel=channel_id, latest=ts, oldest=ts, inclusive=True, limit=1,
        )
    except Exception as exc:  # noqa: BLE001 — an API error must not be read as "absent"
        print(f"  ! lookup failed for {ts} in {channel_id}: {exc}")
        return None
    return any(m.get("ts") == ts for m in resp.get("messages", []))


async def main(apply: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        rows = (await db.execute(CANDIDATES)).all()
        token = await get_any_bot_token(db)

    if not rows:
        print("Nothing to do: no rows with a NULL slack_ts in a Slack channel.")
        await engine.dispose()
        return 0
    if not token:
        print("ERROR: no usable bot token — cannot verify against Slack.", file=sys.stderr)
        await engine.dispose()
        return 1

    client = WebClient(token=token)
    confirmed: list[tuple[str, str]] = []
    absent = 0
    errored = 0
    print(f"{len(rows)} candidate row(s):\n")
    for ts, channel_id, channel_name, sender_name in rows:
        found = _exists_on_slack(client, channel_id, ts)
        mark = {True: "on Slack", False: "NOT on Slack (DB-origin)", None: "unverified"}[found]
        print(f"  {ts}  #{channel_name:<24} {sender_name:<22} {mark}")
        if found is True:
            confirmed.append((ts, channel_id))
        elif found is False:
            absent += 1
        else:
            errored += 1

    print(
        f"\nconfirmed={len(confirmed)}  db_origin={absent}  unverified={errored}"
    )
    if not apply:
        print("\nDry run. Re-run with --apply to write slack_ts on the confirmed rows.")
        await engine.dispose()
        return 0

    async with session_factory() as db:
        for ts, channel_id in confirmed:
            await db.execute(APPLY, {"ts": ts, "ch": channel_id})
        await db.commit()
    print(f"\nUpdated {len(confirmed)} row(s). The rest keep slack_ts NULL, which is correct.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv)))
