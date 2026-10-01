from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "static/js/assessment_chat.js").read_text()


def test_input_is_disabled_while_busy():
    body = JS[JS.index("function setBusy(busy)"):]
    body = body[: body.index("}") + 1]
    assert "els.input.disabled = busy" in body
