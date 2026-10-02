"""Server-side markdown for the downloadable discussions export (C-02).

The HTML export is saved and opened from disk, where /static/js/markdown.js
cannot load, so its proposal summaries are rendered here. CommonMark with raw
HTML off (a tag in the text is escaped, never passed through — the page
renderer's rule, spec D3) and the image rule off (``![alt](url)`` renders as
"!" and a link, never an <img> that would fetch a remote URL when the file is
opened). markdown-it's own link validation drops javascript:, vbscript:, file:
and non-image data: URLs.

markdown-it-py is declared in pyproject.toml; it was already installed as a
dependency of rich.
"""

from markdown_it import MarkdownIt
from markupsafe import Markup

_MD = MarkdownIt("commonmark", {"html": False}).disable("image")


def render_export_markdown(text: str) -> Markup:
    """``text`` as HTML that is safe to emit unescaped in a Jinja template."""
    return Markup(_MD.render(text or ""))
