"""Cohort manifest loading and validation — pure, no database.

The manifest is the only thing standing between a typo and a phantom cohort
member. `src.services.cohorts.compute_gates` treats a membership row naming an
agent with no AgentRegistry row as a valid allowed sender for every one of its
cohort-mates (its docstring records this was confirmed live with 56 such rows),
so an unknown agent_id must abort the seed, not warn about it.
"""

import pytest

from src.services.cohort_seed import (
    COHORT_NAME_RE,
    SeedPlan,
    load_manifest,
    plan_seed,
    validate_manifest,
)

REPO_MANIFEST = "cohorts.json"


def _manifest(**overrides):
    base = {
        "cohorts": {
            "alpha": {
                "description": "d",
                "source": "s",
                "members": ["su", "wiseman"],
            }
        }
    }
    base.update(overrides)
    return base


class TestLoadManifest:
    def test_loads_the_repo_manifest(self):
        m = load_manifest(REPO_MANIFEST)
        assert set(m["cohorts"]) == {
            "cabo-retreat",
            "schultz-reunion",
            "scripps-investigators",
        }

    def test_repo_manifest_has_the_expected_shape(self):
        m = load_manifest(REPO_MANIFEST)["cohorts"]
        assert len(m["cabo-retreat"]["members"]) == 34
        assert len(m["schultz-reunion"]["members"]) == 77
        assert len(m["scripps-investigators"]["members"]) == 37
        rows = sum(len(c["members"]) for c in m.values())
        distinct = {a for c in m.values() for a in c["members"]}
        assert rows == 148
        assert len(distinct) == 122

    def test_repo_manifest_carries_no_personal_data(self):
        """This repo is public (.gitignore:101). Agent IDs only."""
        raw = load_manifest(REPO_MANIFEST)
        for cohort in raw["cohorts"].values():
            for member in cohort["members"]:
                assert member.islower(), member
                assert " " not in member, member
                assert "@" not in member, member
                assert "0000-" not in member, member

    def test_bad_json_raises_value_error_naming_the_path(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        with pytest.raises(ValueError, match="broken.json"):
            load_manifest(p)


class TestValidateManifest:
    def test_valid_manifest_has_no_errors(self):
        assert validate_manifest(_manifest(), {"su", "wiseman"}) == []

    def test_repo_manifest_name_rule_matches_the_admin_form(self):
        for name in load_manifest(REPO_MANIFEST)["cohorts"]:
            assert COHORT_NAME_RE.match(name), name

    def test_unknown_agent_id_is_an_error(self):
        errors = validate_manifest(_manifest(), {"su"})
        assert len(errors) == 1
        assert "wiseman" in errors[0]
        assert "no AgentRegistry row" in errors[0]

    def test_missing_cohorts_key(self):
        assert validate_manifest({}, set()) == [
            "manifest has no non-empty 'cohorts' object"
        ]

    def test_empty_cohorts_object(self):
        assert validate_manifest({"cohorts": {}}, set()) == [
            "manifest has no non-empty 'cohorts' object"
        ]

    def test_bad_cohort_name(self):
        m = _manifest(cohorts={"Not Valid": {"description": "d", "source": "s",
                                             "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("must match" in e for e in errors)

    def test_blank_description_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "  ", "source": "s",
                                         "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("'description'" in e for e in errors)

    def test_blank_source_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "",
                                         "members": ["su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("'source'" in e for e in errors)

    def test_empty_member_list_is_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "s",
                                         "members": []}})
        errors = validate_manifest(m, set())
        assert any("non-empty list" in e for e in errors)

    def test_duplicate_members_are_an_error(self):
        m = _manifest(cohorts={"alpha": {"description": "d", "source": "s",
                                         "members": ["su", "su"]}})
        errors = validate_manifest(m, {"su"})
        assert any("duplicate members: su" in e for e in errors)

    def test_all_errors_are_reported_not_just_the_first(self):
        m = _manifest(cohorts={
            "alpha": {"description": "d", "source": "s", "members": ["ghost1"]},
            "beta": {"description": "d", "source": "s", "members": ["ghost2"]},
        })
        errors = validate_manifest(m, set())
        assert len(errors) == 2


class TestPlanSeed:
    def test_empty_database_creates_everything(self):
        plan = plan_seed(_manifest(), set(), set())
        assert plan.cohorts_to_create == ("alpha",)
        assert plan.memberships_to_add == (("alpha", "su"), ("alpha", "wiseman"))
        assert plan.extra_memberships == ()
        assert plan.is_noop is False

    def test_fully_seeded_database_is_a_noop(self):
        plan = plan_seed(
            _manifest(), {"alpha"}, {("alpha", "su"), ("alpha", "wiseman")}
        )
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == ()
        assert plan.is_noop is True

    def test_existing_cohort_missing_one_member(self):
        plan = plan_seed(_manifest(), {"alpha"}, {("alpha", "su")})
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == (("alpha", "wiseman"),)

    def test_db_only_membership_is_reported_as_extra_not_added(self):
        plan = plan_seed(
            _manifest(),
            {"alpha"},
            {("alpha", "su"), ("alpha", "wiseman"), ("alpha", "cravatt")},
        )
        assert plan.memberships_to_add == ()
        assert plan.extra_memberships == (("alpha", "cravatt"),)
        assert plan.is_noop is True  # extras alone are not work

    def test_unmanaged_cohort_is_left_entirely_alone(self):
        """A cohort the manifest does not name is none of the manifest's business."""
        plan = plan_seed(
            _manifest(),
            {"alpha", "hub-someone"},
            {("alpha", "su"), ("alpha", "wiseman"), ("hub-someone", "cravatt")},
        )
        assert plan.cohorts_to_create == ()
        assert plan.memberships_to_add == ()
        assert plan.extra_memberships == ()

    def test_results_are_sorted_and_deterministic(self):
        m = _manifest(cohorts={
            "zeta": {"description": "d", "source": "s", "members": ["wiseman", "su"]},
            "alpha": {"description": "d", "source": "s", "members": ["cravatt"]},
        })
        plan = plan_seed(m, set(), set())
        assert plan.cohorts_to_create == ("alpha", "zeta")
        assert plan.memberships_to_add == (
            ("alpha", "cravatt"), ("zeta", "su"), ("zeta", "wiseman"),
        )

    def test_plan_is_immutable(self):
        plan = plan_seed(_manifest(), set(), set())
        with pytest.raises(AttributeError):
            plan.cohorts_to_create = ()

    def test_repo_manifest_against_empty_db(self):
        plan = plan_seed(load_manifest(REPO_MANIFEST), set(), set())
        assert len(plan.cohorts_to_create) == 3
        assert len(plan.memberships_to_add) == 148
        assert isinstance(plan, SeedPlan)
