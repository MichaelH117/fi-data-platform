"""Tabular-Editor-style documentation export (semantic/export.py): TMDL + PBIR -> four CSVs.

No warehouse and no Postgres needed: the exporter reads only the files on disk.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from semantic.__main__ import main as semantic_main
from semantic.export import FILES, export, field_ref, measure_refs, parse_tmdl, split_ref, sql_ident

ROOT = Path(__file__).resolve().parents[1]
MEASURE_RE = re.compile(r"^\tmeasure (?:'((?:[^']|'')+)'|(\S+)) =", re.M)
REPORT_DEF = ROOT / "powerbi" / "FI Data Platform.Report" / "definition"


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    out = tmp_path_factory.mktemp("export")
    counts = export(out)
    return out, counts, {f: _read(out / f) for f in FILES}


# --------------------------------------------------------------------------- parser units
TMDL = """/// The measure table.
table 'Key ''Q'' Measures'

\t/// Inline one.
\tmeasure 'Paid' = SUM('Fact'[Amount])
\t\tformatString: #,0
\t\tdisplayFolder: Claims
\t\tannotation dbt_metric = claims_paid

\tmeasure Ratio =
\t\t\tVAR _x = [Paid]
\t\t\tRETURN
\t\t\t    DIVIDE(_x, [Other])
\t\tformatString: 0.0%

\tcolumn 'Amount Key'
\t\tdataType: int64
\t\tisHidden
\t\tsourceColumn: AMOUNT_KEY

