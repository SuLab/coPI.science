"""R1.10: AgentSlackClient escapes on the wire, re-splits after escaping, records the
source text, and only RunAnnouncer disables it (the 2026-10-15 publication-handling
design, C9, F10)."""
import ast
from collections import Counter
from pathlib import Path

from src.agent.slack_client import (
    AgentSlackClient,
    split_for_slack,
    split_for_wire,
    wire_text,
)
from src.agent.specialists import format_panel_note
from src.services.assessment_headline import render_assessment_headline
from tests.fakes import RecordingSlackClient

ROOT = Path(__file__).resolve().parents[2]
SICI = "10.1002/1097-0134(20010201)42:2<279::AID-PROT150>3.0.CO;2-U"


def _client(fake) -> AgentSlackClient:
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    c._client = fake
    c._bot_user_id = "U_SU"
    c._channel_name_to_id = {"general": "C_GENERAL"}
    return c


def _post(text: str, **kwargs):
    fake = RecordingSlackClient(responses={
        "chat_postMessage": {"ok": True, "ts": "1.1", "channel": "C_GENERAL"}})
    result = _client(fake).post_message("general", text, **kwargs)
    return fake.calls_to("chat_postMessage")[0]["text"], result


def test_post_one_sends_escaped_text_and_records_the_source():
    sent, result = _post("<!channel> R&D")
    assert sent == "&lt;!channel&gt; R&amp;D"
    assert result["posted_messages"][0]["text"] == "<!channel> R&D"


def test_a_chunk_that_grows_past_the_limit_is_resplit():
    text = "word " * 797 + "&&&&&"
    assert len(text) == 3990
    assert len(split_for_slack(text)) == 1
    chunks = split_for_wire(text)
    assert len(chunks) == 2
    assert all(len(wire_text(c)) <= 4000 for c in chunks)


def test_an_ampersand_flood_terminates_within_the_limit():
    chunks = split_for_wire("&" * 20000)
    assert all(len(wire_text(c)) <= 4000 for c in chunks)
    assert "".join(chunks) == "&" * 20000


def test_text_without_escapes_splits_as_before():
    fenced = "```\n" + "row = measure(sample)\n" * 400 + "```"
    for body in ("kinetics " * 600, "x" * 12000, fenced):
        assert split_for_wire(body) == split_for_slack(body)


def test_a_panel_note_question_is_escaped_on_the_wire():
    note = format_panel_note(domain="clinical", verdict_signal="gap",
                             question="<!channel> is this <b>real</b>?")
    sent, _ = _post(note)
    assert "&lt;!channel&gt;" in sent
    assert "&lt;b&gt;real&lt;/b&gt;" in sent


def test_headline_fields_are_escaped_on_the_wire():
    headline = render_assessment_headline(
        pi_label="Ana <b> & Co", project="<!channel> KRAS degrader", recommendation="advance",
        scores={}, permalink="https://example.org/a",
        elevator_pitch=f"<!here> see https://doi.org/{SICI} now",
    )
    sent, _ = _post(headline)
    assert sent.startswith(
        ":mag: Ana &lt;b&gt; &amp; Co — &lt;!channel&gt; KRAS degrader → *advance* — "
        "<https://example.org/a|View interview>\n&lt;!here&gt; see "
        "https://doi.org/10.1002/1097-0134(20010201)42:2%3C279::AID-PROT150%3E3.0.CO;2-U now"
    )


def test_escape_false_sends_mrkdwn_only():
    sent, _ = _post("<!here> R&D **x**", escape=False)
    assert sent == "<!here> R&D *x*"


_POSTERS = {"post_message", "apost_message"}


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def test_only_the_run_announcer_disables_escaping():
    """F10. The keyword scan is limited to calls of post_message, apost_message and
    _post_one: src/services/simulation_stats.py passes an unrelated escape= to
    SQLAlchemy's like(), which is not a posting call."""
    posting: Counter[str] = Counter()
    escapes: list[tuple[str, str, ast.expr]] = []
    keyed: list[tuple[str, int]] = []
    for base in ("src", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and _callee(node) in _POSTERS | {"_post_one"}:
                    if _callee(node) in _POSTERS:
                        posting[rel] += 1
                    escapes += [(rel, _callee(node), kw.value)
                                for kw in node.keywords if kw.arg == "escape"]
                if base != "src":
                    continue
                if isinstance(node, ast.Dict):
                    keyed += [(rel, k.lineno) for k in node.keys
                              if isinstance(k, ast.Constant) and k.value == "escape"]
                if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                        and node.slice.value == "escape"):
                    keyed.append((rel, node.lineno))
    outside = [e for e in escapes if e[0] != "src/agent/slack_client.py"]
    assert len(outside) == 1, outside
    rel, name, value = outside[0]
    assert (rel, name) == ("src/agent/engine/run_announcer.py", "apost_message")
    assert isinstance(value, ast.Constant) and value.value is False
    inside = [(name, value) for r, name, value in escapes if r == "src/agent/slack_client.py"]
    assert inside and all(
        name == "_post_one" and isinstance(value, ast.Name) and value.id == "escape"
        for name, value in inside
    )
    assert keyed == [], keyed
    # Control: the six posting calls (slack_io.py:406, headlines.py:194 and :535,
    # run_announcer.py:262, backfill_assessment_headlines.py:237 and :347).
    assert posting == Counter({
        "src/agent/engine/slack_io.py": 1,
        "src/agent/engine/headlines.py": 2,
        "src/agent/engine/run_announcer.py": 1,
        "scripts/backfill_assessment_headlines.py": 2,
    })
