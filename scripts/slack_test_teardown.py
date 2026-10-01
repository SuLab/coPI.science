#!/usr/bin/env python3
"""Archive t-prefixed test channels and delete *ProbeBot apps in the test workspace.

Refuses to touch anything outside those two patterns. DRY RUN by default: nothing is
archived or deleted without --apply. --workspace is required and must EXACTLY equal the
team id, team name, workspace URL or subdomain the bot token authenticates to.

NEVER drops the copi_slack_test database: _config_token() writes the rotated Slack
app-config credential pair into its app_settings table, and the refresh token it was
rotated from is single-use and already dead. Dropping that database loses Slack
app-configuration access permanently.

  SLACK_CONFIG_TOKEN=... SLACK_TEST_BOT_TOKEN_SU=... python scripts/slack_test_teardown.py --workspace T0123ABCD
  (add --apply to act)
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://slack.com/api"
CHANNEL_PREFIX = "t-"
APP_NAME_SUFFIX = "ProbeBot"


def call(method, token, payload=None, form=False):
    if form:
        data = urllib.parse.urlencode(payload or {}).encode()
        headers = {"Authorization": f"Bearer {token}"}
    else:
        data = json.dumps(payload or {}).encode()
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json; charset=utf-8"}
    req = urllib.request.Request(f"{API}/{method}", data=data, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def assert_workspace(auth: dict, expected: str) -> None:
    """Refuse a token from any workspace but the one named (exact match, as
    wipe_slack.py: a substring would let ``test`` match ``test-prod``)."""
    want = expected.strip().lower()
    url = auth.get("url", "").lower().rstrip("/")
    host = url.split("://", 1)[-1]
    candidates = {auth.get("team_id", "").lower(), auth.get("team", "").lower(), url, host,
                  host.split(".", 1)[0]}
    candidates.discard("")
    if not want or want not in candidates:
        print(f"FATAL: --workspace {expected!r} does not match team_id={auth.get('team_id')} "
              f"team={auth.get('team')} url={auth.get('url')}")
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True,
                    help="team id, team name or domain the bot token must authenticate to")
    ap.add_argument("--apply", action="store_true", help="actually archive/delete (default: dry run)")
    args = ap.parse_args()

    bot = os.environ.get("SLACK_TEST_BOT_TOKEN_SU")
    if not bot:
        sys.exit("SLACK_TEST_BOT_TOKEN_SU is required to list/archive channels")

    assert_workspace(call("auth.test", bot, {}, form=True), args.workspace)

    cursor, targets = "", []
    while True:
        d = call("conversations.list", bot, {
            "types": "public_channel,private_channel", "limit": 200, "cursor": cursor,
        }, form=True)
        if not d.get("ok"):
            sys.exit(f"conversations.list failed: {d.get('error')}")
        for c in d["channels"]:
            if c["name"].startswith(CHANNEL_PREFIX) and not c.get("is_archived"):
                targets.append(c)
        cursor = (d.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break

    print(f"channels to archive ({len(targets)}):")
    for c in targets:
        print(f"  #{c['name']} {c['id']}")
        if not args.apply:
            print(f"    (dry run) would archive #{c['name']}")
        else:
            r = call("conversations.archive", bot, {"channel": c["id"]}, form=True)
            if not r.get("ok"):
                print(f"    WARNING: {r.get('error')}")

    cfg = os.environ.get("SLACK_CONFIG_TOKEN")
    if not cfg:
        print("\nSLACK_CONFIG_TOKEN not set — skipping app deletion")
        _end(args)
        return
    apps_file = os.environ.get("PROBE_APPS_JSON")
    if not apps_file or not os.path.exists(apps_file):
        print("\nPROBE_APPS_JSON not set — skipping app deletion "
              "(there is no list-apps API for config tokens)")
        _end(args)
        return
    apps = json.loads(open(apps_file).read())
    print(f"\napps to delete ({len(apps)}):")
    for aid, a in apps.items():
        if not a["bot_name"].endswith(APP_NAME_SUFFIX):
            print(f"  SKIP {a['bot_name']} — does not match *{APP_NAME_SUFFIX}")
            continue
        print(f"  {a['bot_name']} {a['app_id']}")
        if not args.apply:
            print(f"    (dry run) would delete {a['bot_name']}")
        else:
            r = call("apps.manifest.delete", cfg, {"app_id": a["app_id"]})
            if not r.get("ok"):
                print(f"    WARNING: {r.get('error')}")
    _end(args)


def _end(args):
    if not args.apply:
        print("dry run: nothing changed (pass --apply)")


if __name__ == "__main__":
    main()
