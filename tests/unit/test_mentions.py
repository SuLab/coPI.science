"""GATE-8 lab mentions (spec 2026-10-15 §11 R6.1)."""
from src.agent.mentions import RosterEntry, find_lab_mentions

R = (
    RosterEntry("good", "GoodBot", "Benjamin Good"),
    RosterEntry("wu", "WuBot", "Chunlei Wu"),
    RosterEntry("su", "SuBot", "Andrew Su"),
    RosterEntry("chin", "ChinBot", "Jason Chin"),
    RosterEntry("blackbird", "BlackbirdBot", None),
)


def _one(text, roster=R, uid_map=None):
    found = find_lab_mentions(text, roster, uid_map)
    assert len(found) == 1, found
    return found[0]


def test_slack_uid_mention_resolves_through_the_map():
    m = _one("<@U123> thanks", uid_map={"U123": "su"})
    assert (m.kind, m.agent_ids) == ("slack_uid", frozenset({"su"}))


def test_unknown_uid_is_unresolved_not_guessed():
    assert _one("<@UZZZ> hi", uid_map={"U123": "su"}).agent_ids == frozenset()


def test_bot_names_match_in_any_case():
    for text in ("@SUBOT", "subot", "@SuBot"):
        m = _one(text)
        assert (m.kind, m.agent_ids) == ("bot_name", frozenset({"su"})), text


def test_full_pi_name_with_title_initial_and_suffix():
    m = _one("the lab of Dr. Andrew I. Su")
    assert (m.kind, m.agent_ids, m.start) == ("pi_name", frozenset({"su"}), 0)
    m = _one("Andrew Su's group")
    assert (m.kind, m.agent_ids) == ("pi_name", frozenset({"su"}))


def test_coordinated_labs_give_one_mention_per_name():
    found = find_lab_mentions("the Good and Su labs", R)
    assert [(m.kind, m.agent_ids) for m in found] == [
        ("lab_group", frozenset({"good"})), ("lab_group", frozenset({"su"})),
    ]


def test_bare_surname_and_et_al_are_not_mentions():
    assert find_lab_mentions("Su et al. showed it", R) == []
    assert find_lab_mentions("as Su noted", R) == []


def test_titled_surname():
    m = _one("Prof. Chin agrees")
    assert (m.kind, m.agent_ids) == ("titled_surname", frozenset({"chin"}))


def test_coauthor_only_forms_need_a_coauthor_stem():
    m = _one("B. Good co-authored it")
    assert (m.kind, m.agent_ids) == ("coauthor_surname", frozenset({"good"}))
    assert find_lab_mentions("B. Good presented it", R) == []


def test_namesakes_share_one_mention():
    m = _one("the Wu lab", roster=(*R, RosterEntry("pwu", "PwuBot", "Peng Wu")))
    assert m.agent_ids == frozenset({"wu", "pwu"})


def test_hub_title_cased_agent_id():
    m = _one("the Blackbird team")
    assert (m.kind, m.agent_ids) == ("lab_group", frozenset({"blackbird"}))


def test_stopwords_are_never_labs():
    assert find_lab_mentions("the Other lab", R) == []
    assert find_lab_mentions("Our lab", R) == []


def test_fold_keeps_spans_aligned():
    text = "the Müller lab"
    m = _one(text, roster=(RosterEntry("muller", "MullerBot", "Anna Müller"),))
    assert m.agent_ids == frozenset({"muller"})
    assert text[m.start:m.end] == m.text
