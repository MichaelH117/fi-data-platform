// Theme toggle + re-ink Plotly charts from CSS tokens so they match light or dark mode.
(function () {
  "use strict";
  var root = document.documentElement;
  var token = function (name) { return getComputedStyle(root).getPropertyValue(name).trim(); };

  function inkCharts() {
    if (!window.Plotly) return;
    document.querySelectorAll(".js-plotly-plot").forEach(function (el) {
      var lay = {
        "font.color": token("--ink-2"),
        "hoverlabel.bgcolor": token("--surface"),
        "hoverlabel.bordercolor": token("--axis"),
        "hoverlabel.font.color": token("--ink-1"),
        "legend.font.color": token("--ink-2")
      };
      Object.keys(el.layout || {}).forEach(function (k) {
        if (/^[xy]axis\d*$/.test(k)) {
          lay[k + ".gridcolor"] = token("--grid");
          lay[k + ".linecolor"] = token("--axis");
          lay[k + ".tickfont.color"] = token("--ink-3");
        }
      });
      (el.layout.shapes || []).forEach(function (s, i) { lay["shapes[" + i + "].line.color"] = token("--ink-3"); });
      (el.layout.annotations || []).forEach(function (a, i) { lay["annotations[" + i + "].font.color"] = token("--ink-3"); });
      Plotly.relayout(el, lay);
      var idx = [], colors = [];
      el.data.forEach(function (t, i) {
        if (t.meta && t.meta.slot) { idx.push(i); colors.push(token("--s" + t.meta.slot)); }
      });
      if (idx.length) {
        Plotly.restyle(el, { "marker.color": colors, "line.color": colors, "marker.line.color": token("--surface") }, idx);
      }
    });
  }

  function setTheme(next) {
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* storage blocked: theme lasts this page only */ }
    inkCharts();
  }

  var btn = document.querySelector(".theme-toggle");
  if (btn) {
    btn.addEventListener("click", function () {
      var dark = root.dataset.theme ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      setTheme(dark ? "light" : "dark");
    });
  }
  // Phone/tablet menu: the sidebar is a drawer opened by the Menu button; Escape or a click outside closes it.
  var menu = document.querySelector(".nav-toggle");
  var sidebar = document.getElementById("sidebar");
  function setMenu(open, focusBack) {
    document.body.classList.toggle("nav-open", open);
    menu.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = sidebar.querySelector("a[aria-current]") || sidebar.querySelector("a");
      if (first) first.focus();
    } else if (focusBack) {
      menu.focus();
    }
  }
  if (menu && sidebar) {
    menu.addEventListener("click", function () { setMenu(!document.body.classList.contains("nav-open"), false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("nav-open")) setMenu(false, true);
    });
    document.addEventListener("click", function (e) {
      if (document.body.classList.contains("nav-open") && !sidebar.contains(e.target) && !menu.contains(e.target)) {
        setMenu(false, false);
      }
    });
  }

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", inkCharts);
  if (document.readyState === "complete") inkCharts(); else window.addEventListener("load", inkCharts);
})();
