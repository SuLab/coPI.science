#!/usr/bin/env python3
"""
Batch-provision Slack apps for LabBots that don't yet have a bot token.

NOTE: single agents are now provisioned self-service from the admin approve page
(Provision button → web OAuth callback → token saved to AgentRegistry). Use this
script only for large batches.

How it works
------------
0. First run `scripts/export_agent_roster.py` IN THE CONTAINER to write
   data/agent_roster.json (this host script can't reach postgres directly).
1. Reads data/agent_roster.json to find active/pending bots without a token
2. Creates a Slack app for each via the Manifest API (apps.manifest.create)
3. Starts a local OAuth callback server on --port (default 8888)
4. Prints authorize URLs — a workspace admin clicks each one in a browser
5. Each click redirects back here; the code is exchanged for an xoxb- token
6. Tokens are appended to .env as SLACK_BOT_TOKEN_<AGENT_ID>; run
   scripts/backfill_agent_tokens.py in-container to copy them into the DB

Prerequisites (one-time, done by a workspace admin in a browser)
-----------------------------------------------------------------
  1. Go to https://api.slack.com/apps
  2. Click "Your App Configuration Tokens" → "Generate Token" for your workspace
  3. Copy both the token (xoxe-...) and the refresh token
  4. Add to .env:
       SLACK_CONFIG_TOKEN=xoxe-...
       SLACK_CONFIG_REFRESH_TOKEN=xoxe-...

Usage
-----
  # From project root:
  python scripts/provision_slack_bots.py

  # Custom port or env file:
  python scripts/provision_slack_bots.py --port 9000 --env-file .env

  # Preview what would be created without calling any APIs:
  python scripts/provision_slack_bots.py --dry-run

  # Re-run the OAuth step without recreating apps (useful if the server was
  # interrupted midway — re-uses credentials saved in .provision_state.json):
  python scripts/provision_slack_bots.py --skip-create
"""

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.table import Table

