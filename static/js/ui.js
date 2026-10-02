// Page behaviours that used to be inline on…= handlers (spec §6.3, M-03). A CSP
// without 'unsafe-inline' refuses every inline handler, so each one is a data
// attribute read here. Every listener is delegated on `document`, so markup swapped
// in later (the simulation panel's refresh) keeps working without re-binding.
//
//   data-row-href="/path"    a click on the element navigates there, unless it
//                            landed on (or inside) a control of its own
//   data-autosubmit          a change on this control submits its form through
//                            requestSubmit(), so submit listeners (confirm.js) run
//   data-filter-nav="/path"  a change on a [data-filter-param] control inside it
//                            navigates to /path?<param>=<value>…, empty values omitted
//   data-toggle-target="id"  a click toggles `hidden` on #id, and aria-expanded on
//                            the clicked element when it carries one
(function () {
  "use strict";

  // A click on one of these inside a [data-row-href] or [data-toggle-target]
  // belongs to that control (the ORCID link in a user row), not to the row.
  const INTERACTIVE = "a, button, input, select, textarea, label, summary";

  function ownControl(target, host) {
    const inner = target.closest(INTERACTIVE);
    return inner !== null && inner !== host && host.contains(inner);
  }

  function localPath(value) {
    return typeof value === "string" && value.charAt(0) === "/" && value.charAt(1) !== "/";
  }

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const row = target.closest("[data-row-href]");
    if (row && !ownControl(target, row)) {
      const href = row.getAttribute("data-row-href");
      if (localPath(href)) window.location.assign(href);
      return;
    }

    const toggler = target.closest("[data-toggle-target]");
    if (toggler && !ownControl(target, toggler)) {
      const panel = document.getElementById(toggler.getAttribute("data-toggle-target"));
      if (!panel) return;
      const hidden = panel.classList.toggle("hidden");
      if (toggler.hasAttribute("aria-expanded")) {
        toggler.setAttribute("aria-expanded", hidden ? "false" : "true");
      }
    }
  });

  document.addEventListener("change", function (event) {
    const control = event.target;
    if (!(control instanceof Element)) return;

    if (control.hasAttribute("data-autosubmit") && control.form) {
      control.form.requestSubmit();
      return;
    }

    const nav = control.closest("[data-filter-nav]");
    if (nav && control.hasAttribute("data-filter-param")) {
      const base = nav.getAttribute("data-filter-nav");
      if (!localPath(base)) return;
      const params = new URLSearchParams();
      nav.querySelectorAll("[data-filter-param]").forEach(function (field) {
        if (field.value) params.set(field.getAttribute("data-filter-param"), field.value);
      });
      const query = params.toString();
      window.location.assign(query ? base + "?" + query : base);
    }
  });
})();