\tpartition 'Key Measures' = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Data = Source{[Schema = "core", Item = "fct_x"]}[Data]
\t\t\t\tin
\t\t\t\t    Data
"""


def test_parser_reads_quoted_names_inline_and_multiline_expressions_and_partitions():
    (table,) = parse_tmdl(TMDL)
    assert table.keyword == "table" and table.name == "Key 'Q' Measures"
    assert table.description == "The measure table."
    paid, ratio = table.kids("measure")
    assert paid.value == "SUM('Fact'[Amount])" and paid.description == "Inline one."
    assert paid.props() == {"formatString": "#,0", "displayFolder": "Claims"}
    assert paid.annotations() == {"dbt_metric": "claims_paid"}
    assert ratio.value.splitlines() == ["VAR _x = [Paid]", "RETURN", "    DIVIDE(_x, [Other])"]
    assert ratio.props() == {"formatString": "0.0%"}
    (col,) = table.kids("column")
    assert col.props()["sourceColumn"] == "AMOUNT_KEY" and "isHidden" in col.flags()
    (part,) = table.kids("partition")
    source = next(c for c in part.children if c.keyword == "source")
    assert source.value.startswith("let\n") and 'Item = "fct_x"' in source.value


@pytest.mark.parametrize("ref, expected", [
    ("'Claim Payment Line'.'Claim Key'", ("Claim Payment Line", "Claim Key")),
    ("Claim.'Claim Key'", ("Claim", "Claim Key")),
    ("Contracts.fxDealerId", ("Contracts", "fxDealerId")),
])
def test_split_ref(ref, expected):
    assert split_ref(ref) == expected


def test_measure_refs_ignore_column_references_and_unknown_names():
    dax = "DIVIDE([Paid], [Earned]) + SUM('Fact'[Paid]) + Fact[Earned] + [Nope]"
    assert measure_refs(dax, {"Paid", "Earned"}) == {"Paid", "Earned"}
    assert measure_refs("SUM('Fact'[Paid])", {"Paid"}) == set()


@pytest.mark.parametrize("name, sql", [("claim_key", "claim_key"), ("CLAIM_KEY", "claim_key"),
                                       ("fxDealerId", '"fxDealerId"'), ("Claim Amount", '"Claim Amount"')])
def test_sql_identifier_quoting(name, sql):
    assert sql_ident(name) == sql


def test_field_ref_shapes():
    src = {"Expression": {"SourceRef": {"Entity": "Dealer"}}}
    assert field_ref({"Measure": {**src, "Property": "Paid"}}) == ("Dealer", "Paid", "measure")
    assert field_ref({"Column": {**src, "Property": "State"}}) == ("Dealer", "State", "column")
    assert field_ref({"Aggregation": {"Expression": {"Column": {**src, "Property": "Amt"}}, "Function": 0}}) == \
        ("Dealer", "Sum(Amt)", "aggregation")
    hl = {"HierarchyLevel": {"Expression": {"Hierarchy": {**src, "Hierarchy": "Geo"}}, "Level": "State"}}
    assert field_ref(hl) == ("Dealer", "Geo.State", "hierarchy_level")
    assert field_ref({"Literal": {"Value": "1"}}) is None


# --------------------------------------------------------------------------- the real repo export
def test_four_files_with_rows(exported):
    out, counts, rows = exported
    assert set(counts) == set(FILES)
    assert all(counts[f] > 0 for f in FILES), counts


def _model_measures(folder: Path) -> set[str]:
    names = set()
    for p in (folder / "definition" / "tables").glob("*.tmdl"):
        for a, b in MEASURE_RE.findall(p.read_text(encoding="utf-8")):
            names.add((a or b).replace("''", "'"))
    return names


@pytest.mark.parametrize("model_set, folder", [
    ("modern_domain", ROOT / "semantic" / "model" / "Claims.SemanticModel"),
    ("modern_domain", ROOT / "semantic" / "model" / "Finance.SemanticModel"),
    ("modern_report", ROOT / "powerbi" / "FI Data Platform.SemanticModel"),
    ("legacy", ROOT / "legacy" / "semantic_models" / "Ops.SemanticModel"),
    ("legacy", ROOT / "legacy" / "semantic_models" / "Reporting.SemanticModel"),
])
def test_every_measure_is_documented_with_its_dax(exported, model_set, folder):
    doc = exported[2]["Model_Documentation.csv"]
    # the display name lives in database.tmdl ("F&I Data Platform"); the folder drops the "&"
    model = re.match(r"database (?:'(.+)'|(\S+))", (folder / "definition" / "database.tmdl").read_text("utf-8"))
    model = model.group(1) or model.group(2)
    got = {r["name"]: r for r in doc if r["model_set"] == model_set and r["model"] == model
           and r["object_type"] == "measure"}
    assert set(got) == _model_measures(folder)
    assert all(r["expression"] for r in got.values())
    if model_set != "legacy":  # modern measures name their single definition: a dbt metric or a stated source
        assert all(r["dbt_metric"] or "source=" in r["extra"] for r in got.values())


def test_calc_items_rls_and_partition_sources_are_documented(exported):
    doc = exported[2]["Model_Documentation.csv"]
    modern = {r["model"] for r in doc if r["model_set"] != "legacy"}
    for model in modern:
        items = {r["name"] for r in doc if r["model"] == model and r["object_type"] == "calculation_item"}
        assert items == {"Current", "MTD", "QTD", "YTD", "PY", "YoY %", "T12M"}, model
        rls = [r for r in doc if r["model"] == model and r["model_set"] != "legacy"
               and r["object_type"] == "rls_filter"]
        assert rls and all(r["expression"] and "modelPermission=read" in r["extra"] for r in rls), model
        exprs = {r["expression"] for r in rls}
        assert any("USERPRINCIPALNAME()" in e for e in exprs) or exprs == {"(no row filter)"}, model
    parts = [r for r in doc if r["object_type"] == "partition"]
    assert parts and all(r["source_table"] and r["expression"] for r in parts)
    # legacy: no calc group, no RLS, partitions bound to the three source-named schemas
    legacy = [r for r in doc if r["model_set"] == "legacy"]
    assert not any(r["object_type"] in {"calculation_item", "rls_filter"} for r in legacy)
    assert {r["source_table"].split(".")[0] for r in legacy if r["object_type"] == "partition"} == \
        {"legacy_ops", "legacy_finance", "legacy_reporting"}


def test_relationships_are_sql_join_maps_on_physical_names(exported):
    rels = exported[2]["Model_Relationships.csv"]
    assert rels
    for r in rels:
        assert r["from_source"] and r["to_source"], r
        assert r["sql_join"].startswith(f"from {r['from_source']} as f left join {r['to_source']} as d on f.")
        assert "?" not in r["sql_join"]
    inactive = [r for r in rels if r["is_active"] == "false"]
    assert inactive and all("USERELATIONSHIP" in r["sql_join"] for r in inactive)
    assert {r["key_enforced"].split(":")[0] for r in rels if r["model_set"] == "legacy"} == {"no"}
    assert any('"fxDealerId"' in r["sql_join"] for r in rels if r["model_set"] == "legacy")


def test_report_field_usage_covers_every_page_and_resolves_every_field(exported):
    usage = exported[2]["Report_Field_Usage.csv"]
    order = json.loads((REPORT_DEF / "pages" / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    names = [json.loads((REPORT_DEF / "pages" / p / "page.json").read_text(encoding="utf-8"))["displayName"]
             for p in order]
    assert [p for p in dict.fromkeys(r["page"] for r in usage if r["page_order"] != "-1")] == names
    for r in usage:
        assert r["model"] == "F&I Data Platform"
        if r["field_kind"] == "measure":
            assert r["dax"], r
        elif r["entity"] != "Time Intelligence":
            assert r["source_table"] and r["source_column"], r
    # the calculation group is used, and its filter value is read from the PBIR filter
    assert any(r["entity"] == "Time Intelligence" and "T12M" in r["filter_values"] for r in usage)
    # every measure field in the PBIR files appears once per visual and role
    raw = 0
    for vj in REPORT_DEF.glob("pages/*/visuals/*/visual.json"):
        q = json.loads(vj.read_text(encoding="utf-8"))["visual"].get("query", {}).get("queryState", {})
        raw += len({(role, p["field"]["Measure"]["Property"]) for role, b in q.items()
                    for p in b.get("projections", []) if "Measure" in p["field"]})
    assert raw == sum(1 for r in usage if r["field_kind"] == "measure" and r["role"] not in {"sort", "filter",
                                                                                           "formatting"})


def test_measure_report_map_statuses_and_dependencies(exported):
    mm = exported[2]["Measure_Report_Map.csv"]
    usage = exported[2]["Report_Field_Usage.csv"]
    report = {r["measure"]: r for r in mm if r["model"] == "F&I Data Platform"}
    used_in_report = {r["field"] for r in usage if r["field_kind"] == "measure"}
    assert {m for m, r in report.items() if r["status"] == "used"} == used_in_report
    lr = report["Loss Ratio (on earned premium)"]
    assert "Earned Premium" in lr["depends_on"] and lr["status"] == "used"
    inc = report["Incurred Claims (accident month)"]
    assert "Loss Ratio (on earned premium)" in inc["used_by_measures"]
    assert inc["status"] in {"used", "used_via_measure"}
    assert {r["status"] for r in mm if r["model_set"] == "legacy"} == {"no_report"}
    assert {r["status"] for r in mm} <= {"used", "used_via_measure", "unused", "no_report"}
    # domain models share the report's use through the dbt metric (one definition, many models)
    dom = [r for r in mm if r["model_set"] == "modern_domain" and r["dbt_metric"] == lr["dbt_metric"]]
    assert dom and all(r["status"] == "used" for r in dom)


def test_export_is_deterministic_and_cli_writes_the_same_files(exported, tmp_path):
    out, _counts, _rows = exported
    assert semantic_main(["export-docs", "--out", str(tmp_path)]) == 0
    for f in FILES:
        assert (tmp_path / f).read_bytes() == (out / f).read_bytes(), f


# --------------------------------------------------------------------------- tiny synthetic model + report
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_unused_and_via_measure_status_on_a_tiny_model(tmp_path):
    m = tmp_path / "models" / "Tiny.SemanticModel" / "definition"
    _write(m / "tables" / "Fact.tmdl", "\n".join([
        "table Fact", "",
        "\tmeasure Base = SUM(Fact[Amt])", "",
        "\tmeasure Top = [Base] * 2", "",
        "\tmeasure Orphan = COUNTROWS(Fact)", "",
        "\tcolumn Amt", "\t\tdataType: double", "\t\tsourceColumn: amt", "",
        "\tcolumn 'Dim Key'", "\t\tdataType: int64", "\t\tsourceColumn: dim_key", "",
        "\tannotation source_table = core.fct_tiny", ""]))
    _write(m / "tables" / "Dim.tmdl", "table Dim\n\n\tcolumn 'Dim Key'\n\t\tsourceColumn: dim_key\n\n"
                                       "\tannotation source_table = core.dim_tiny\n")
    _write(m / "relationships.tmdl", "relationship r1\n\tfromColumn: Fact.'Dim Key'\n\ttoColumn: Dim.'Dim Key'\n")
    rep = tmp_path / "reports" / "Tiny.Report"
    _write(rep / "definition.pbir", json.dumps({"datasetReference": {"byPath": {
        "path": "../../models/Tiny.SemanticModel"}}}))
    _write(rep / "definition" / "pages" / "pages.json", json.dumps({"pageOrder": ["p1"]}))
    _write(rep / "definition" / "pages" / "p1" / "page.json", json.dumps({"name": "p1", "displayName": "Only"}))
    visual = {"name": "v1", "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Fact"}}, "Property": "Top"}}},
        {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Fact"}},
                                                             "Property": "Amt"}}, "Function": 0}}}]}}}}}
    _write(rep / "definition" / "pages" / "p1" / "visuals" / "v1" / "visual.json", json.dumps(visual))
    counts = export(tmp_path / "out", model_sets=(("modern_report", tmp_path / "models"),),
                    report_roots=(tmp_path / "reports",))
    assert counts["Model_Relationships.csv"] == 1
    status = {r["measure"]: r["status"] for r in _read(tmp_path / "out" / "Measure_Report_Map.csv")}
    assert status == {"Base": "used_via_measure", "Top": "used", "Orphan": "unused"}
    usage = _read(tmp_path / "out" / "Report_Field_Usage.csv")
    agg = next(r for r in usage if r["field_kind"] == "aggregation")
    assert agg["field"] == "Sum(Amt)" and agg["source_column"] == "amt" and agg["source_table"] == "core.fct_tiny"
    (rel,) = _read(tmp_path / "out" / "Model_Relationships.csv")
    assert rel["sql_join"] == "from core.fct_tiny as f left join core.dim_tiny as d on f.dim_key = d.dim_key"
