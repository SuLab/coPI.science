"""B-06: while an answer streams the question box is read-only, not disabled (a disabled
control drops focus to <body>), says so with aria-busy, and gets focus back when the
answer finishes or is abandoned. JS has no runner here; these pin the source, and the
harness journey `journey_chat_focus_after_answer` exercises it in a browser."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "static/js/assessment_chat.js").read_text()


def _function_body(name: str) -> str:
    start = JS.index(f"function {name}(")
    open_at = JS.index("{", start)
    depth = 0
    for i in range(open_at, len(JS)):
        if JS[i] == "{":
            depth += 1
        elif JS[i] == "}":
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_input_is_read_only_not_disabled_while_busy():
    body = _function_body("setBusy")
    assert "els.input.readOnly = busy;" in body
    assert 'els.input.setAttribute("aria-busy", "true");' in body
    assert 'els.input.removeAttribute("aria-busy");' in body
    assert "els.input.disabled" not in body


def test_focus_returns_when_the_answer_finishes_or_is_abandoned():
    assert "els.input.focus(" in _function_body("refocusInput")
    ask = _function_body("ask")
    abandon = ask[ask.index("function abandon("):]
    abandon = abandon[: abandon.index("}") + 1]
    assert "refocusInput();" in abandon
    after_stream = ask[ask.index("await readStream(resp, handlers);"):]
    assert after_stream.index("setBusy(false);") < after_stream.index("refocusInput();")
