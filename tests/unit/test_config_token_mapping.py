from src.config import Settings

#: Today's 124 agent ids, in the order the hand-written get_slack_tokens listed
#: them (frozen at the Phase 3 base).
FROZEN_KEYS = [
    "su", "wiseman", "lotz", "cravatt", "grotjahn", "petrascheck", "ken", "racki", "saez", "wu", "ward",
    "briney", "forli", "deniz", "lairson", "badran", "kern", "lasker", "lippi", "macrae", "maillie", "miller",
    "mravic", "paulson", "pwu", "seiple", "williamson", "wilson", "millar", "sali", "larabell", "zaro", "roe",
    "santi", "wells", "echeverria", "fraser", "craik", "stroud", "minor", "manglik", "susa", "capra", "kim",
    "azumaya", "nomura", "yeager", "moore", "young", "achatterjee", "bollong", "chatterjee", "chen", "chin",
    "ckim", "cliu", "cochran", "corey", "cornish", "diercks", "ding", "ellman", "good", "gray", "hsiehwilson",
    "johnsson", "lemke", "liu", "lyssiotis", "mehta", "pei", "pezacki", "schen", "schultz", "shao", "shokat",
    "ting", "wang", "williams", "winssinger", "wliu", "xiao", "yang", "mcnamara", "watanabe", "summerer",
    "vranken", "wemmer", "zhou", "dyoung", "brustad", "gan", "larman", "wurdak", "ulrich", "luesch", "ai",
    "gildersleeve", "mills", "xie", "guo", "liao", "jwang", "hogenesch", "lee", "alfonta", "meijler", "koh",
    "goto", "lin", "zuckermann", "rwang", "mehl", "cherry", "schiller", "santoro", "cropp", "scanlan",
    "xchen", "xwu", "zhang", "chang", "yliu", "magliery",
]


def test_mapping_keys_and_order_are_todays():
    assert list(Settings(_env_file=None).get_slack_tokens()) == FROZEN_KEYS


def test_values_follow_the_fields(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_WISEMAN", "xoxb-w")
    s = Settings(_env_file=None)
    assert s.get_slack_tokens()["wiseman"] == "xoxb-w"
    assert len(s.get_slack_tokens()) == 124
