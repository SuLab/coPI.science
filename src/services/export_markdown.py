"""Server-side markdown for the downloadable discussions export (C-02).

The HTML export is saved and opened from disk, where /static/js/markdown.js
cannot load, so its proposal summaries are rendered here. CommonMark with raw
HTML off (a tag in the text is escaped, never passed through — the page
renderer's rule, spec D3) and the image rule off (``![alt](url)`` renders as
"!" and a link, never an <img> that would fetch a remote URL when the file is
opened). Links are an allowlist, the page sanitizer's (static/js/markdown.js
PAGE_PURIFY): http(s), mailto and in-page fragments. markdown-it's own check is a
blocklist, and a file opened from disk has no CSP, so a relative link (it resolves
to file://) or a protocol handler such as search-ms: renders as plain text.

markdown-it-py is declared in pyproject.toml; it was already installed as a
dependency of rich.
"""

import re

from markdown_it import MarkdownIt
from markupsafe import Markup

_SAFE_LINK = re.compile(r"^(?:https?:|mailto:|#)", re.IGNORECASE)

_MD = MarkdownIt("commonmark", {"html": False}).disable("image")
_MD.validateLink = lambda url: bool(_SAFE_LINK.match(url.strip()))  # type: ignore[method-assign]


def render_export_markdown(text: str) -> Markup:
    """``text`` as HTML that is safe to emit unescaped in a Jinja template."""
    return Markup(_MD.render(text or ""))
