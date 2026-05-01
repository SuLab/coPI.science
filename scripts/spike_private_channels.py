"""Spike: verify Slack private-channel creation and bot-to-bot invite mechanics.

This validates the assumptions baked into specs/privacy-and-channel-visibility.md
before we start implementing the migration flow. Creates one throwaway private
channel, exercises the three mechanics in question, prints a pass/fail summary,
then archives the channel.

Usage:
    docker exec copi-python-opus-app-1 python3 scripts/spike_private_channels.py \\
        --bot-a su --bot-b wiseman

    # Optional: also invite a human user to verify the PI-invite path
    docker exec copi-python-opus-app-1 python3 scripts/spike_private_channels.py \\
        --bot-a su --bot-b wiseman --pi-user-id U01234567

    # Optional: also test the negative case (uninvited bot tries to post)
    docker exec copi-python-opus-app-1 python3 scripts/spike_private_channels.py \\
        --bot-a su --bot-b wiseman --uninvited-bot lotz

What it checks:
    (1) bot A can create a private channel via conversations.create(is_private=true)
    (2) bot A can invite bot B via conversations.invite
    (3) bot B can post to the channel using its own token (without conversations.join)
    (4) bot A and bot B can both read history
    (5) an uninvited bot gets 'not_in_channel' when trying to post
    (6) an uninvited bot gets the expected error when trying conversations.join
        on a private channel

Cleanup: the channel is archived at the end via bot A's token. If the script
fails mid-way, re-run with --archive-only <channel_id> to clean up.
"""

import argparse
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.config import get_settings


def _get_tokens(agent_ids: list[str]) -> dict[str, str]:
    """Load bot tokens for the given agent IDs from settings."""
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    out = {}
    for aid in agent_ids:
        tok = env_tokens.get(aid, "")
        if not tok or tok.startswith("xoxb-placeholder"):
            print(f"FATAL: no valid bot token configured for agent '{aid}'", flush=True)
            sys.exit(2)
        out[aid] = tok
    return out


def _bot_user_id(token: str, label: str) -> str:
    client = WebClient(token=token)
    try:
        uid = client.auth_test()["user_id"]
        print(f"[{label}] bot user_id = {uid}", flush=True)
        return uid
    except SlackApiError as exc:
        print(f"[{label}] FATAL: auth_test failed: {exc.response.get('error')}", flush=True)
        sys.exit(2)


