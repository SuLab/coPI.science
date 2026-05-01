"""Wipe all bot messages from Slack and optionally reset working memories.

Usage:
    docker exec copi-python-opus-app-1 python3 scripts/wipe_slack.py
    docker exec copi-python-opus-app-1 python3 scripts/wipe_slack.py --memory
"""

import argparse
import concurrent.futures
import time
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from src.config import get_settings

MEMORY_DIR = Path("profiles/memory")

# Subtypes that can't be deleted by bots
UNDELETABLE_SUBTYPES = {"channel_join", "channel_leave", "channel_purpose", "channel_topic"}


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


def wipe_slack():
    settings = get_settings()
    tokens = settings.get_slack_tokens()
    bots = [(aid, tok) for aid, tok in tokens.items() if tok]
    print(f"Bots: {[b[0] for b in bots]}", flush=True)

    client = WebClient(token=bots[0][1])
    channels = client.conversations_list(types="public_channel", limit=200)["channels"]
    channel_ids = [ch["id"] for ch in channels]
    channel_names = {ch["id"]: ch["name"] for ch in channels}
    print(f"Channels: {[ch['name'] for ch in channels]}", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bots)) as pool:
        futures = {pool.submit(_wipe_for_bot, aid, tok, channel_ids, channel_names): aid for aid, tok in bots}
        total = sum(f.result() for f in concurrent.futures.as_completed(futures))

    print(f"\n=== DONE — {total} total messages deleted ===", flush=True)


def reset_memories():
    count = 0
    for f in sorted(MEMORY_DIR.glob("*.md")):
        f.unlink()
        count += 1
        print(f"  Deleted: {f.name}", flush=True)
    print(f"Reset {count} working memories", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory", action="store_true", help="Also reset bot working memories")
    parser.add_argument("--memory-only", action="store_true", help="Only reset working memories")
    args = parser.parse_args()

    if not args.memory_only:
        wipe_slack()
    if args.memory or args.memory_only:
        reset_memories()
