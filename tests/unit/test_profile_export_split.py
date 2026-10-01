"""Differential: the split export_profile_to_markdown writes the same bytes as the frozen original."""

from types import SimpleNamespace

import pytest

from src.services import profile_export
from src.services.tenure_scope import TenureScopedPublications
from tests.unit._frozen_profile_export import frozen_export


def _pubs(n, tie_at=(5, 20)):
    items = []
    for i in range(n):
        year = 2024 - (i // 3)
        if i + 1 in tie_at:
            year = 2020
        items.append(
            SimpleNamespace(
                title=f"Title {i}." if i % 7 else "",
                journal="J" if i % 2 else None,
                year=year if i % 11 else None,
                doi=f"10.1/x{i}" if i % 3 else None,
                pmid=str(1000 + i) if i % 5 else None,
            )
        )
    return TenureScopedPublications(publications=tuple(items), tenure_start=None)


def _special_pubs():
    items = [
        SimpleNamespace(title="Tilde ~ *bold* [x](y) #h", journal="Science", year=2023,
                        doi="10.1038/s41586-1", pmid="1"),
        SimpleNamespace(title="Mismatch DOI", journal="Cell Reports", year=2022,
                        doi="10.1038/s41586-2", pmid=None),
        SimpleNamespace(title="Mismatch with pmid", journal="Cell Reports", year=2021,
                        doi="10.1038/s41586-3", pmid="3"),
        SimpleNamespace(title="No links...", journal=None, year=None, doi=None, pmid=None),
    ]
    return TenureScopedPublications(publications=tuple(items), tenure_start=2020)


_USERS = [
    SimpleNamespace(name="Ada Lab", institution="JHU", department="Bio"),
    SimpleNamespace(name="Bo", institution=None, department=None),
    SimpleNamespace(name="Ca~rl *Q* _x_", institution="", department="D|ept # 1"),
]
_PROFILES = [
    SimpleNamespace(research_summary="Sum", techniques=["a", "b"], experimental_models=["m"],
                    disease_areas=["d"], key_targets=["k"], keywords=["x", "y"], grant_titles=["G1"]),
    SimpleNamespace(research_summary="", techniques=[], experimental_models=None, disease_areas=[],
                    key_targets=None, keywords=[], grant_titles=None),
    SimpleNamespace(research_summary="~tilde~ **bold** `code`\n\n# heading", techniques=["T ~ 1", "*T2*"],
                    experimental_models=None, disease_areas=["a/b"], key_targets=[],
                    keywords=["k~1", "[k2]"], grant_titles=["R01 ~ x", "# G2"]),
    SimpleNamespace(research_summary=None, techniques=None, experimental_models=None, disease_areas=None,
                    key_targets=None, keywords=None, grant_titles=["only grants"]),
]
_PUBS = [None, _pubs(1), _pubs(19), _pubs(20), _pubs(21), _pubs(45), _special_pubs(),
         TenureScopedPublications(publications=(), tenure_start=None)]


@pytest.mark.parametrize("user", _USERS)
@pytest.mark.parametrize("profile", _PROFILES)
@pytest.mark.parametrize("pubs", range(len(_PUBS)))
def test_export_bytes_identical(tmp_path, monkeypatch, user, profile, pubs):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    publications = _PUBS[pubs]
    path = profile_export.export_profile_to_markdown(user, profile, "lab1", publications=publications)
    assert path.read_bytes() == frozen_export(user, profile, "lab1", publications=publications).encode("utf-8")


def test_empty_agent_id_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    assert profile_export.export_profile_to_markdown(_USERS[0], _PROFILES[0], "") is None
    assert list(tmp_path.iterdir()) == []


def test_bare_list_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    with pytest.raises(TypeError):
        profile_export.export_profile_to_markdown(_USERS[0], _PROFILES[0], "lab1", publications=[])
