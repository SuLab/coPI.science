document.addEventListener("DOMContentLoaded", function () {
  var root = document.querySelector("main") || document;
  document.querySelectorAll("[data-details-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var open = button.getAttribute("data-details-toggle") === "open";
      root.querySelectorAll("details").forEach(function (detail) { detail.open = open; });
    });
  });
  root.querySelectorAll("[data-open-details]").forEach(function (anchor) {
    anchor.addEventListener("click", function () {
      var detail = document.getElementById(anchor.getAttribute("data-open-details"));
      if (detail && detail.tagName === "DETAILS") detail.open = true;
    });
  });
  var clampChecks = [];
  root.querySelectorAll("[data-unclamp]").forEach(function (button) {
    var box = button.previousElementSibling;
    if (!box) return;
    var check = function () {
      var clamped = box.scrollHeight > box.clientHeight + 2;
      button.hidden = !clamped && !box.classList.contains("is-unclamped");
      var fade = box.querySelector(".timeline-message-fade");
      if (fade) fade.hidden = !clamped;
    };
    button.addEventListener("click", function () {
      var lifted = box.classList.toggle("is-unclamped");
      box.classList.toggle("max-h-64", !lifted);
      box.classList.toggle("overflow-hidden", !lifted);
      button.textContent = lifted ? "Show less" : "Show full message";
      check();
    });
    clampChecks.push({ box: box, check: check });
    setTimeout(check, 0);
    setTimeout(check, 500);
  });
  document.addEventListener("toggle", function (event) {
    var detail = event.target;
    if (!(detail instanceof HTMLDetailsElement) || !detail.open) return;
    clampChecks.forEach(function (entry) {
      if (detail.contains(entry.box)) entry.check();
    });
  }, true);
});
