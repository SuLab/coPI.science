"""Wipe all bot messages from Slack and optionally reset working memories.

This is a destructive mass-delete: it removes every bot message across every
public channel of whatever workspace the tokens authenticate to. To make it
hard to run against the wrong workspace by accident (SEC-11), it:

  * requires a ``--workspace`` assertion that must match the workspace the
    tokens actually authenticate to (team id, team domain, or workspace name),
  * supports ``--dry-run`` to report what *would* be deleted, and
  * prompts for confirmation before deleting (skip with ``--yes``).

Bot tokens are sourced from the DB (``AgentRegistry.slack_bot_token``) with the
legacy ``.env`` mapping as a fallback.

Usage:
    # See what would be deleted (safe):
    docker exec copi-python-opus-app-1 python3 scripts/wipe_slack.py \
        --workspace T0123ABCD --dry-run

    # Actually delete (asks for confirmation):
    docker exec -it copi-python-opus-app-1 python3 scripts/wipe_slack.py \
        --workspace T0123ABCD

    # Non-interactive delete + reset memories:
    docker exec copi-python-opus-app-1 python3 scripts/wipe_slack.py \
        --workspace T0123ABCD --yes --memory

    # Only reset working memories (no Slack access):
    docker exec copi-python-opus-app-1 python3 scripts/wipe_slack.py --memory-only
"""

import argparse
import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path

# Prefer the mounted project root over any copy of `src` baked into the image.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_sdk import WebClient  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.models import AgentRegistry  # noqa: E402
from src.services.slack_tokens import token_for_agent_row  # noqa: E402

MEMORY_DIR = Path("profiles/memory")

# Subtypes that can't be deleted by bots
UNDELETABLE_SUBTYPES = {"channel_join", "channel_leave", "channel_purpose", "channel_topic"}