# This script runs on the HOST as `python3 scripts/provision_slack_bots.py`, so
# the project root isn't on sys.path by default. Add it so `src` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Shared provisioning helpers (transport-only; no heavy deps so this stays
# importable on the host). See src/services/slack_provisioning.py.
from src.services.slack_provisioning import (
    BOT_SCOPES,
    create_app,
    exchange_code,
    rotate_config_token,
)
from src.services.slack_provisioning import (
    lookup_team_id as _slack_lookup_team_id,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALLBACK_PATH = "/oauth/callback"
STATE_FILE = Path(".provision_state.json")

console = Console()


def _write_state(created: list[dict]) -> None:
    """Persist created-app credentials to STATE_FILE with owner-only perms.

    The file holds Slack app client_secrets, so it is chmod 0600 (was created
    world-readable at 0644) and is gitignored — never commit it. Written after
    each app is created so an interrupted run can be resumed with --skip-create
    without recreating apps or losing already-issued secrets. See SEC-9.
    """
    STATE_FILE.write_text(json.dumps(created, indent=2))
    try:
        STATE_FILE.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Load the agent roster from the DB export (data/agent_roster.json).
# The roster is produced in-container by scripts/export_agent_roster.py — this
# host script can't reach postgres directly. Only active/pending agents are
# provisionable (inactive/suspended don't need new tokens).
# ---------------------------------------------------------------------------

ROSTER_PATH = Path("data/agent_roster.json")
PROVISIONABLE_STATUSES = {"active", "pending"}


def load_roster() -> list[dict]:
    if not ROSTER_PATH.exists():
        raise RuntimeError(
            f"{ROSTER_PATH} not found. Generate it in the container first:\n"
            f"  docker compose exec app python scripts/export_agent_roster.py"
        )
    roster = json.loads(ROSTER_PATH.read_text())
    return [r for r in roster if r.get("status") in PROVISIONABLE_STATUSES]


# ---------------------------------------------------------------------------
# Slack API helpers
# ---------------------------------------------------------------------------
# create_app / exchange_code / rotate_config_token live in
# src/services/slack_provisioning.py (shared with the admin UI). The only
# script-local helper is the env-scanning team-id detector below.


def lookup_team_id(existing_env: dict) -> str | None:
    """Detect the workspace team_id from the first valid bot token in .env."""
    for key, val in existing_env.items():
        if (
            key.upper().startswith("SLACK_BOT_TOKEN_")
            and val
            and val.startswith("xoxb-")
            and not val.startswith("xoxb-placeholder")
        ):
            team_id = _slack_lookup_team_id(val)
            if team_id:
                return team_id
    return None


# ---------------------------------------------------------------------------
# OAuth callback HTTP server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Handles GET /oauth/callback?code=...&state=<agent_id>
    Exchanges the code for a token and writes it to .env.
    """

    # Shared state injected before server starts
    pending: dict = {}       # agent_id -> {bot_name, client_id, client_secret}
    received: dict = {}      # agent_id -> xoxb-token
    env_file: str = ".env"
    redirect_uri: str = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._html(404, "<h2>404 Not found</h2>")
            return

        params = dict(urllib.parse.parse_qsl(parsed.query))
        code = params.get("code")
        error = params.get("error")
        agent_id = params.get("state")

        if error:
            self._html(400, f"<h2>Slack returned an error: {error}</h2>")
            return

        if not code or not agent_id:
            self._html(400, "<h2>Missing code or state parameter</h2>")
            return

        info = self.pending.get(agent_id)
        if not info:
            self._html(400, f"<h2>Unknown agent_id in state: {agent_id!r}</h2>")
            return

        if agent_id in self.received:
            self._html(200, f"<h2>{info['bot_name']} already installed — duplicate callback ignored.</h2>")
            return

        try:
            token = exchange_code(
                info["client_id"], info["client_secret"], code, self.redirect_uri
            )
        except Exception as exc:
            console.print(f"[red]Token exchange failed for {agent_id}: {exc}[/red]")
            self._html(500, f"<h2>Token exchange failed: {exc}</h2>")
            return

        env_key = f"SLACK_BOT_TOKEN_{agent_id.upper()}"
        set_key(self.env_file, env_key, token, quote_mode="never")
        self.received[agent_id] = token

        remaining = len(self.pending) - len(self.received)
        console.print(f"[green]✓[/green] [bold]{info['bot_name']}[/bold] → {env_key}")
        self._html(200, f"""
            <h2 style="color:green">✅ {info['bot_name']} installed!</h2>
            <p>Token written to .env as <code>{env_key}</code></p>
            <p><b>{remaining}</b> bot(s) remaining. You may close this tab.</p>
        """)

    def _html(self, code: int, body: str):
        content = (
            "<html><body style='font-family:sans-serif;padding:2em;max-width:600px'>"
            + body
            + "</body></html>"
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass  # suppress default access log noise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int, default=8888,
        help="Local port for the OAuth callback server (default: 8888)",
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="Path to the .env file that will receive the new tokens (default: .env)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show which bots need tokens; make no API calls",
    )
    parser.add_argument(
        "--skip-create", action="store_true",
        help=f"Skip app creation and reuse credentials from {STATE_FILE}",
    )
    parser.add_argument(
        "--team-id",
        help="Slack workspace team ID (e.g. T012AB3CD) to pin OAuth URLs to the right workspace. "
             "Auto-detected from an existing bot token if not provided.",
    )
    parser.add_argument(
        "--omit-scope", action="append", default=[], metavar="AGENT_ID:SCOPE",
        help="Create AGENT_ID's app without SCOPE. Repeatable. The scope set is fixed "
             "at manifest time — Slack's consent screen has no per-scope choice — so "
             "this is the only way to install a bot deliberately missing one. The live "
             "tier needs it for wiseman: --omit-scope wiseman:groups:write",
    )
    parser.add_argument(
        "--only", nargs="+", metavar="AGENT_ID",
        help="Only provision these agent_id(s), e.g. --only good. "
             "Handy for testing the OAuth approval flow on a single bot.",
    )
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}{CALLBACK_PATH}"

    # -----------------------------------------------------------------------
    # 1. Determine which bots are missing tokens
    # -----------------------------------------------------------------------
    roster = load_roster()
    existing_env = dotenv_values(args.env_file)

    team_id = args.team_id
    if not team_id and not args.dry_run:
        team_id = lookup_team_id(existing_env)
        if team_id:
            console.print(f"Detected workspace team ID: [cyan]{team_id}[/cyan]")
        else:
            console.print("[yellow]Could not detect team ID — OAuth links may open the wrong workspace.[/yellow]")
            console.print("  Pass --team-id T... to fix this.")

    tokenized = {
        k[len("SLACK_BOT_TOKEN_"):].lower()
        for k, v in existing_env.items()
        if k.upper().startswith("SLACK_BOT_TOKEN_")
        and v
        and not v.startswith("xoxb-placeholder")
    }

    # An agent needs a token if it has neither a DB token (roster has_token) nor
    # a token already in this .env.
    missing = [
        lab for lab in roster
        if not lab.get("has_token") and lab["id"] not in tokenized
    ]

    omit: dict[str, set[str]] = {}
    for spec in args.omit_scope:
        aid, _, scope = spec.partition(":")
        if not aid or not scope:
            console.print(f"[red]--omit-scope needs AGENT_ID:SCOPE, got {spec!r}[/red]")
            raise SystemExit(2)
        omit.setdefault(aid.lower(), set()).add(scope)

    if args.only:
        only = {a.lower() for a in args.only}
        unknown = only - {lab["id"].lower() for lab in roster}
        if unknown:
            console.print(f"[yellow]--only: not in agent roster, ignoring: {', '.join(sorted(unknown))}[/yellow]")
        already = only & tokenized
        if already:
            console.print(f"[yellow]--only: already have tokens, will skip: {', '.join(sorted(already))}[/yellow]")
        missing = [lab for lab in missing if lab["id"].lower() in only]

    if not missing:
        console.print("[green]All bots already have tokens. Nothing to do.[/green]")
        return

    t = Table(title=f"{len(missing)} bot(s) need Slack tokens", show_lines=True)
    t.add_column("agent_id", style="cyan")
    t.add_column("Bot name")
    t.add_column("PI")
    for lab in missing:
        t.add_row(lab["id"], lab["name"], lab["pi"])
    console.print(t)

    if args.dry_run:
        console.print("[yellow]--dry-run active: no API calls made.[/yellow]")
        return

    # -----------------------------------------------------------------------
    # 2. Obtain / rotate config token
    # -----------------------------------------------------------------------
    config_token = existing_env.get("SLACK_CONFIG_TOKEN", "").strip()
    refresh_token = existing_env.get("SLACK_CONFIG_REFRESH_TOKEN", "").strip()

    # EITHER credential is enough. Requiring SLACK_CONFIG_TOKEN here was a bug: the
    # rotation below derives a fresh access token from the refresh token and
    # overwrites config_token unconditionally, so a refresh-token-only .env — the
    # normal state after a rotation, since rotation replaces both — was rejected
    # for want of a value the script was about to discard anyway.
    if not config_token and not refresh_token:
        console.print(
            "\n[bold red]Neither SLACK_CONFIG_TOKEN nor SLACK_CONFIG_REFRESH_TOKEN "
            "is set in .env[/bold red]"
        )
        console.print(
            "  1. Open https://api.slack.com/apps in a browser\n"
            "  2. Click 'Your App Configuration Tokens'\n"
            "  3. Click 'Generate Token' for your workspace\n"
            "  4. Copy BOTH values into .env. Note the prefixes differ:\n"
            "       SLACK_CONFIG_TOKEN=xoxe.xoxp-...   (access token, ~12h life)\n"
            "       SLACK_CONFIG_REFRESH_TOKEN=xoxe-1-...  (refresh token, single use)\n"
            "  The refresh token alone is sufficient — it mints the access token.\n"
        )
        sys.exit(1)

    if refresh_token:
        console.print("Rotating config token...")
        try:
            config_token, new_refresh, _exp = rotate_config_token(refresh_token)
            set_key(args.env_file, "SLACK_CONFIG_TOKEN", config_token, quote_mode="never")
            set_key(args.env_file, "SLACK_CONFIG_REFRESH_TOKEN", new_refresh, quote_mode="never")
            console.print("[green]Config token rotated and saved.[/green]")
        except Exception as exc:
            console.print(f"[yellow]Token rotation failed ({exc}); using existing token.[/yellow]")

    # Rotation can fail with a still-empty config_token — a refresh token that was
    # revoked, expired, or already spent (they are single use, so a value left in
    # .env after someone rotated it elsewhere is dead). Stop here rather than
    # calling apps.manifest.create with an empty Authorization header, which fails
    # with an opaque Slack error that says nothing about the real cause.
    if not config_token:
        console.print(
            "\n[bold red]No usable config token.[/bold red] The refresh token in "
            f"{args.env_file} did not rotate, and SLACK_CONFIG_TOKEN is empty."
        )
        console.print(
            "  Config refresh tokens are SINGLE USE: whoever rotated it last holds the\n"
            "  only live one, and a copy left behind in .env is already dead.\n"
            "  Generate a fresh pair at https://api.slack.com/apps -> "
            "'Your App Configuration Tokens'\n"
            "  and replace BOTH values in .env."
        )
        sys.exit(1)

    # -----------------------------------------------------------------------
    # 3. Start OAuth callback server (before app creation so URLs work immediately)
    # -----------------------------------------------------------------------
    _CallbackHandler.pending = {}
    _CallbackHandler.received = {}
    _CallbackHandler.env_file = args.env_file
    _CallbackHandler.redirect_uri = redirect_uri

    server = HTTPServer(("localhost", args.port), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    console.print(f"\n[bold]OAuth callback server running on http://localhost:{args.port}[/bold]")
    console.print(
        "\n[bold yellow]Open each URL in a browser while signed into the workspace.[/bold yellow]\n"
        "Each approval redirects back here and saves the token to .env automatically.\n"
    )

    # -----------------------------------------------------------------------
    # 4. Create apps (or load previous run's state) and print URLs as they appear
    # -----------------------------------------------------------------------
    def _oauth_url(app: dict) -> str:
        extra = {"state": app["agent_id"], "redirect_uri": redirect_uri}
        if team_id:
            extra["team"] = team_id
        return app["oauth_url"] + "&" + urllib.parse.urlencode(extra)

    created: list[dict] = []
    if args.skip_create:
        if not STATE_FILE.exists():
            console.print(f"[red]--skip-create: {STATE_FILE} not found. Run without that flag first.[/red]")
            server.shutdown()
            sys.exit(1)
        all_state: list[dict] = json.loads(STATE_FILE.read_text())
        missing_ids = {lab["id"] for lab in missing}
        created = [a for a in all_state if a["agent_id"] in missing_ids]
        console.print(f"Loaded {len(created)} app credential(s) from {STATE_FILE}\n")
        for i, app in enumerate(created, 1):
            _CallbackHandler.pending[app["agent_id"]] = {
                "bot_name": app["bot_name"],
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
            }
            console.print(f"  [cyan]{i:2d}.[/cyan] [bold]{app['bot_name']}[/bold] ({app['pi_name']})")
            console.print(f"      {_oauth_url(app)}\n")
    else:
        failed_count = 0
        for i, lab in enumerate(missing):
            try:
                dropped = omit.get(lab["id"].lower(), set())
                scopes = [x for x in BOT_SCOPES if x not in dropped] if dropped else None
                if dropped:
                    console.print(f"      [yellow]omitting scope(s) {sorted(dropped)} for {lab['id']}[/yellow]")
                app = create_app(
                    config_token, lab["id"], lab["name"], lab["pi"], redirect_uri,
                    scopes=scopes,
                )
                created.append(app)
                # Persist immediately (0600) so an interruption mid-run doesn't
                # lose the client_secret we just minted.
                _write_state(created)
                _CallbackHandler.pending[app["agent_id"]] = {
                    "bot_name": app["bot_name"],
                    "client_id": app["client_id"],
                    "client_secret": app["client_secret"],
                }
                console.print(f"  [green]{i+1:2d}.[/green] [bold]{app['bot_name']}[/bold] (app {app['app_id']})")
                console.print(f"      {_oauth_url(app)}\n")
            except Exception as exc:
                console.print(f"  [red]failed[/red]  {lab['name']}: {exc}")
                failed_count += 1
            # Slack's Manifest API allows ~10 req/min; 12s between calls stays well under
            if i < len(missing) - 1:
                time.sleep(12)

        if created:
            _write_state(created)
        if failed_count:
            console.print(f"[yellow]{failed_count} app(s) failed to create — fix errors and re-run.[/yellow]")

    if not created:
        console.print("[red]No apps available for OAuth. Exiting.[/red]")
        server.shutdown()
        sys.exit(1)

    # -----------------------------------------------------------------------
    # 5. Wait for all OAuth callbacks
    # -----------------------------------------------------------------------
    console.print(f"Waiting for {len(created)} installation(s)…  (Ctrl-C to stop early)\n")
    try:
        while len(_CallbackHandler.received) < len(created):
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    finally:
        server.shutdown()

    done = len(_CallbackHandler.received)
    total = len(created)
    console.print(f"\n[bold]Finished: {done}/{total} token(s) saved to {args.env_file}[/bold]")

    if done < total:
        outstanding = [a["bot_name"] for a in created if a["agent_id"] not in _CallbackHandler.received]
        console.print(f"[yellow]Still missing: {', '.join(outstanding)}[/yellow]")
        console.print("Re-run with [bold]--skip-create[/bold] to retry without recreating the apps.")
    else:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        # These names are NOT interchangeable with org1's. This host runs a second,
        # unrelated CoPI deployment (project `copi-python`) whose simulation container
        # is named `agent-run` — the UNPREFIXED name. `docker stop agent-run` /
        # `docker rm agent-run` would kill THAT deployment's production run. This repo's
        # container is `blackbird-agent-run`. Likewise `-f docker-compose.prod.yml` is
        # not optional: a bare `docker compose` resolves to the dev stack, whose web
        # service is `app`, while the deployed prod service is `blackbird-app`.
        console.print("[green]All done! Restart the agent container to pick up the new tokens.[/green]")
        console.print("  docker stop -t 30 blackbird-agent-run  # SIGTERM so the engine flushes")
        console.print("  docker rm blackbird-agent-run")
        console.print("  docker compose -f docker-compose.prod.yml up -d --build blackbird-app worker")
        console.print("  docker compose -f docker-compose.prod.yml --profile agent build agent")
        console.print(
            "  docker compose -f docker-compose.prod.yml --profile agent run -d "
            "--name blackbird-agent-run agent python -m src.agent.main"
        )


if __name__ == "__main__":
    main()
