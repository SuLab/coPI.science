// Shared sanitizing markdown renderer (SEC-2).
//
// Renders every element carrying a `data-markdown` attribute as GitHub-flavored
// markdown, passed through DOMPurify BEFORE it is assigned to innerHTML. The
// source text is LLM/agent-generated proposal and summary content — untrusted —
// so it must never reach innerHTML unsanitized. Load this AFTER marked and
// DOMPurify. Exposes window.copiRenderMarkdown(md) for ad-hoc use.
(function () {
  // Disable GFM strikethrough: the corpus uses single tildes for
  // "approximately" (e.g. "~30-37%"), which marked otherwise pairs into a
  // <del> span. No content uses intentional ~~strikethrough~~. Returning
  // undefined from the del tokenizer makes marked treat every tilde as
  // literal text. Guarded because marked may be absent (fail-closed path).
  if (window.marked && typeof marked.use === "function") {
    marked.use({ tokenizer: { del() { return undefined; } } });
  }

  // One factory for every sanitizing renderer (LC-02): the detail pages ("page"),
  // the assessment chat ("chat") and the collaboration graph ("graph"). Each call
  // returns a PRIVATE marked instance, so no profile's options leak into another
  // or into the page-global marked. Every profile treats "~" as literal text.
  function createSanitizingMarked(profile) {
    if (!window.marked || !window.marked.Marked) return null;
    const instance = new window.marked.Marked();
    const tokenizer = { del() { return undefined; } };
    const ext = { tokenizer: tokenizer };
    if (profile === "chat") {
      // marked's own `tag` tokenizer enters a raw-block state after <code>, <pre>,
      // <kbd> or <script> in which later text is emitted UNESCAPED (RSEC-1); raw
      // HTML is rendered as text below, so that state is never wanted.
      tokenizer.tag = function (src) {
        const cap = this.rules.inline.tag.exec(src);
        if (cap) {
          return { type: "html", raw: cap[0], inLink: false, inRawBlock: false, block: false, text: cap[0] };
        }
        return undefined;
      };
      ext.renderer = {
        html: function (html) {
          return String(html)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/["]/g, "&quot;")
            .replace(/[']/g, "&#39;");
        }
      };
    } else if (profile === "graph") {
      ext.gfm = true;
      ext.breaks = true;
    } else if (profile !== "page") {
      throw new Error("unknown markdown profile: " + profile);
    }
    instance.use(ext);
    return instance;
  }
  window.createSanitizingMarked = createSanitizingMarked;

  function renderMarkdown(md) {
    if (!md) return "";
    if (!window.marked || !window.DOMPurify) {
      // Fail closed: never inject unsanitized HTML if a dependency is missing.
      // Fall back to plain text via textContent round-trip.
      var div = document.createElement("div");
      div.textContent = md;
      return div.innerHTML;
    }
    return DOMPurify.sanitize(marked.parse(md));
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
