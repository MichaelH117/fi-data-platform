// Theme toggle for the raw-data portal (light / dark). Storage is optional; the page works without it.
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem("portal-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var cur = root.getAttribute("data-theme") ||
        (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = cur === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("portal-theme", next); } catch (e) {}
    });
  });
})();
