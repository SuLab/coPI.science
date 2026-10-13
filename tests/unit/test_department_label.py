"""_department_label folds free-text users.department into Scripps departments."""

import pytest

from src.routers.public import (
    _DEPARTMENT_UNKNOWN,
    _department_label,
    _department_legend,
)

ISCB = "Integrative Structural & Computational Biology"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Every ISCB spelling present in prod users.department.
        ("Department of Integrative Structural and Computational Biology", ISCB),
        ("Department of Integrative Structural & Computational Biology (ISCB)", ISCB),
        ("Integrative Structural and Computational Biology", ISCB),
        ("Integrative, Structural and Computational Biology", ISCB),
        ("Integrative Structural & Computational Biology", ISCB),
        ("Chemistry", "Chemistry"),
        ("Department of Immunology and Microbiology", "Immunology & Microbiology"),
        ("Molecular and Cellular Biology", "Molecular & Cellular Biology"),
        ("Department of Molecular and Cellular Biology", "Molecular & Cellular Biology"),
        ("Department of Molecular Medicine", "Molecular Medicine"),
        ("Molecular Medicine", "Molecular Medicine"),
        # Two departments listed: the Molecular Medicine one is the real match.
        (
            "Molecular and Cellular Medicine and Dept of Molecular Medicine",
            "Molecular Medicine",
        ),
        ("Department of Molecular Experimental Medicine", "Molecular & Experimental Medicine"),
        ("Calibr-Skaggs Institute for Innovative Medicines", "Calibr-Skaggs"),
        ("Neuroscience", "Neuroscience"),
        ("Artificial Intelligence Enablement", "AI Enablement"),
        ("Department of Something New", "Something New"),
        (None, _DEPARTMENT_UNKNOWN),
        ("   ", _DEPARTMENT_UNKNOWN),
    ],
)
def test_department_label(raw, expected):
    assert _department_label(raw) == expected


def test_legend_keeps_singletons_and_puts_unknown_last():
    nodes = [
        {"institution": "Chemistry"},
        {"institution": _DEPARTMENT_UNKNOWN},
        {"institution": ISCB},
        {"institution": ISCB},
        {"institution": "Something New"},
    ]
    legend, color_map = _department_legend(nodes)
    assert [item["label"] for item in legend] == [
        ISCB, "Chemistry", "Something New", _DEPARTMENT_UNKNOWN,
    ]
    assert len(set(color_map.values())) == len(color_map)
