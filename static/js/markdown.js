// Shared sanitizing markdown renderer (SEC-2; spec 2026-10-01 §5.2).
//
// Renders every element carrying a `data-markdown` attribute as markdown. The source
// is LLM/agent-generated text — untrusted — so raw HTML in it is shown as TEXT, and the
// output passes an explicit DOMPurify allowlist before it reaches innerHTML. A lone
// <br> is the one raw tag kept (the only raw HTML in production data, 2026-10-01).
// Load this AFTER marked and DOMPurify. Exposes window.copiRenderMarkdown(md).
(function () {
  // Disable GFM strikethrough: the corpus uses single tildes for "approximately"
  // (e.g. "~30-37%"), which marked otherwise pairs into a <del> span.
  if (window.marked && typeof marked.use === "function") {
    marked.use({ tokenizer: { del() { return undefined; } } });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/["]/g, "&quot;")
      .replace(/[']/g, "&#39;");
  }

  var LONE_BR = /^<br\s*\/?>$/i;

  // marked's own `tag` tokenizer enters a raw-block state after <code>, <pre>, <kbd>
  // or <script> in which later text is emitted UNESCAPED (RSEC-1); raw HTML is
  // rendered as text by the profiles below, so that state is never wanted.
  function rawTagTokenizer(src) {
    const cap = this.rules.inline.tag.exec(src);
    if (cap) {
      return { type: "html", raw: cap[0], inLink: false, inRawBlock: false, block: false, text: cap[0] };
    }
    return undefined;
  }

  // One factory for every sanitizing renderer (LC-02): the detail pages ("page"),
  // the assessment chat ("chat") and the collaboration graph ("graph"). Each call
  // returns a PRIVATE marked instance. Every profile treats "~" as literal text.
  function createSanitizingMarked(profile) {
    if (!window.marked || !window.marked.Marked) return null;
    const instance = new window.marked.Marked();
    const tokenizer = { del() { return undefined; } };
    const ext = { tokenizer: tokenizer };
    if (profile === "chat") {
      tokenizer.tag = rawTagTokenizer;
      ext.renderer = {
        html: function (html) { return escapeHtml(html); }
      };
    } else if (profile === "page") {
      tokenizer.tag = rawTagTokenizer;
      ext.renderer = {
        html: function (html) {
          var s = String(html);
          if (LONE_BR.test(s.trim())) return "<br>";
          // A block that only OPENS with a lone <br> (text right after it) must not
          // take the following markdown down with it into escaped text.
          var lead = /^\s*<br\s*\/?>[ \t]*\n([\s\S]*)$/i.exec(s);
          if (lead && pageMarked) return "<br>" + pageMarked.parse(lead[1]);
          return escapeHtml(s);
        },
        // GFM task lists: marked emits an <input type=checkbox>, which the allowlist
        // drops; keep the state as text.
        checkbox: function (checked) { return checked ? "[x]" : "[ ]"; },
        // marked 12 passes `text` (the alt text) already escaped (outputLink).
        image: function (href, title, text) {
          if (!href) return text || "";
          return '<a href="' + escapeHtml(href) + '">' + (text || escapeHtml(href)) + "</a>";
        }
      };
    } else if (profile === "graph") {
      ext.gfm = true;
      ext.breaks = true;
    } else {
      throw new Error("unknown markdown profile: " + profile);
    }
    instance.use(ext);
    return instance;
  }
  window.createSanitizingMarked = createSanitizingMarked;

  // Exactly what markdown produces; no form controls, media, styles or ids. `title`
  // carries md_citations' cited URL (src/services/prose_citations.py); `start` keeps an
  // ordered list's numbering and `align` a table column's alignment (both inert, both
  // emitted by marked). DOMPurify keeps data-* and aria-* by default even with an
  // explicit ALLOWED_ATTR, so both are switched off: the site's document-level
  // behaviours key on data-* attributes.
  var PAGE_PURIFY = {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li",
                   "a", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
                   "table", "thead", "tbody", "tr", "th", "td"],
    ALLOWED_ATTR: ["href", "title", "start", "align"],
    // DOMPurify checks every non-URI-safe attribute's value against
    // ALLOWED_URI_REGEXP; "2" and "center" are not URIs, so both are declared safe.
    ADD_URI_SAFE_ATTR: ["start", "align"],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i
  };
  window.COPI_PAGE_PURIFY = PAGE_PURIFY;

  var pageMarked = null;

  function renderMarkdown(md) {
    if (!md) return "";
    if (!pageMarked) pageMarked = createSanitizingMarked("page");
    if (!pageMarked || !window.DOMPurify) {
      // Fail closed: never inject unsanitized HTML if a dependency is missing.
      return escapeHtml(md);
    }
    return DOMPurify.sanitize(pageMarked.parse(md), PAGE_PURIFY);
  }

  function renderAll() {
    document.querySelectorAll("[data-markdown]").forEach(function (el) {
      var md = el.getAttribute("data-markdown");
      if (md) el.innerHTML = renderMarkdown(md);
    });
  }

  window.copiRenderMarkdown = renderMarkdown;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAll);
  } else {
    renderAll();
  }
})();
