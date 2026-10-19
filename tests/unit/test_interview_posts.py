"""Post merging and the first-sentence rule behind the Interviews page."""

import uuid
from types import SimpleNamespace

from src.services.interview_posts import merge_chunks, posts_hash
from src.services.interview_trajectories import first_sentence


def row(agent, posted_at, content, phase="thread_reply"):
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent, sender_name=f"{agent}-name", phase=phase,
        posted_at=posted_at, content=content,
    )


def test_merge_rule_joins_only_same_author_chunks_under_two_seconds():
    rows = [
        row("lab", 0.0, "pitch", phase="new_post"), row("hub", 30.0, "A"),
        row("hub", 31.0, "B"), row("lab", 32.0, "C"), row("lab", 34.5, "D"),
    ]
    posts = merge_chunks(rows)
    assert [p.text for p in posts] == ["pitch", "A\n\nB", "C", "D"]
    assert posts[1].first_row_id == rows[1].id
    assert [p.kind for p in posts] == ["pitch", "reply", "reply", "reply"]


def test_posts_hash_is_twelve_hex_characters_of_the_id_list():
    posts = merge_chunks([row("lab", 0.0, "x"), row("hub", 10.0, "y")])
    assert len(posts_hash(posts)) == 12
    assert posts_hash(posts) != posts_hash(posts[:1])


def test_first_sentence_cuts_at_terminator_or_word():
    assert first_sentence("Thanks — I read it. Next question?") == "Thanks — I read it."
    assert first_sentence("word " * 60).endswith("…") and len(first_sentence("word " * 60)) <= 161


def test_first_sentence_drops_bold_markers_and_collapses_whitespace():
    assert first_sentence("**Bold**   claim\nhere!  More") == "Bold claim here!"
