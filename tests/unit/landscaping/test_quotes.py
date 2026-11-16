"""The deterministic quote check (plan Task 4.2; spec §7.4 step 3, D67)."""
from src.landscaping.quotes import apply_quote_check, verify_quote

TEXT = (
    "Title: An invented kinase inhibitor.\n"
    "We show that “blocking KINX” reduces\n  tumour growth in mice. "
    "See doi 10.1234/inv.2 for the earlier screen."
)


def test_quote_normalization():
    assert verify_quote('"blocking kinx" reduces tumour growth', TEXT)
    assert verify_quote("REDUCES TUMOUR GROWTH IN MICE", TEXT)
    assert verify_quote("Ｂlocking", TEXT)  # full-width letter folds under NFKC
    assert not verify_quote("reduces tumor growth", TEXT)
    assert not verify_quote("", TEXT)
    assert not verify_quote("   ", TEXT)


def test_unverified_rubric_item_becomes_unclear():
    extraction = {
        "ideas": [{
            "target_rubric": [
                {"item": 1, "status": "present", "grade": "in_vivo",
                 "quote": "reduces tumour growth in mice", "section": "abstract"},
                {"item": 2, "status": "present", "grade": "clinical",
                 "quote": "a later trial confirmed it", "section": "abstract"},
                {"item": 3, "status": "absent", "grade": None, "quote": "", "section": ""},
            ],
            "key_results": [
                {"claim": "growth falls", "quote": "Reduces  tumour growth", "section": "abstract"},
                {"claim": "made up", "quote": "survival doubled", "section": "abstract"},
            ],
            "novelty_claims": [{"quote": "", "section": ""}],
            "limitations": [],
            "unmet_need_statements": [{"quote": "an invented need", "section": "abstract"}],
        }],
        "linked_publications": [
            {"doi": "10.1234/inv.2", "pmid": "", "title": ""},
            {"doi": "10.1234/inv.9", "pmid": "", "title": ""},
            {"doi": "", "pmid": "", "title": ""},
        ],
    }

    cleaned, verified, dropped = apply_quote_check(extraction, TEXT)

    rubric = cleaned["ideas"][0]["target_rubric"]
    assert rubric[0]["status"] == "present" and rubric[0]["grade"] == "in_vivo"
    assert rubric[1] == {"item": 2, "status": "unclear", "grade": None, "quote": "",
                         "section": "abstract"}
    assert rubric[2]["status"] == "unclear"
    assert [r["claim"] for r in cleaned["ideas"][0]["key_results"]] == ["growth falls"]
    assert cleaned["ideas"][0]["novelty_claims"] == []
    assert cleaned["ideas"][0]["unmet_need_statements"] == []
    assert cleaned["linked_publications"] == [{"doi": "10.1234/inv.2", "pmid": "", "title": ""}]
    assert (verified, dropped) == (2, 3)
    # the input is not mutated
    assert extraction["ideas"][0]["target_rubric"][1]["status"] == "present"
