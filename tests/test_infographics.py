"""The six textbook infographics (reports/infographics.py): committed SVGs, both themes, honest numbers."""

from __future__ import annotations

import json
import re

import pytest

from reports import infographics as ig

SRC = ig.OUT_DIRS[0]
pytestmark = pytest.mark.skipif(not (SRC / "numbers.json").exists(), reason="run python -m reports.infographics")


@pytest.mark.parametrize("stem", list(ig.FIGS))
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_svg_is_self_contained_and_labelled(stem, theme):
    svg = (SRC / f"{stem}-{theme}.svg").read_text(encoding="utf-8")
    assert 'role="img"' in svg and "<title>" in svg and "<desc>" in svg and 'aria-label="' in svg
    assert "<script" not in svg and "foreignObject" not in svg and "<style" not in svg
    assert "Source:" in svg and "Synthetic data" in svg
    assert re.search(r'width="1600"', svg)
    for d in ig.OUT_DIRS:  # the docs copy matches the site copy
        assert (d / f"{stem}-{theme}.svg").read_text(encoding="utf-8") == svg


def test_numbers_tie_out():
    n = json.loads((SRC / "numbers.json").read_text(encoding="utf-8"))
    assert sum(n["life"]["status"].values()) == n["life"]["contracts"]
    d = n["dollar"]
    assert abs(sum(d["parts"].values()) - d["retail"]) < 0.01
    assert abs(d["retail"] - d["parts"]["dealer_markup"] - d["coverage_cost"]) < 0.01
    p = n["participation"]["total"]
    assert abs(p["earned"] - p["incurred"] - p["ceding_fee"] - p["uw"]) < 0.01
    assert n["legacy"]["questions"] == len(n["legacy"]["rows"])


def test_figure_markup_paths(tmp_path):
    html = ig.figure("06-legacy-vs-better", "finance/index.html", "A caption & more.")
    assert 'src="../assets/infographics/06-legacy-vs-better-dark.svg"' in html
    assert 'class="ig-light"' in html and "A caption &amp; more." in html
    assert ig.figure("no-such-figure", "x.html", "c") == ""
    copied = ig.copy_to_site(tmp_path)
    assert len(copied) == 2 * len(ig.FIGS)
