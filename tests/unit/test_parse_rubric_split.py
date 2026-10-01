"""The split parse_rubric yields an EQUAL Rubric for the live document and raises
the same error as the frozen original for broken ones."""
import pytest

from src.services import blackbird_rubric as br
from tests.unit._frozen_parse_rubric import frozen_parse_rubric


def test_live_rubric_is_equal():
    assert br.parse_rubric(br.RUBRIC_PATH) == frozen_parse_rubric(br.RUBRIC_PATH)
    assert repr(br.parse_rubric(br.RUBRIC_PATH)) == repr(frozen_parse_rubric(br.RUBRIC_PATH))
    assert br.parse_rubric(br.RUBRIC_PATH) == br.load_rubric()


def _broken_docs():
    live = br.RUBRIC_PATH.read_text(encoding="utf-8")
    return [
        "not toml = = =",
        "[meta]\nversion = 1\n",
        live.replace("advance_min", "advance_minimum", 1),
        live.replace("[[dimension]]", "[[dimensionx]]", 1),
        live.replace("[stage_bar_global]", "[stage_bar_globalx]", 1),
        live.replace("[gating]", "[gatingx]", 1),
        live.replace("[scale]", "[scalex]", 1),
        live.replace("[red_flags]", "[red_flagsx]", 1),
    ]


def _outcome(fn, path):
    try:
        return ("ok", repr(fn(path)))
    except Exception as exc:
        return (type(exc).__name__, str(exc))


@pytest.mark.parametrize("text", _broken_docs())
def test_errors_are_todays(tmp_path, text):
    path = tmp_path / "r.toml"
    path.write_text(text, encoding="utf-8")
    assert _outcome(br.parse_rubric, path) == _outcome(frozen_parse_rubric, path)


def test_unreadable_path_is_todays(tmp_path):
    path = tmp_path / "missing.toml"
    assert _outcome(br.parse_rubric, path) == _outcome(frozen_parse_rubric, path)
