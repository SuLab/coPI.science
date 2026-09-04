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
