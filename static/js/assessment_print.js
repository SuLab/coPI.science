(function () {
  var wasOpen = null;
  window.addEventListener("beforeprint", function () {
    wasOpen = [];
    document.querySelectorAll("details").forEach(function (detail) {
      wasOpen.push(detail.open);
      detail.open = true;
    });
  });
  window.addEventListener("afterprint", function () {
    if (!wasOpen) return;
    document.querySelectorAll("details").forEach(function (detail, index) {
      if (index < wasOpen.length) detail.open = wasOpen[index];
    });
    wasOpen = null;
  });
})();
