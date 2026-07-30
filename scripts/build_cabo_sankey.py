"""Sankey funnel: top-level posts → threads → outcomes, for one simulation run window.

Pulls numbers live from Postgres so it can be re-run as the simulation
progresses. Parameterized by window start date so it serves any run window that
shares the single resumed simulation_run_id (date is the only way to isolate a
window — see the window constants in src/routers/public.py).

Run inside the app container (scripts/ isn't mounted — docker cp it in first):
  docker cp scripts/build_cabo_sankey.py copi-python-app-1:/app/scripts/

  # Cabo run (defaults):
  docker exec copi-python-app-1 python scripts/build_cabo_sankey.py

  # Schultz alumni reunion window:
  docker exec copi-python-app-1 python scripts/build_cabo_sankey.py \
      --start 2026-06-06 --out /app/data/schultz_viz --label "Schultz Alumni reunion run"

Output (sankey.html + sankey.png) lands in --out inside the container; retrieve
with `docker cp copi-python-app-1:/app/data/schultz_viz ./data/`.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
from sqlalchemy import text

from src.database import get_session_factory

# Defaults preserve the original Cabo behavior.
DEFAULT_START = "2026-05-01"
DEFAULT_OUT = "/app/data/cabo_viz"
DEFAULT_LABEL = "40-PI Cabo run"


def _hex_to_rgba(h: str, a: float) -> str:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:], 16)
    return f"rgba({r},{g},{b},{a})"


async def fetch_counts(start: datetime) -> dict[str, int]:
    sql = text(
        """
        WITH posts AS (
          SELECT message_ts FROM agent_messages
          WHERE phase='new_post' AND created_at >= :start
            AND message_ts IS NOT NULL
        ),
        replies AS (
          SELECT DISTINCT thread_ts FROM agent_messages
          WHERE thread_ts IS NOT NULL AND created_at >= :start
        ),
        sparked AS (
          SELECT p.message_ts FROM posts p
          WHERE p.message_ts IN (SELECT thread_ts FROM replies)
        ),
        decisions AS (
          SELECT DISTINCT ON (thread_id) thread_id, outcome
          FROM thread_decisions
          WHERE decided_at >= :start
          ORDER BY thread_id, decided_at DESC
        )
        SELECT
          (SELECT COUNT(*) FROM posts)                                 AS posts,
          (SELECT COUNT(*) FROM sparked)                               AS sparked,
          (SELECT COUNT(*) FROM posts) - (SELECT COUNT(*) FROM sparked) AS silent,
          COUNT(*) FILTER (WHERE d.outcome='proposal')                 AS proposal,
          COUNT(*) FILTER (WHERE d.outcome='no_proposal')              AS no_proposal,
          COUNT(*) FILTER (WHERE d.outcome='timeout')                  AS timeout,
          COUNT(*) FILTER (WHERE d.outcome IS NULL)                    AS active,
          (SELECT COUNT(*) FROM agent_messages
             WHERE phase='thread_reply' AND created_at >= :start)      AS replies
        FROM sparked s
        LEFT JOIN decisions d ON d.thread_id = s.message_ts;
        """
    )
    async with get_session_factory()() as session:
        row = (await session.execute(sql, {"start": start})).mappings().one()
        return dict(row)


def build(c: dict[str, int], out: Path, label: str, start: datetime) -> None:
    out.mkdir(parents=True, exist_ok=True)

    labels = [
        f"Top-level posts\n({c['posts']})",        # 0
        f"Sparked a thread\n({c['sparked']})",     # 1
        f"No replies\n({c['silent']})",            # 2
        f"Proposal drafted\n({c['proposal']})",    # 3
        f"No proposal\n({c['no_proposal']})",      # 4
        f"Timed out\n({c['timeout']})",            # 5
        f"Still active\n({c['active']})",          # 6
    ]
    src = [0, 0, 1, 1, 1, 1]
    tgt = [1, 2, 3, 4, 5, 6]
    val = [c["sparked"], c["silent"],
           c["proposal"], c["no_proposal"], max(c["timeout"], 0), c["active"]]
    link_colors = ["#4C72B0", "#BBB",
                   "#55A868", "#C44E52", "#8172B2", "#937860"]
    node_colors = ["#34495E",
                   "#4C72B0", "#BBB",
                   "#55A868", "#C44E52", "#8172B2", "#937860"]
    subtitle = (f"{c['posts']} top-level posts · {c['replies']} thread replies · "
                f"{label} from {start:%Y-%m-%d}")

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(pad=24, thickness=24,
                  line=dict(color="white", width=0.5),
                  label=labels, color=node_colors),
        link=dict(source=src, target=tgt, value=val,
                  color=[_hex_to_rgba(lc, 0.45) for lc in link_colors])))
    fig.update_layout(
        title=dict(text=(
            f"<b>From post to outcome — {label}</b><br>"
            f"<span style='font-size:12px;color:#555'>{subtitle}</span>")),
        font=dict(size=12), height=560, width=1200,
        paper_bgcolor="white",
        margin=dict(l=20, r=20, t=95, b=20))
    html_path = out / "sankey.html"
    fig.write_html(html_path, include_plotlyjs="cdn")
    try:
        fig.write_image(out / "sankey.png", width=1400, height=750, scale=2)
    except Exception as e:
        print(f"(skipping PNG export: {e})")
    print(f"✓ wrote {html_path}")
    print(f"  {c}")


def _parse_start(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"Run-window start date (ISO, UTC). Default {DEFAULT_START}.")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"Output dir (inside container). Default {DEFAULT_OUT}.")
    ap.add_argument("--label", default=DEFAULT_LABEL,
                    help=f'Run label for titles. Default "{DEFAULT_LABEL}".')
    args = ap.parse_args()
    start = _parse_start(args.start)
    counts = await fetch_counts(start)
    build(counts, Path(args.out), args.label, start)


if __name__ == "__main__":
    asyncio.run(main())
