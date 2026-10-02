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
//   button[data-toggles="id"]  a disclosure button (X-05): a click shows or hides
//                            #id and keeps the button's aria-expanded in step
//   tr[data-row-toggles]     a click elsewhere in the row clicks the row's
//                            [data-toggles] button, so the whole row stays clickable
//                            while the button is the keyboard path
(function () {
  "use strict";

  // A click on one of these inside a [data-row-href], [data-toggle-target] or
  // [data-row-toggles] belongs to that control (the ORCID link in a user row),
  // not to the row.
  const INTERACTIVE = "a, button, input, select, textarea, label, summary";

  function ownControl(target, host) {
    const inner = target.closest(INTERACTIVE);
    return inner !== null && inner !== host && host.contains(inner);
  }

  function localPath(value) {
    // A backslash after the slash is protocol-relative to a browser, like "//host".
    return typeof value === "string" && /^\/(?![\/\\])/.test(value);
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

  // Disclosure (X-05). A forwarded row click dispatches a click on the button,
  // which reaches this listener again through the button branch.
  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const button = target.closest("button[data-toggles]");
    if (button) {
      const panel = document.getElementById(button.getAttribute("data-toggles"));
      if (!panel) return;
      const opening = panel.classList.contains("hidden");
      panel.classList.toggle("hidden", !opening);
      button.setAttribute("aria-expanded", opening ? "true" : "false");
      return;
    }

    const row = target.closest("tr[data-row-toggles]");
    if (!row || ownControl(target, row)) return;
    const rowButton = row.querySelector("button[data-toggles]");
    if (rowButton) rowButton.click();
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
