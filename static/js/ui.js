// Page behaviours that used to be inline on…= handlers (spec §6.3, M-03). A CSP
// without 'unsafe-inline' refuses every inline handler, so each one is a data
// attribute read here. Every listener is delegated on `document`, so markup swapped
// in later (the simulation panel's refresh) keeps working without re-binding.
//
//   data-row-href="/path"    a click on the element navigates there, unless it
//                            landed on (or inside) a control of its own
//   data-toggle-target="id"  a click toggles `hidden` on #id, and aria-expanded on
//                            the clicked element when it carries one
//   button[data-toggles="id"]  a disclosure button (X-05): a click shows or hides
//                            #id and keeps the button's aria-expanded in step
//   tr[data-row-toggles]     a click elsewhere in the row clicks the row's
//                            [data-toggles] button, so the whole row stays clickable
//                            while the button is the keyboard path
//   form[data-allow-resubmit]  opts a POST form out of the double-submit guard
//                            (B-04, after the first IIFE)
//   details [data-lazy-fragment]  on first open, the slot's link is fetched and its
//                            HTML put in place (C-16, at the end of this file)
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
})();

// B-04: once a POST form's submission is under way its submit buttons are
// disabled, so a double click sends one request. Deferred with setTimeout
// because a control disabled during the submit event is left out of the form
// data, and the clicked button's own name/value must still be sent. Left alone:
// a submission a handler cancelled (confirm.js's dismissed dialog), GET forms,
// forms that target another window, and forms marked data-allow-resubmit. The
// server's duplicate check (assessment_reviews.submit_feedback) is the backstop.
document.addEventListener('submit', function (event) {
  var form = event.target;
  if (event.defaultPrevented || !(form instanceof HTMLFormElement)) { return; }
  if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') { return; }
  if (form.hasAttribute('data-allow-resubmit')) { return; }
  var target = form.getAttribute('target');
  if (target && target !== '_self') { return; }
  window.setTimeout(function () {
    form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]')
      .forEach(function (b) {
        b.disabled = true;
        b.setAttribute('data-submit-guarded', '');
      });
  }, 0);
});

// A page restored from the back-forward cache would keep those buttons disabled.
window.addEventListener('pageshow', function (event) {
  if (!event.persisted) { return; }
  document.querySelectorAll('[data-submit-guarded]').forEach(function (b) {
    b.disabled = false;
    b.removeAttribute('data-submit-guarded');
  });
});

// C-16: a <details> row holding [data-lazy-fragment] loads its body on first open
// from the slot's link (also the no-JavaScript fallback). `toggle` does not bubble,
// hence the capture listener. A redirect (expired session -> /login) or a non-HTML
// answer leaves the link in place with a message rather than injecting that page.
(function () {
  "use strict";
  document.addEventListener("toggle", function (event) {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement) || !details.open) {
      return;
    }
    const slot = details.querySelector("[data-lazy-fragment]");
    const link = slot ? slot.querySelector("a[href]") : null;
    if (!link || slot.dataset.loaded) {
      return;
    }
    slot.dataset.loaded = "1";
    slot.setAttribute("aria-busy", "true");
    fetch(link.href, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (resp) {
        const type = resp.headers.get("content-type") || "";
        if (!resp.ok || resp.redirected || type.indexOf("text/html") !== 0) {
          throw new Error("fragment");
        }
        return resp.text();
      })
      .then(function (html) {
        // Parsed, not assigned as markup: DOMParser's nodes never run a script.
        const doc = new DOMParser().parseFromString(html, "text/html");
        slot.replaceChildren(...doc.body.childNodes);
      })
      .catch(function () {
        delete slot.dataset.loaded;
        link.textContent = "Could not load here — open the prompt and response on their own page";
      })
      .finally(function () {
        slot.removeAttribute("aria-busy");
      });
  }, true);
})();
