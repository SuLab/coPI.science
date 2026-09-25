"""Render-time rewriting of URLs in assessment prose into "cited paper" links.

Why render-time and not write-time: ``opportunity_assessments`` stores what the
hub wrote, and the Slack headline
(``src/services/assessment_headline.py::render_assessment_headline``) publishes
the RAW ``elevator_pitch`` clipped at a sentence boundary. Rewriting the stored
text would move that clip boundary and change what is posted to a channel this
change is not allowed to touch.

Two functions because the app has two render paths, chosen per row by the
write-time ``prose_format`` stamp:

* ``markdown_with_citation_links`` feeds ``data-markdown``, which
  ``static/js/markdown.js`` parses with marked and sanitizes with DOMPurify.
  It emits MARKDOWN, not HTML.
* ``plain_with_citation_links`` feeds a ``whitespace-pre-line`` block and is
  the only function here that produces HTML. It returns ``Markup``, so a
  template renders it unescaped — which is safe only because this function
  escapes every non-URL span itself.

Measured 2026-09-21 against CDN bundles whose sha384 matches the SRI pins in
all four assessment wrappers, so these are the builds production serves:

* marked 12.0.2 with this repo's ``del``-disabled config already autolinks a
  bare URL and already leaves an unbalanced trailing ``)`` outside the link.
  What it cannot do is change the link TEXT, which is the whole request.
* DOMPurify 3.1.6's default ``ALLOWED_ATTR`` does **not** include ``target``.
  So the anchor carries no ``target``: one would survive on the plain path
  and be stripped on the markdown path, and the two renderings would
  disagree. Keeping it would mean widening the shared sanitizer for every
  markdown surface in the app, including the interview transcript.
  ``rel="noreferrer"`` is a different case and IS emitted on the plain path:
  it has no visible behaviour, so the two paths cannot be seen to differ, and
  it keeps the assessment's own URL out of the ``Referer`` sent to whatever
  host the hub cited. Markdown has no syntax for ``rel``, so a
  ``prose_format='markdown'`` row's links fall back to the browser's own
  referrer policy.

Known and accepted (2026-09-21 security review): the label is identical for
every destination, so a reader cannot tell a DOI from any other host without
hovering. That is the operator's explicit request — a hyperlink reading
"cited paper" *instead of* the full URL — and the URL is kept in ``title``
rather than discarded.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from markupsafe import Markup, escape

#: The visible text of every rewritten link. Identical for every URL: two
#: citations in one paragraph are told apart by the ``title`` (the full URL),
#: not by a number this module would have to invent.
CITATION_LABEL = "cited paper"

#: Conservative on purpose. ``http``/``https`` only — a false link on a staff
#: reviewing surface is worse than a visible URL, so ``mailto:``, bare ``www.``
#: and scheme-less DOIs are left as text. The excluded characters are the ones
#: that would break the markdown destination (``<``, ``>``), the title (``"``),
#: a code span (backtick), or — the subtle one — the CommonMark escape: a
#: trailing ``\`` escapes the destination's closing ``>`` and then the title's
#: closing ``"``, so the whole inline link fails to parse and marked renders
#: the raw ``[cited paper](<https://…`` to the reader. That is precisely the
#: failure the angle brackets exist to prevent.
_URL_RE = re.compile(r'https?://[^\s<>"`\\]+')

#: Spans the markdown rewriter must not touch: fenced code, code spans,
#: autolinks, and inline links/images. Ordered longest-construct-first so a
#: fence is not eaten as a code span. A link destination containing an
#: unescaped parenthesis is not matched here and will be treated as free text;
#: the hub writes bare URLs rather than markdown links, so that shape does not
#: occur in the corpus, and mis-rewriting it would produce a visible link
#: rather than a silent loss.
#:
#: The angle-destination alternative is written to have NO two adjacent
#: ``\s*`` around an optional group. The obvious form,
#: ``\s*<[^>]*>\s*(?:"[^"]*")?\s*\)``, splits a whitespace run O(n) ways when
#: the title is absent and re-scans each: measured 0.046s for
#: ``"[](<>" + " " * 5000`` and 0.802s for 20000 — on a field the hub can
#: write to ~5000 characters, rendered once per card on a page that renders up
#: to 500 cards. Pulling the trailing ``\s*`` INSIDE the optional group leaves
#: exactly one star on each path through it and keeps the no-whitespace title
#: form ``[t](<u>"ti")`` matched, which a ``\s+`` would have dropped out of the
#: protected set — turning a construct that was previously left alone into
#: free text whose inner URL gets rewritten.
_MD_PROTECTED_RE = re.compile(
    r"```.*?```"
    r"|`[^`]*`"
    r"|<https?://[^>\s]+>"
    r"|!?\[[^\]]*\]\(\s*<[^>]*>\s*(?:\"[^\"]*\"\s*)?\)"
    r"|!?\[[^\]]*\]\([^()\s]*(?:\s+\"[^\"]*\")?\)",
    re.S,
)

#: An inline link, for the one protected shape that IS rewritten: a link whose
#: visible text is its own destination. The hub wrote no link text there, so
#: there is nothing deliberate to preserve.
_MD_LINK_RE = re.compile(
    r"^(!?)\[([^\]]*)\]\(\s*<?([^()\s>]*)>?\s*(?:\"[^\"]*\")?\s*\)$"
)

#: Trailing characters that belong to the sentence, not the URL.
_SENTENCE_TRAILING = ".,;:!?'"


def _truncated_at_backslash(span: str, end: int) -> bool:
    """Was this match cut short by a ``\\`` rather than ending naturally?

    ``\\`` is outside ``_URL_RE`` because it escapes the markdown
    destination's closing ``>``. That exclusion also means a URL that
    genuinely contains one is matched only up to it — and linkifying the
    truncated half hides the truncation behind the label, which is worse than
    the visible URL it replaced. So a match followed immediately by a
    backslash is left as text on both paths.
    """
    return end < len(span) and span[end] == "\\"


def _split_trailing(url: str) -> tuple[str, str]:
    """Peel sentence punctuation off the end of a matched URL.

    A ``)`` is peeled ONLY when it is unbalanced, which is marked's own GFM
    rule and the reason ``https://doi.org/10.1002/(SICI)…`` survives intact
    while ``(https://doi.org/x)`` does not swallow its bracket.
    """
    trail = ""
    while url:
        last = url[-1]
        if last in _SENTENCE_TRAILING:
            url, trail = url[:-1], last + trail
        elif last == ")" and url.count(")") > url.count("("):
            url, trail = url[:-1], last + trail
        else:
            break
    return url, trail


def _is_linkable(url: str) -> bool:
    """Is this a URL worth turning into a "cited paper" link?

    Two rejections, both of which produced a link that lies:

    * anything ``_URL_RE`` would not have matched in free text. The free-text
      path is scheme-checked; the inline-link path was not, so
      ``[mailto:pi@jhu.edu](mailto:pi@jhu.edu)``, ``[/admin/users/x](/admin/users/x)``
      and ``[javascript:alert1](javascript:alert1)`` all became "cited paper".
      The last one is not XSS today only because DOMPurify's
      ``ALLOWED_URI_REGEXP`` rejects the scheme — i.e. the label had replaced
      the one signal a reader had, and a CDN bundle was the only defence left.
    * a bare scheme with no host. ``_split_trailing`` can peel a match down to
      ``https://`` ("the endpoint https://, which we saw"), and a dead link
      presented as a citation is worse than the text it replaced.
    """
    if not _URL_RE.fullmatch(url):
        return False
    # A real host, not merely a non-empty remainder: `https://?q=1` and
    # `https:///doi.org/x` both have text after the `://` and neither has an
    # authority, so a `strip("/")` check passed them straight through to the
    # "cited paper" label — the same dead-link-presented-as-a-citation this
    # gate exists to stop.
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        # urlsplit raises on a malformed IPv6 literal, e.g. `https://[::1`.
        return False


def _markdown_link(url: str) -> str:
    """``[cited paper](<url> "url")``.

    Angle-bracketed because a bare destination terminates at the first ``)``,
    and a real DOI can contain one — marked then renders the literal text
    ``[cited paper](https://…`` to the reader. ``_URL_RE`` already guarantees
    the URL holds no ``<``, ``>`` or ``"``, so nothing further needs escaping
    inside either the destination or the title.
    """
    return f'[{CITATION_LABEL}](<{url}> "{url}")'


def _rewrite_protected(span: str) -> str:
    """Return a protected span verbatim, except a self-titled inline link."""
    match = _MD_LINK_RE.match(span)
    if (
        match
        and not match.group(1)
        and match.group(2) == match.group(3)
        and _is_linkable(match.group(3))
    ):
        return _markdown_link(match.group(3))
    return span


def _link_free_span(span: str) -> str:
    """Rewrite every bare URL in a span known to carry no markdown link."""
    if not span:
        return span
    out: list[str] = []
    pos = 0
    for match in _URL_RE.finditer(span):
        url, trail = _split_trailing(match.group())
        if not _is_linkable(url) or _truncated_at_backslash(span, match.end()):
            continue
        out.append(span[pos : match.start()])
        out.append(_markdown_link(url))
        out.append(trail)
        pos = match.end()
    out.append(span[pos:])
    return "".join(out)


def markdown_with_citation_links(text: object) -> object:
    """Rewrite every bare URL in ``text`` as a ``cited paper`` markdown link.

    Idempotent: a second pass sees only links whose visible text is
    ``cited paper``, which are left alone.

    A non-string is returned unchanged rather than raising. Several call sites
    feed these helpers raw JSONB elements (``key_points`` bullets, and the
    hub's ``strengths``/``risks``/``competitive_landscape``/
    ``evidence_maturity``), and the templates around them are
    deliberately defensive about that column's shape — a model-written bullet
    that arrives as a number used to render as text and must not start
    500ing the page.
    """
    if not isinstance(text, str):
        return text
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for protected in _MD_PROTECTED_RE.finditer(text):
        out.append(_link_free_span(text[pos : protected.start()]))
        out.append(_rewrite_protected(protected.group()))
        pos = protected.end()
    out.append(_link_free_span(text[pos:]))
    return "".join(out)


def plain_with_citation_links(text: object) -> Markup | None:
    """Escape ``text`` and rewrite every URL in it as a ``cited paper`` anchor.

    Matching runs on the RAW string and escaping is applied per span. The
    reverse order is the defect this ordering exists to prevent: ``escape()``
    turns ``&`` into ``&amp;``, the matcher then captures the entity, and the
    href ends up holding ``&amp;amp;`` — a dead link.

    Apply exactly ONCE per value. The result is ``Markup``; feeding it back in
    would match the URL inside the ``href`` it just produced.

    A non-string is escaped and returned rather than raising — see the twin
    note on ``markdown_with_citation_links``. ``None`` stays ``None`` so a
    template's ``{% if %}`` still reads it as absent.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return Markup(escape(text))
    if not text:
        return Markup(text)
    parts: list[Markup] = []
    pos = 0
    for match in _URL_RE.finditer(text):
        url, trail = _split_trailing(match.group())
        if not _is_linkable(url) or _truncated_at_backslash(text, match.end()):
            continue
        parts.append(escape(text[pos : match.start()]))
        parts.append(
            Markup(
                '<a class="citation-link" href="{}" title="{}"'
                ' rel="noreferrer">{}</a>'
            ).format(url, url, CITATION_LABEL)
        )
        parts.append(escape(trail))
        pos = match.end()
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)
