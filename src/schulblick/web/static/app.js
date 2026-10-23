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