async def _load_bots() -> list[tuple[str, str]]:
    """Load (agent_id, bot_token) for every agent with a valid token.

    DB column first (authoritative), then the legacy ``.env`` fallback — the
    same precedence the running app uses (src.services.slack_tokens).
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sf() as db:
            rows = (
                await db.execute(select(AgentRegistry).order_by(AgentRegistry.agent_id))
            ).scalars().all()
            bots = []
            for r in rows:
                tok = token_for_agent_row(r)
                if tok:
                    bots.append((r.agent_id, tok))
    finally:
        await engine.dispose()
    return bots


def _assert_workspace(client: WebClient, expected: str) -> dict:
    """Authenticate and verify the workspace matches ``expected``.

    ``expected`` may be the team id (T…), the team domain, or the workspace
    name. Aborts the process on any mismatch so a stale/misconfigured token
    can't silently wipe the wrong workspace (SEC-11).
    """
    try:
        info = client.auth_test()
    except Exception as exc:
        print(f"FATAL: could not authenticate to Slack: {exc}", flush=True)
        sys.exit(1)

    team_id = info.get("team_id", "")
    team = info.get("team", "")
    url = info.get("url", "")
    want = expected.strip().lower()
    candidates = {team_id.lower(), team.lower(), url.lower().rstrip("/")}
    matched = want in candidates or any(want and want in c for c in candidates if c)
    if not matched:
        print(
            "FATAL: --workspace assertion did not match the authenticated "
            f"workspace.\n  expected: {expected!r}\n  team_id : {team_id}\n"
            f"  team    : {team}\n  url     : {url}",
            flush=True,
        )
        sys.exit(1)
    print(f"Workspace confirmed: team={team!r} team_id={team_id} url={url}", flush=True)
    return info


def _count_for_bot(agent_id: str, bot_token: str, channel_ids: list[str], channel_names: dict[str, str]) -> int:
    """Dry-run: count (don't delete) this bot's deletable messages."""
    client = WebClient(token=bot_token)
    try:
        bot_user_id = client.auth_test()["user_id"]
    except Exception as exc:
        print(f"[{agent_id}] AUTH FAILED: {exc}", flush=True)
        return 0

    total = 0
    for ch_id in channel_ids:
        name = channel_names.get(ch_id, ch_id)
        cursor = None
        ch_count = 0
        while True:
            try:
                hist = client.conversations_history(channel=ch_id, limit=200, cursor=cursor)
            except SlackApiError as e:
                if e.response.get("error") == "ratelimited":
                    delay = int(e.response.headers.get("Retry-After", 2))
                    time.sleep(delay)
                    continue
                break
            except Exception:
                break
            for m in hist.get("messages", []):
                if m.get("user") == bot_user_id and m.get("subtype") not in UNDELETABLE_SUBTYPES:
                    ch_count += 1
            cursor = hist.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        if ch_count:
            print(f"[{agent_id}] #{name} would delete {ch_count} messages", flush=True)
        total += ch_count
    print(f"[{agent_id}] DRY-RUN — {total} messages would be deleted", flush=True)
    return total


def _wipe_for_bot(agent_id: str, bot_token: str, channel_ids: list[str], channel_names: dict[str, str]) -> int:
    """Join all channels and delete this bot's messages."""
    print(f"[{agent_id}] authenticating...", flush=True)
    client = WebClient(token=bot_token)
    try:
        bot_user_id = client.auth_test()["user_id"]
        print(f"[{agent_id}] authenticated as {bot_user_id}", flush=True)
    except Exception as exc:
        print(f"[{agent_id}] AUTH FAILED: {exc}", flush=True)
        return 0

    total = 0
    for ch_id in channel_ids:
        name = channel_names.get(ch_id, ch_id)
        skip_ts = set()  # track messages we failed to delete so we don't loop forever

        while True:
            try:
                hist = client.conversations_history(channel=ch_id, limit=200)
            except SlackApiError as e:
                if e.response.get("error") == "ratelimited":
                    delay = int(e.response.headers.get("Retry-After", 2))
                    print(f"[{agent_id}] #{name} rate limited on history, waiting {delay}s", flush=True)
                    time.sleep(delay)
                    continue
                break
            except Exception:
                break

            msgs = hist.get("messages", [])
            if not msgs:
                break

            my_msgs = [
                m for m in msgs
                if m.get("user") == bot_user_id
                and m.get("subtype") not in UNDELETABLE_SUBTYPES
                and m["ts"] not in skip_ts
            ]
            if not my_msgs:
                break

            print(f"[{agent_id}] #{name} deleting {len(my_msgs)} messages...", flush=True)
            for msg in my_msgs:
                ts = msg["ts"]
                try:
                    client.chat_delete(channel=ch_id, ts=ts)
                    total += 1
                    time.sleep(0.05)
                except SlackApiError as e:
                    err = e.response.get("error")
                    if err == "ratelimited":
                        delay = int(e.response.headers.get("Retry-After", 2))
                        print(f"[{agent_id}] #{name} rate limited, waiting {delay}s", flush=True)
                        time.sleep(delay)
                        try:
                            client.chat_delete(channel=ch_id, ts=ts)
                            total += 1
                        except Exception:
                            skip_ts.add(ts)
                    else:
                        skip_ts.add(ts)
                except Exception:
                    skip_ts.add(ts)

    print(f"[{agent_id}] DONE — {total} messages deleted", flush=True)
    return total


def wipe_slack(workspace: str, dry_run: bool, assume_yes: bool):
    bots = asyncio.run(_load_bots())
    if not bots:
        print("No agents with a valid Slack token found — nothing to do.", flush=True)
        return
    print(f"Bots: {[b[0] for b in bots]}", flush=True)

    client = WebClient(token=bots[0][1])
    # Refuse to touch anything unless the operator's --workspace matches the
    # workspace these tokens actually authenticate to.
    _assert_workspace(client, workspace)

    channels = client.conversations_list(types="public_channel", limit=200)["channels"]
    channel_ids = [ch["id"] for ch in channels]
    channel_names = {ch["id"]: ch["name"] for ch in channels}
    print(f"Channels: {[ch['name'] for ch in channels]}", flush=True)

    worker = _count_for_bot if dry_run else _wipe_for_bot

    if not dry_run:
        print(
            f"\n*** About to DELETE all bot messages from {len(bots)} bots across "
            f"{len(channel_ids)} public channels. This cannot be undone. ***",
            flush=True,
        )
        if not assume_yes:
            if not sys.stdin.isatty():
                print(
                    "Refusing to run non-interactively without --yes. "
                    "Re-run with --dry-run first, or add --yes to confirm.",
                    flush=True,
                )
                sys.exit(1)
            reply = input(f"Type the workspace ({workspace!r}) to confirm deletion: ").strip()
            if reply != workspace:
                print("Confirmation did not match — aborting.", flush=True)
                sys.exit(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bots)) as pool:
        futures = {
            pool.submit(worker, aid, tok, channel_ids, channel_names): aid
            for aid, tok in bots
        }
        total = sum(f.result() for f in concurrent.futures.as_completed(futures))

    verb = "would be deleted" if dry_run else "deleted"
    print(f"\n=== DONE — {total} total messages {verb} ===", flush=True)


def reset_memories():
    count = 0
    for f in sorted(MEMORY_DIR.glob("*.md")):
        f.unlink()
        count += 1
        print(f"  Deleted: {f.name}", flush=True)
    print(f"Reset {count} working memories", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        help="Required for a Slack wipe: the team id, team domain, or workspace "
        "name the tokens must authenticate to. Guards against wiping the wrong "
        "workspace.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting."
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the interactive deletion confirmation."
    )
    parser.add_argument("--memory", action="store_true", help="Also reset bot working memories")
    parser.add_argument("--memory-only", action="store_true", help="Only reset working memories")
    args = parser.parse_args()

    if not args.memory_only:
        if not args.workspace:
            parser.error("--workspace is required (use --dry-run to preview). "
                         "Pass the team id / domain / name the tokens belong to.")
        wipe_slack(args.workspace, args.dry_run, args.yes)
    if args.memory or args.memory_only:
        reset_memories()