def _archive(token: str, channel_id: str) -> None:
    try:
        WebClient(token=token).conversations_archive(channel=channel_id)
        print(f"archived {channel_id}", flush=True)
    except SlackApiError as exc:
        print(f"archive failed for {channel_id}: {exc.response.get('error')}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-a", required=True, help="Agent ID of the channel creator")
    parser.add_argument("--bot-b", required=True, help="Agent ID of the invited bot")
    parser.add_argument("--uninvited-bot", default=None, help="Optional agent ID for the negative case")
    parser.add_argument("--pi-user-id", default=None, help="Optional Slack user ID to also invite")
    parser.add_argument("--archive-only", default=None, help="Skip the spike and just archive this channel ID")
    args = parser.parse_args()

    agents = [args.bot_a, args.bot_b]
    if args.uninvited_bot:
        agents.append(args.uninvited_bot)
    tokens = _get_tokens(agents)

    if args.archive_only:
        _archive(tokens[args.bot_a], args.archive_only)
        return 0

    bot_a_uid = _bot_user_id(tokens[args.bot_a], args.bot_a)
    bot_b_uid = _bot_user_id(tokens[args.bot_b], args.bot_b)
    bot_c_uid = _bot_user_id(tokens[args.uninvited_bot], args.uninvited_bot) if args.uninvited_bot else None

    client_a = WebClient(token=tokens[args.bot_a])
    client_b = WebClient(token=tokens[args.bot_b])
    client_c = WebClient(token=tokens[args.uninvited_bot]) if args.uninvited_bot else None

    slug = f"spike-priv-{args.bot_a}-{args.bot_b}-{int(time.time())}"
    results: list[tuple[str, bool, str]] = []

    # (1) Create private channel via bot A
    print(f"\n=== (1) create private channel '{slug}' via bot A ===", flush=True)
    try:
        resp = client_a.conversations_create(name=slug, is_private=True)
        channel_id = resp["channel"]["id"]
        print(f"  OK — channel_id = {channel_id}, is_private = {resp['channel'].get('is_private')}", flush=True)
        results.append(("create private channel (bot A)", True, channel_id))
    except SlackApiError as exc:
        err = exc.response.get("error")
        print(f"  FAIL — {err}", flush=True)
        results.append(("create private channel (bot A)", False, err))
        _summarize(results)
        return 1

    try:
        # (2) Invite bot B via bot A
        print(f"\n=== (2) bot A invites bot B ({bot_b_uid}) ===", flush=True)
        try:
            client_a.conversations_invite(channel=channel_id, users=bot_b_uid)
            print("  OK", flush=True)
            results.append(("invite bot B via bot A", True, ""))
        except SlackApiError as exc:
            err = exc.response.get("error")
            print(f"  FAIL — {err}", flush=True)
            results.append(("invite bot B via bot A", False, err))

        # (2b) Optionally invite the PI user
        if args.pi_user_id:
            print(f"\n=== (2b) bot A invites PI user {args.pi_user_id} ===", flush=True)
            try:
                client_a.conversations_invite(channel=channel_id, users=args.pi_user_id)
                print("  OK", flush=True)
                results.append(("invite PI user via bot A", True, ""))
            except SlackApiError as exc:
                err = exc.response.get("error")
                print(f"  FAIL — {err}", flush=True)
                results.append(("invite PI user via bot A", False, err))

        # (3) Bot A posts
        print("\n=== (3a) bot A posts message ===", flush=True)
        try:
            client_a.chat_postMessage(channel=channel_id, text=f"Hello from {args.bot_a} (bot A)")
            print("  OK", flush=True)
            results.append(("bot A posts", True, ""))
        except SlackApiError as exc:
            err = exc.response.get("error")
            print(f"  FAIL — {err}", flush=True)
            results.append(("bot A posts", False, err))

        # (3b) Bot B posts WITHOUT conversations.join (the key question)
        print("\n=== (3b) bot B posts without prior conversations.join ===", flush=True)
        try:
            client_b.chat_postMessage(channel=channel_id, text=f"Hello from {args.bot_b} (bot B)")
            print("  OK — bot B did not need to call conversations.join", flush=True)
            results.append(("bot B posts without join", True, ""))
        except SlackApiError as exc:
            err = exc.response.get("error")
            print(f"  FAIL — {err}", flush=True)
            results.append(("bot B posts without join", False, err))

        # (4a) Bot A reads history
        print("\n=== (4a) bot A reads conversations.history ===", flush=True)
        try:
            hist_a = client_a.conversations_history(channel=channel_id, limit=10)
            msgs = hist_a.get("messages", [])
            print(f"  OK — bot A sees {len(msgs)} messages", flush=True)
            results.append(("bot A reads history", True, f"{len(msgs)} messages"))
        except SlackApiError as exc:
            err = exc.response.get("error")
            print(f"  FAIL — {err}", flush=True)
            results.append(("bot A reads history", False, err))

        # (4b) Bot B reads history
        print("\n=== (4b) bot B reads conversations.history ===", flush=True)
        try:
            hist_b = client_b.conversations_history(channel=channel_id, limit=10)
            msgs = hist_b.get("messages", [])
            print(f"  OK — bot B sees {len(msgs)} messages", flush=True)
            results.append(("bot B reads history", True, f"{len(msgs)} messages"))
        except SlackApiError as exc:
            err = exc.response.get("error")
            print(f"  FAIL — {err}", flush=True)
            results.append(("bot B reads history", False, err))

        # (5) Uninvited bot tries to post — expect 'not_in_channel' or 'channel_not_found'
        if client_c is not None:
            print(f"\n=== (5) uninvited bot ({args.uninvited_bot}) tries to post ===", flush=True)
            try:
                client_c.chat_postMessage(channel=channel_id, text="I should not be here")
                print("  UNEXPECTED SUCCESS — private channel did NOT block uninvited bot post", flush=True)
                results.append(("uninvited bot blocked from posting", False, "unexpectedly succeeded"))
            except SlackApiError as exc:
                err = exc.response.get("error")
                print(f"  OK — blocked with error: {err}", flush=True)
                results.append(("uninvited bot blocked from posting", True, err))

            # (6) Uninvited bot tries conversations.join — expect it to fail cleanly
            print(f"\n=== (6) uninvited bot tries conversations.join (private channel) ===", flush=True)
            try:
                client_c.conversations_join(channel=channel_id)
                print("  UNEXPECTED SUCCESS — uninvited bot was allowed to self-join a private channel", flush=True)
                results.append(("uninvited bot blocked from self-joining", False, "unexpectedly succeeded"))
            except SlackApiError as exc:
                err = exc.response.get("error")
                print(f"  OK — blocked with error: {err}", flush=True)
                results.append(("uninvited bot blocked from self-joining", True, err))

    finally:
        print("\n=== cleanup: archiving channel ===", flush=True)
        _archive(tokens[args.bot_a], channel_id)

    return _summarize(results)


def _summarize(results: list[tuple[str, bool, str]]) -> int:
    print("\n" + "=" * 60, flush=True)
    print("SPIKE RESULTS", flush=True)
    print("=" * 60, flush=True)
    all_ok = True
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {name}"
        if detail:
            line += f"  ({detail})"
        print(line, flush=True)
        if not ok:
            all_ok = False
    print("=" * 60, flush=True)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILURES'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
