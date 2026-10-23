"""Report-site shell: grouped sidebar nav, the Start here page, KPI disclosures and sparklines."""

from __future__ import annotations

import types
from datetime import date
from types import SimpleNamespace

import reports.build as rb
from reports import charts
from reports.__main__ import broken_links


def test_group_nav_puts_start_first_and_every_link_in_one_section():
    nav = rb.NAV + [("regulatory/index.html", "Regulatory"), ("finance/index.html", "Finance"),
                    ("ops.html", "Operations"), ("x/index.html", "Unknown area")]
    groups = {"regulatory/index.html": "Regulatory", "finance/index.html": "Finance", "ops.html": "Platform"}
    top, sections = rb.group_nav(nav, groups)
    assert top == [(rb.START, "Start here")]
    names = [g for g, _ in sections]
    assert names == [g for g in rb.NAV_GROUP_ORDER if g in names]
    flat = [h for _, items in sections for h, _ in items]
    assert sorted(flat) == sorted(h for h, _ in nav if h != rb.START)  # nothing dropped, nothing doubled
    by = dict(sections)
    assert by["Finance"][0][0] == "finance/index.html"  # NAV_RANK: statements before products
    assert ("x/index.html", "Unknown area") in by["Platform"]


def test_sparkline_svg_is_decorative_and_needs_three_points():
    svg = charts.sparkline([1, 2, 3, 2.5])
    assert svg.startswith("<svg") and 'aria-hidden="true"' in svg and "spark-line" in svg
    assert charts.sparkline([1, 2]) == "" and charts.sparkline([]) == ""
    assert rb.spark(__import__("pandas").Series([None, 1.0, 2.0, 3.0])) == [1.0, 2.0, 3.0]


def _plugin(path: str, label: str, area_group: str | None = None):
    mod = types.ModuleType(f"reports.{label.lower()}")
    mod.build = lambda con, out: [rb.Page(path, label, f"{label} lede. More text.")]
    mod.NAV_LABEL = label
    if area_group:
        mod.NAV_GROUP = area_group
    return mod


def test_render_site_builds_sidebar_start_page_and_kpi_disclosure(tmp_path, monkeypatch):
    core = [rb.Page("index.html", "Executive overview", "core page",
                    kpis=[rb.Kpi("Loss ratio, T12", "49.6%", "incurred / earned claims reserve", "good",
                                 spark=[0.5, 0.48, 0.49, 0.496]),
                          rb.Kpi("No definition", "7")])]
    monkeypatch.setattr(rb, "build_pages", lambda d: core)
    monkeypatch.setattr(rb, "NAV", [(rb.START, "Start here"), ("index.html", "Overview")])
    d = SimpleNamespace(as_of=date(2026, 10, 22), last_month=date(2026, 9, 1))
    out = tmp_path / "site"
    written = rb.render_site(d, out, con=object(), plugins=[
        ("regulatory", _plugin("regulatory/index.html", "Regulatory")),
        ("custom", _plugin("custom.html", "Custom", area_group="Dealers"))])
    names = {p.relative_to(out).as_posix() for p in written}
    assert {"start.html", "index.html", "regulatory/index.html", "custom.html"} == names
    assert broken_links(out) == []

    index = (out / "index.html").read_text(encoding="utf-8")
    side = index[index.index('<aside class="sidebar"'):index.index("</aside>")]
    assert side.index("Start here") < side.index("Executive") < side.index("Regulatory") < side.index("Dealers")
    assert 'href="custom.html"' in side.split("Dealers")[1]  # NAV_GROUP on a plug-in wins
    assert 'aria-controls="sidebar"' in index and 'aria-expanded="false"' in index
    # long text lives in an expandable <details>, readable without hover; status stays visible
    assert '<details class="kpi-info">' in index and "incurred / earned claims reserve</p>" in index
    assert "On track" in index and 'class="spark"' in index
    assert index.count('<details class="kpi-info">') == 1  # a tile without a definition gets no "i"
    assert 'href="start.html"' in index and "New here?" in index

    start = (out / "start.html").read_text(encoding="utf-8")
    assert '<a href="start.html" aria-current="page">' in start
    for word in ("Reliable", "Reconciled", "Secure", "synthetic"):
        assert word in start
    for href in ("index.html", "regulatory/index.html", "custom.html"):
        assert f'<a href="{href}">' in start  # every section is linked from the directory


def test_without_start_in_nav_no_start_page(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "build_pages", lambda d: [rb.Page("index.html", "Overview", "core page")])
    monkeypatch.setattr(rb, "NAV", [("index.html", "Overview")])
    d = SimpleNamespace(as_of=date(2026, 10, 22), last_month=date(2026, 9, 1))
    written = rb.render_site(d, tmp_path / "site")
    assert [p.name for p in written] == ["index.html"]
    assert "New here?" not in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
