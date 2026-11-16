"""The S6 specialist panel (spec §7.7 step 8, D12)."""
from src.landscaping import panel


def test_compose_persona_parity_with_tools():
    # Only this test imports src.agent.tools: landscaping code never does.
    from src.agent.tools import _load_persona_text

    for domain in panel.PANEL_CHEMISTRY:
        composed = panel.compose_persona(domain)
        assert composed == _load_persona_text(domain)
        assert "{stage_bar}" not in composed


def test_panel_domains_by_idea_type():
    assert panel.panel_domains("biology") == panel.PANEL_BIOLOGY
    assert panel.panel_domains("chemistry") == panel.PANEL_CHEMISTRY
    assert "technologic" not in panel.PANEL_BIOLOGY
    assert set(panel.DOMAIN_QUESTIONS) == set(panel.PANEL_CHEMISTRY)


def test_consult_message_carries_question_summary_and_dossier():
    message = panel.consult_message("legal", dossier="DOSSIER-TEXT", summary="SUMMARY-TEXT")
    assert panel.DOMAIN_QUESTIONS["legal"] in message
    assert "SUMMARY-TEXT" in message and "DOSSIER-TEXT" in message
