"""RSS feed builder for podcast episodes."""

import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIO_DIR = Path("data/podcast_audio")


def build_feed(agent_id: str, pi_name: str, episodes: list[Any], base_url: str) -> str:
    """Build an RSS 2.0 feed with iTunes extensions for the given agent's episodes.

    episodes: list of PodcastEpisode ORM objects, newest first.
    base_url: public base URL (e.g. https://copi.science)
    """
    feed_url = f"{base_url}/podcast/{agent_id}/feed.xml"
    items_xml = "\n".join(_build_item(ep, agent_id, base_url) for ep in episodes)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{_escape(pi_name)} — LabBot Research Briefings</title>
    <description>Daily personalized research summaries for {_escape(pi_name)} at Scripps Research.</description>
    <link>{_escape(feed_url)}</link>
    <language>en-us</language>
    <atom:link href="{_escape(feed_url)}" rel="self" type="application/rss+xml"/>
    <itunes:author>{_escape(pi_name)}</itunes:author>
    <itunes:category text="Science"/>
    <itunes:explicit>false</itunes:explicit>
{items_xml}
  </channel>
</rss>"""


def _build_item(ep: Any, agent_id: str, base_url: str) -> str:
    """Build a single RSS <item> for a PodcastEpisode."""
    date_str = ep.episode_date.isoformat()
    pub_date = format_datetime(
        datetime(ep.episode_date.year, ep.episode_date.month, ep.episode_date.day,
                 9, 0, 0, tzinfo=timezone.utc)
    )
    title = _escape(f"{ep.paper_title} — {date_str}")
    description = _escape(ep.text_summary)
    guid = f"{agent_id}-{date_str}"
    pmid_url = getattr(ep, "paper_url", None) or f"https://pubmed.ncbi.nlm.nih.gov/{ep.pmid}/"

    enclosure_xml = ""
    duration_xml = ""
    if ep.audio_file_path:
        audio_url = f"{base_url}/podcast/{agent_id}/audio/{date_str}.mp3"
        audio_path = Path(ep.audio_file_path)
        file_size = audio_path.stat().st_size if audio_path.exists() else 0
        enclosure_xml = (
            f'    <enclosure url="{_escape(audio_url)}" '
            f'type="audio/mpeg" length="{file_size}"/>'
        )
        if ep.audio_duration_seconds:
            mins, secs = divmod(ep.audio_duration_seconds, 60)
            duration_xml = f"    <itunes:duration>{mins}:{secs:02d}</itunes:duration>"

    return f"""  <item>
    <title>{title}</title>
    <description>{description}</description>
    <link>{_escape(pmid_url)}</link>
    <guid isPermaLink="false">{_escape(guid)}</guid>
    <pubDate>{pub_date}</pubDate>
{enclosure_xml}
{duration_xml}
  </item>"""


def _escape(text: str) -> str:
    """Escape XML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
