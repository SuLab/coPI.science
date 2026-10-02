// Tag widgets (templates/_tag_field.html, D-16). Every tag is its own hidden
// <input name="<field>">, so a tag containing a comma is posted whole and the
// handler reads the field with form.getlist(). Typing a comma or pasting a
// comma-separated list still splits it into several tags, as before.
(function () {
  "use strict";

  function tagText(pill) {
    var hidden = pill.querySelector('input[type="hidden"]');
    return hidden ? hidden.value : "";
  }

  function init(wrapper) {
    var name = wrapper.getAttribute("data-tag-field");
    var container = wrapper.querySelector(".tag-container");
    var textInput = wrapper.querySelector(".tag-input");
    if (!name || !container || !textInput) return;

    function addTag(raw) {
      var text = raw.trim();
      if (!text) return;
      var pills = container.querySelectorAll(".tag-pill");
      for (var i = 0; i < pills.length; i++) {
        if (tagText(pills[i]).toLowerCase() === text.toLowerCase()) return;
      }
      var pill = document.createElement("span");
      pill.className = "tag-pill";
      pill.appendChild(document.createTextNode(text));
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = name;
      hidden.value = text;
      pill.appendChild(hidden);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tag-remove";
      btn.setAttribute("aria-label", "Remove " + text);
      btn.textContent = "×";
      pill.appendChild(btn);
      container.insertBefore(pill, textInput);
    }

    container.addEventListener("click", function (event) {
      var remove = event.target.closest(".tag-remove");
      if (remove && container.contains(remove)) {
        remove.closest(".tag-pill").remove();
      }
      textInput.focus();
    });

    textInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTag(textInput.value.replace(/,/g, ""));
        textInput.value = "";
      } else if (event.key === "Backspace" && textInput.value === "") {
        var pills = container.querySelectorAll(".tag-pill");
        if (pills.length > 0) pills[pills.length - 1].remove();
      }
    });

    textInput.addEventListener("paste", function (event) {
      event.preventDefault();
      var pasted = (event.clipboardData || window.clipboardData).getData("text");
      pasted.split(",").forEach(addTag);
    });

    textInput.addEventListener("blur", function () {
      if (textInput.value.trim()) {
        addTag(textInput.value);
        textInput.value = "";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-tag-field]").forEach(init);
  });
})();
