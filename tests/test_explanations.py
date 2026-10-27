"""Plain-English explanations (docs/V3_DESIGN.md section 10.2): one source, nothing left blank.

1. Every metric in ``dbt/models/metrics.yml`` has a row in ``dbt/seeds/core/metric_explanations.csv``.
2. Every KPI tile and every chart or table section the site renders (each ``Kpi(...)``, ``Section(...)`` and
   ``chart_section(...)`` call under ``reports/``) is mapped in ``reports/content/tile_metric_map.csv`` to an
   explanation row. A section may map to no metric only when it is a page guide, navigation or download.
3. The three lines a reader sees are filled in and short (160 characters at most), and every acronym in
   them is defined in ``dbt/seeds/core/glossary_terms.csv``.
4. The glossary holds the required terms, and every "see also" link points at a real term.

Reads files only: no warehouse, no dbt run.
"""
from __future__ import annotations

import ast
import csv
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "dbt" / "seeds" / "core"
EXPLAIN = SEEDS / "metric_explanations.csv"
GLOSSARY = SEEDS / "glossary_terms.csv"
TILE_MAP = ROOT / "reports" / "content" / "tile_metric_map.csv"
TEXT_COLS = ("what_it_means", "why_shown_this_way", "why_your_boss_asks")
MAX_LINE = 160
DIRECTIONS = {"up", "down", "band"}
COMPARE = {"prior_year", "target", "peer_avg", "prior_month"}
AUDIENCES = {"dealer", "finance", "compliance", "executive"}  # audience_roles tags in the explanation seed
HUBS = {"accounting", "finance", "legal", "compliance", "dealer", "claims", "customer-service", "data-bi",
        "executive"}  # department hubs in reports/content/role_pages.yml
REQUIRED_TERMS = {"vsc", "gap", "ancillary", "earned_vs_written", "loss_ratio", "cession", "funded_reserve",
                  "clip", "ibnr", "rule_of_78", "remit", "chargeback", "obligor", "producer_vs_provider", "in_force"}
KINDS = {"Kpi": "tile", "Section": "section", "chart_section": "section"}
ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]+\b")
NOT_ACRONYMS = {"AM", "PM"}  # clock words, not jargon


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _constants(trees: dict[Path, ast.Module]) -> dict[str, str]:
    """Module-level string constants across reports/ (labels such as CANCEL_RATE_ITD live in one module)."""
    out = {}
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.value.value
    return out


