"""X-02 template scan: every form control in templates/ has an accessible name.

Static and template-level, so it also covers branches a seeded page does not render; the
rendered-page gate (tests/integration/test_rendered_page_gate.py) checks real pages.
Jinja tags are blanked before parsing, so a control inside {% if %} is still seen."""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SKIP_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_JINJA_STMT = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels: list[dict] = []
        self.label_for: set[str] = set()
        self.controls: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "label":
            self.labels.append({"for": a.get("for"), "text": [], "controls": []})
        elif tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "text").lower() in _SKIP_TYPES:
                return
            self.controls.append({"tag": tag, "a": a, "line": self.getpos()[0], "wrapped": False})
            for label in self.labels:
                label["controls"].append(len(self.controls) - 1)

    def handle_endtag(self, tag):
        if tag == "label" and self.labels:
            label = self.labels.pop()
            if "".join(label["text"]).strip():
                for i in label["controls"]:
                    self.controls[i]["wrapped"] = True
                if label["for"]:
                    self.label_for.add(label["for"])

    def handle_data(self, data):
        for label in self.labels:
            label["text"].append(data)


def _unnamed(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Keep line numbers: replace each Jinja construct with the same number of newlines.
    text = _JINJA_STMT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _JINJA_EXPR.sub(lambda m: "X" + "\n" * m.group(0).count("\n"), text)
    scan = _Scan()
    scan.feed(text)
    out = []
    for c in scan.controls:
        a = c["a"]
        if a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip():
            continue
        if c["wrapped"] or (a.get("id") and a["id"] in scan.label_for):
            continue
        out.append(f"{path.relative_to(ROOT)}:{c['line']}: <{c['tag']} name={a.get('name')!r}>")
    return out


def test_every_template_control_has_an_accessible_name():
    found = [msg for path in sorted((ROOT / "templates").rglob("*.html")) for msg in _unnamed(path)]
    assert not found, "\n".join(found)
