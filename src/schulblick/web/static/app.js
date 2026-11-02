// Small progressive enhancements; every page works without this file.
document.addEventListener("click", function (ev) {
  var btn = ev.target.closest("[data-copy]");
  if (!btn) return;
  var input = document.getElementById(btn.getAttribute("data-copy"));
  if (!input) return;
  input.focus();
  input.select();
  var done = function () { btn.textContent = "Kopiert"; setTimeout(function () { btn.textContent = "Link kopieren"; }, 2000); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand && document.execCommand("copy"); done(); });
  } else {
    try { document.execCommand("copy"); done(); } catch (e) { /* text stays selected for manual copy */ }
  }
});
document.addEventListener("focusin", function (ev) {
  if (ev.target.matches && ev.target.matches("input[readonly]")) ev.target.select();
});

// ---------------------------------------------------------------- overview: collapsible and reorderable blocks
(function () {
  var nav = document.getElementById("jump");
  var blocks = document.getElementById("blocks");
  if (!nav || !blocks) return;
  var links = nav.querySelector(".jump-links");
  var tools = nav.querySelector(".jump-tools");
  var hint = document.querySelector(".arrange-hint");
  var btnArrange = nav.querySelector("[data-arrange]");
  var btnDone = nav.querySelector("[data-done]");
  var btnReset = nav.querySelector("[data-reset]");
  tools.hidden = false;

  function save(fields) {
    var body = new URLSearchParams(fields);
    return fetch("/ansicht", { method: "POST", body: body, credentials: "same-origin",
      headers: { "x-requested-with": "fetch" } }).catch(function () { /* layout is a convenience only */ });
  }

  // remember open/closed per block; programmatic opens (jump links) are not saved
  var quiet = false;
  blocks.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (!d.classList || !d.classList.contains("blk") || quiet) return;
    save({ block: d.closest(".block").dataset.key, open: d.open ? "1" : "0" });
  }, true);

  // a jump to a collapsed block opens it first
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest('a[href^="#"]');
    if (!a || nav.classList.contains("arranging")) return;
    var target = document.getElementById(a.getAttribute("href").slice(1));
    var block = target && target.closest(".block");
    var d = block && block.querySelector("details.blk");
    if (d && !d.open) { quiet = true; d.open = true; setTimeout(function () { quiet = false; }, 0); }
  });

  // arranging mode
  function setMode(on) {
    nav.classList.toggle("arranging", on);
    btnArrange.hidden = on; btnDone.hidden = !on; btnReset.hidden = !on;
    if (hint) hint.hidden = !on;
    links.querySelectorAll("a").forEach(function (a) { a.setAttribute("draggable", "false"); a.tabIndex = 0; });
    if (on) { var f = links.querySelector("a"); if (f) f.focus(); }
  }
  function order() {
    return Array.prototype.map.call(links.querySelectorAll("a[data-key]"), function (a) { return a.dataset.key; });
  }
  function applyOrder() {
    order().forEach(function (k) {
      var s = blocks.querySelector('.block[data-key="' + k + '"]');
      if (s) blocks.appendChild(s);
    });
  }
  btnArrange.addEventListener("click", function () { setMode(true); });
  btnDone.addEventListener("click", function () {
    setMode(false); applyOrder(); save({ order: order().join(",") });
  });
  btnReset.addEventListener("click", function () {
    save({ order: "" }).then(function () { location.reload(); });
  });

  links.addEventListener("click", function (ev) {
    if (nav.classList.contains("arranging") && ev.target.closest("a")) ev.preventDefault();
  });

  // pointer dragging (mouse, pen and touch)
  var drag = null;
  links.addEventListener("pointerdown", function (ev) {
    if (!nav.classList.contains("arranging")) return;
    var a = ev.target.closest("a[data-key]");
    if (!a) return;
    ev.preventDefault();
    drag = a; a.classList.add("dragging");
    a.setPointerCapture(ev.pointerId);
  });
  links.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    var others = Array.prototype.filter.call(links.querySelectorAll("a[data-key]"), function (x) { return x !== drag; });
    var before = null;
    for (var i = 0; i < others.length; i++) {
      var r = others[i].getBoundingClientRect();
      var sameRow = ev.clientY >= r.top - 4 && ev.clientY <= r.bottom + 4;
      if ((sameRow && ev.clientX < r.left + r.width / 2) || ev.clientY < r.top - 4) { before = others[i]; break; }
    }
    if (before !== drag.nextElementSibling) links.insertBefore(drag, before);
  });
  function endDrag() { if (drag) { drag.classList.remove("dragging"); drag = null; } }
  links.addEventListener("pointerup", endDrag);
  links.addEventListener("pointercancel", endDrag);

  // keyboard: arrows move the focused link
  links.addEventListener("keydown", function (ev) {
    if (!nav.classList.contains("arranging")) return;
    var a = ev.target.closest("a[data-key]");
    if (!a) return;
    if (ev.key === "ArrowLeft" && a.previousElementSibling) { links.insertBefore(a, a.previousElementSibling); a.focus(); ev.preventDefault(); }
    else if (ev.key === "ArrowRight" && a.nextElementSibling) { links.insertBefore(a.nextElementSibling, a); a.focus(); ev.preventDefault(); }
    else if (ev.key === "Enter" || ev.key === "Escape") { btnDone.click(); ev.preventDefault(); }
  });
})();