def _render(node: ast.AST, consts: dict[str, str]) -> str:
    """A label as written in the source; every runtime part becomes ``*``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "*" for v in node.values)
    if isinstance(node, ast.Name):
        return consts.get(node.id, "*")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render(node.left, consts) + _render(node.right, consts)
    return "*"


def site_labels() -> list[tuple[str, str, str]]:
    """(source file, tile|section, label pattern) for every tile and section call under reports/.

    The body of the ``chart_section`` helper itself is skipped: it only passes its caller's title through.
    """
    files = sorted((ROOT / "reports").rglob("*.py"))
    trees = {f: ast.parse(f.read_text(encoding="utf-8")) for f in files}
    consts = _constants(trees)
    out = set()
    for f, tree in trees.items():
        skip = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "chart_section":
                skip.update(id(n) for n in ast.walk(fn))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in skip or not node.args:
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in KINDS:
                out.add((f.relative_to(ROOT).as_posix(), KINDS[name], _render(node.args[0], consts)))
    return sorted(out)


@pytest.fixture(scope="module")
def explanations() -> dict[str, dict]:
    rows = _csv(EXPLAIN)
    ids = [r["metric_id"] for r in rows]
    assert len(ids) == len(set(ids)), "metric_id must be unique"
    return {r["metric_id"]: r for r in rows}


@pytest.fixture(scope="module")
def glossary() -> list[dict]:
    return _csv(GLOSSARY)


def test_every_metric_in_metrics_yml_has_an_explanation(explanations):
    spec = yaml.safe_load((ROOT / "dbt" / "models" / "metrics.yml").read_text(encoding="utf-8"))
    names = {m["name"] for m in spec["metrics"]}
    missing = sorted(names - set(explanations))
    assert not missing, f"metrics with no explanation row: {missing}"


def test_every_rendered_tile_and_chart_has_an_explanation(explanations):
    mapped = {(r["source_file"], r["kind"], r["label_pattern"]): r for r in _csv(TILE_MAP)}
    found = set(site_labels())
    assert len(found) > 150, "the label scan found too little; did reports/ move?"
    missing = sorted(found - set(mapped))
    assert not missing, f"tiles or sections with no row in {TILE_MAP.name}: {missing}"
    stale = sorted(set(mapped) - found)
    assert not stale, f"map rows for labels the site no longer renders: {stale}"
    for key, r in mapped.items():
        if r["metric_id"]:
            assert r["metric_id"] in explanations, (key, r["metric_id"])
        else:
            assert key[1] == "section" and r["note"], f"only a guide/navigation section may skip a metric: {key}"


def test_explanation_rows_are_complete_and_short(explanations):
    for mid, r in explanations.items():
        assert r["plain_name"].strip(), mid
        for col in TEXT_COLS:
            text = r[col].strip()
            assert text, (mid, col)
            assert len(text) <= MAX_LINE, (mid, col, len(text))
        assert r["good_direction"] in DIRECTIONS, (mid, r["good_direction"])
        assert r["compare_to"] in COMPARE, (mid, r["compare_to"])
        roles = set(r["audience_roles"].split("|"))
        assert roles and roles <= AUDIENCES, (mid, roles)
        assert r["decision_it_drives"].strip(), mid
        for col in ("watch_threshold", "alert_threshold"):
            if r[col]:
                float(r[col])


def test_no_acronym_without_a_glossary_entry(explanations, glossary):
    known = {a for g in glossary for a in ACRONYM.findall(g["term"])}
    texts = [(mid, r[c]) for mid, r in explanations.items() for c in ("plain_name", *TEXT_COLS)]
    texts += [(g["term_id"], g["plain_definition"]) for g in glossary]
    unknown = sorted({(mid, a) for mid, t in texts for a in ACRONYM.findall(t)
                      if a not in known and a not in NOT_ACRONYMS})
    assert not unknown, f"acronyms not defined in {GLOSSARY.name}: {unknown}"


def test_glossary_has_the_required_terms_and_valid_links(glossary):
    ids = [g["term_id"] for g in glossary]
    assert len(ids) == len(set(ids))
    assert REQUIRED_TERMS <= set(ids), sorted(REQUIRED_TERMS - set(ids))
    for g in glossary:
        assert g["term"].strip() and g["plain_definition"].strip(), g["term_id"]
        for s in filter(None, g["see_also"].split("|")):
            assert s in ids, (g["term_id"], s)


def test_role_pages_and_data_flow_copy_point_at_real_things(explanations):
    """reports/content/ copy: each department hub has questions, 3-6 answering pages, real glossary terms, tiles
    the seed tags for its audience, an owner and a runbook section that exists; gates in the flow are real."""
    doc = yaml.safe_load((ROOT / "reports" / "content" / "role_pages.yml").read_text(encoding="utf-8"))
    roles = doc["roles"]
    assert {r["id"] for r in roles} == HUBS
    from reports import guides  # the site builds the hubs from this YAML only (no second list in code)

    assert [r.key for r in guides.ROLES] == [r["id"] for r in roles]
    assert [len(r.links) for r in guides.ROLES] == [len(r["pages"]) for r in roles]
    assert "Role(\"" not in (ROOT / "reports" / "guides.py").read_text(encoding="utf-8")
    from reports import explain
    assert not hasattr(explain, "ROLES"), "hub ids live in role_pages.yml only"
    glossary = {g["term_id"] for g in _csv(ROOT / "dbt" / "seeds" / "core" / "glossary_terms.csv")}
    runbook = (ROOT / doc["runbook"]).read_text(encoding="utf-8")
    headings = {h.strip().strip("`") for h in re.findall(r"^#{2,3}\s+(.+)$", runbook, flags=re.M)}
    bands = {b["id"] for b in doc["bands"]}
    for r in roles:
        rid = r["id"]
        assert r["band"] in bands and r["audience"] in AUDIENCES, rid
        assert 3 <= len(r["pages"]) <= 6, rid
        assert 3 <= len(r["tiles"]) <= 4 and set(r["tiles"]) <= set(explanations), rid
        tagged = {m for m, e in explanations.items() if r["audience"] in e["audience_roles"].split("|")}
        assert set(r["tiles"]) <= tagged, (rid, sorted(set(r["tiles"]) - tagged))
        hrefs = {p["href"] for p in r["pages"]}
        for p in r["pages"]:
            assert p["why"].strip() and len(p["why"]) <= MAX_LINE, (rid, p["href"])
        assert 3 <= len(r["questions"]) <= 6, rid
        for q in r["questions"]:
            assert q["ask"].strip().endswith("?") and len(q["why"]) <= MAX_LINE, (rid, q["ask"])
            assert q["page"] in hrefs, (rid, q["ask"], "answer must be one of the hub's pages")
        assert r["terms"] and set(r["terms"]) <= glossary, (rid, sorted(set(r["terms"]) - glossary))
        w = r["if_wrong"]
        assert w["owner"].strip() and w["check"].strip() and any(h.startswith(w["runbook"]) for h in headings), (rid, w)
    flow = yaml.safe_load((ROOT / "reports" / "content" / "data_flow.yml").read_text(encoding="utf-8"))["steps"]
    assert len(flow) == 5
    src = (ROOT / "pipeline" / "diagnostics.py").read_text(encoding="utf-8")
    gates = set(re.findall(r'"(diag_[a-z_]+)": \(', src))
    used = {g for s in flow for g in s["gates"]}
    assert used <= gates, sorted(used - gates)
    assert used == gates, f"every gate belongs to one step: {sorted(gates - used)}"
    for s in flow:
        assert s["can_go_wrong"] and len(s["can_go_wrong"]) == len(s["how_we_stop_it"]), s["id"]
