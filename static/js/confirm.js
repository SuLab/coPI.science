// The one confirmation mechanism for destructive forms (spec 2026-10-01 §5.1).
//
// A form opts in with data-confirm="<message>". The message is an HTML attribute
// value, autoescaped by Jinja, and is only ever passed to window.confirm as a
// string: a quote, backslash or tag in a user's name cannot change what runs.
// Inline onsubmit="return confirm('...{{ name }}...')" handlers were the A-01 bug
// (an entity-decoded quote closed the JS string) and must not come back.
(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message === null) return;
    if (!window.confirm(message)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
})();
