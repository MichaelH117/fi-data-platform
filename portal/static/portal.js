// Theme toggle for the raw-data portal (dark by default; light when the viewer picks it). Storage is optional; the page works without it.
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem("portal-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("portal-theme", next); } catch (e) {}
    });
  });
})();
