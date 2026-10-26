"""Power BI project (powerbi/): generated files are current, valid against Microsoft's schemas, and every
visual field exists in the reporting semantic model. The live check (Postgres) skips without a server."""

from __future__ import annotations

import copy
import json
import re

import pytest

from powerbi import build, model, report, schemas
from powerbi import validate as pv
from semantic import spec
from semantic.metrics import has_time_word

field_ref = report.field_ref

MODEL_DEF = build.MODEL_DIR / "definition"


@pytest.fixture(scope="module")
def files():
    return build.build_files()


@pytest.fixture(scope="module")
def sem():
    return pv.read_model()


def _visuals() -> list[tuple[str, dict]]:
    return [(str(f.relative_to(build.HERE)), json.loads(f.read_text(encoding="utf-8")))
            for f in sorted(build.REPORT_DIR.rglob("visual.json"))]


# ---------------------------------------------------------------------- generated and current
def test_committed_project_is_up_to_date(files):
    assert build.check(files) == [], "run: python -m powerbi"


def test_pbip_binds_the_report_to_the_reporting_model_by_path():
    pbip = json.loads(build.PBIP.read_text(encoding="utf-8"))
    assert pbip["artifacts"] == [{"report": {"path": build.REPORT_DIR.name}}]
    pbir = json.loads((build.REPORT_DIR / "definition.pbir").read_text(encoding="utf-8"))
    assert pbir["datasetReference"] == {"byPath": {"path": f"../{build.MODEL_DIR.name}"}}
    assert (build.MODEL_DIR / "definition.pbism").exists()


def test_seven_pages_in_order():
    pages_dir = build.REPORT_DIR / "definition" / "pages"
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    names = [json.loads((pages_dir / p / "page.json").read_text(encoding="utf-8"))["displayName"] for p in order]
    assert names == ["Executive overview", "Dealer scorecard", "Claims & IBNR", "Regulatory funded reserve",
                     "Finance", "Experience", "New Reserves"]


# ---------------------------------------------------------------------- V6: Experience (1b), New Reserves (11b)
def _page(key: str) -> report.Page:
    return next(p for p in (f() for f in report.PAGES) if p.key == key)


def _page_visuals(key: str) -> list[dict]:
    return _page(key).visuals


def test_experience_view_by_offers_the_planned_fields_in_order():
    labels = [label for label, _, _ in model.EXPERIENCE_VIEW_FIELDS]
    assert labels == ["Date", "Product", "Dealer", "State", "Plan", "Make", "Entered year", "Term (months)"]
    text = (MODEL_DEF / "tables" / f"{model.EXPERIENCE_VIEW}.tmdl").read_text(encoding="utf-8")
    assert "annotation PBI_FieldParameter = true" in text
    for i, (label, _, _) in enumerate(model.EXPERIENCE_VIEW_FIELDS):
        assert f'("{label}", NAMEOF(' in text and f"), {i})" in text, label
    assert f"ref table '{model.EXPERIENCE_VIEW}'" in (MODEL_DEF / "model.tmdl").read_text(encoding="utf-8")


def test_every_view_by_pick_slices_every_experience_measure():
    """Each Experience measure reads a fact related straight to every View-by table, so no pick repeats a
    total on every row (and every view adds up to the same total)."""
    plan, columns = model.load_plan()
    dm = model.portfolio_model(plan, columns)
    related = {(f, t) for f, _, t, _ in dm.relationships}
    used = {field_ref(pr["field"])[2] for v in _page_visuals("experience")
            for role in v["visual"].get("query", {}).get("queryState", {}).values()
            for pr in role["projections"] if "Measure" in pr["field"]}
    assert used >= {"Earned Pct", "Cancel Pct", "Earned Reserve", "Claims Paid"}
    facts = {t for m in dm.measures if m.name in used for t in m.tables if t in dm.facts}
    assert len(facts) >= 4, facts
    for fact in facts:
        for _, table, _ in model.EXPERIENCE_VIEW_FIELDS:
            assert (fact, table) in related, f"{fact} has no relationship to {table}"


def test_experience_visuals_use_the_view_by_field_parameter_and_closed_periods():
    page = _page("experience")
    flt =page.page_json()["filterConfig"]["filters"]
    assert any(field_ref(x["field"]) == ("Column", "Posting Period", "Is Closed") for x in flt)
    with_param = [v for v in page.visuals
                  if any("fieldParameters" in r for r in v["visual"].get("query", {}).get("queryState", {}).values())]
    assert {v["visual"]["visualType"] for v in with_param} == {"tableEx", "clusteredBarChart"}
    for v in with_param:
        for r in v["visual"]["query"]["queryState"].values():
            for fp in r.get("fieldParameters", []):
                assert field_ref(fp["parameterExpr"]) == ("Column", model.EXPERIENCE_VIEW, model.EXPERIENCE_VIEW)
                assert field_ref(r["projections"][fp["index"]]["field"])[1] == model.EXPERIENCE_VIEW


def test_new_reserve_visuals_read_sale_postings_only():
    """'New reserve' = claims reserve on SALE postings; the net card is the only unfiltered reserve visual."""
    for v in _page_visuals("new_reserves"):
        qs = v["visual"].get("query", {}).get("queryState", {})
        labels = {pr.get("displayName") for r in qs.values() for pr in r["projections"]}
        if "New reserve" not in labels:
            continue
        sale = [f for f in v.get("filterConfig", {}).get("filters", [])
                if field_ref(f["field"]) == ("Column", "Disbursement", "Txn Type")]
        assert sale, v["name"]
        vals = sale[0]["filter"]["Where"][0]["Condition"]["In"]["Values"]
        assert vals == [[{"Literal": {"Value": "'SALE'"}}]]


# ---------------------------------------------------------------------- field references
def test_every_visual_field_exists_in_the_model(sem):
    problems = [p for where, doc in _visuals() for p in pv.check_fields(doc, sem, where)]
    assert problems == []


def test_checker_catches_a_missing_or_miskinded_field(sem):
    where, doc = next((w, d) for w, d in _visuals() if d["visual"]["visualType"] == "card")
    bad = copy.deepcopy(doc)
    proj = next(iter(bad["visual"]["query"]["queryState"].values()))["projections"][0]
    proj["field"]["Measure"]["Property"] = "No Such Measure"
    assert any("No Such Measure" in p for p in pv.check_fields(bad, sem, where))
    # a column referenced as a measure is also an error
    bad2 = copy.deepcopy(doc)
    p2 = next(iter(bad2["visual"]["query"]["queryState"].values()))["projections"][0]
    p2["field"] = report.measure("Dealer Name", "Dealer")
    p2["queryRef"] = "Dealer.Dealer Name"
    assert any("Dealer[Dealer Name]" in p for p in pv.check_fields(bad2, sem, where))
    # a calculation-item filter must name a real item
    bad3 = copy.deepcopy(doc)
    bad3["filterConfig"] = {"filters": [report.time_item("Last Week", "x")]}
    assert any("Last Week" in p for p in pv.check_fields(bad3, sem, where))


def test_time_windows_come_only_from_the_calculation_group(sem):
    """Visuals use base measures + a Time Intelligence filter: no measure in the model carries a time word."""
    names = {m for ms in sem.measures.values() for m in ms}
    assert not [n for n in names if has_time_word(n)]
    used_items = set()
    for _, doc in _visuals():
        for flt in (doc.get("filterConfig") or {}).get("filters", []):
            if report.field_ref(flt["field"])[1] == report.TI[0]:
                for row in flt["filter"]["Where"][0]["Condition"]["In"]["Values"]:
                    used_items.add(row[0]["Literal"]["Value"].strip("'"))
    assert {"T12M", "MTD", "QTD", "YTD"} <= used_items <= sem.calc_items[report.TI[0]]


def test_layout_stays_on_the_grid_without_overlaps():
    assert pv.check_layout() == []


# ---------------------------------------------------------------------- Microsoft schemas
def test_every_json_file_validates_against_its_published_schema():
    problems, skipped = pv.check_schemas()
    if skipped and not problems:
        pytest.skip(f"schemas not downloadable here: {skipped[0]}")
    assert problems == []


def test_every_json_file_names_a_microsoft_schema():
    for f in pv.json_files():
        doc = json.loads(f.read_text(encoding="utf-8"))
        url = doc.get("$schema")
        if "RegisteredResources" in f.parts:
            assert url is None and set(doc) >= {"name", "dataColors", "visualStyles"}, f
        else:
            assert url and url.startswith(schemas.BASE), f


# ---------------------------------------------------------------------- reporting model
def test_reporting_model_measures_map_to_metrics_or_actuarial_output():
    text = (MODEL_DEF / "tables" / f"{spec.MEASURES}.tmdl").read_text(encoding="utf-8")
    measures = re.findall(r"^\tmeasure ('(?:[^']|'')+'|\S+)", text, re.M)
    annotated = re.findall(r"annotation dbt_metric = (\S+)", text)
    assert len(measures) == len(annotated) > 70
    plan, _ = model.load_plan()
    assert set(annotated) == {m.metric for dm in plan.models.values() for m in dm.measures}
    ibnr = (MODEL_DEF / "tables" / f"{model.IBNR}.tmdl").read_text(encoding="utf-8")
    assert ibnr.count("annotation source = actuarial") == len(model.IBNR_MEASURES)


def test_external_roles_cannot_see_internal_only_tables():
    for role in ("Dealer User", "Tenant Partner"):
        text = (MODEL_DEF / "roles" / f"{role}.tmdl").read_text(encoding="utf-8")
        for t in model.INTERNAL_ONLY:
            assert f"tablePermission '{t}' = FALSE()" in text, (role, t)
        assert "tablePermission Dealer =" in text
    internal = (MODEL_DEF / "roles" / "Internal.tmdl").read_text(encoding="utf-8")
    assert "tablePermission" not in internal


def test_reporting_model_uses_the_postgres_source_and_no_reserved_table_names():
    tables = [p.stem for p in (MODEL_DEF / "tables").glob("*.tmdl")]
    assert "Measures" not in tables, "'Measures' is a reserved table name in Power BI Desktop"
    expr = (MODEL_DEF / "expressions.tmdl").read_text(encoding="utf-8")
    assert '"localhost:5432"' in expr and '"vsc_analytics"' in expr
    assert "PostgreSQL.Database(PostgresServer, PostgresDatabase)" in \
        (MODEL_DEF / "tables" / "Dealer.tmdl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- live: model vs the Postgres server
_COMPATIBLE = {
    # DuckDB HUGEINT sums land as numeric on the server; whole numbers load into an int64 column.
    "int64": {"smallint", "integer", "bigint", "numeric"},
    "double": {"double precision", "real", "numeric"},
    "decimal": {"numeric", "double precision"},
    "string": {"text", "character varying", "character"},
    "boolean": {"boolean"},
    "dateTime": {"date", "timestamp without time zone", "timestamp with time zone"},
}


def _imported_tables() -> dict[tuple[str, str], dict[str, str]]:
    """(schema, table) -> {source column: TMDL type} for every M (imported) table of the model."""
    out = {}
    for f in (MODEL_DEF / "tables").glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        src = re.search(r'Source\{\[Schema = "(\w+)", Item = "(\w+)"\]\}', text)
        if not src:
            continue
        cols = {}
        for block in text.split("\n\tcolumn ")[1:]:
            dtype = re.search(r"^\t\tdataType: (\w+)", block, re.M)
            source = re.search(r"^\t\tsourceColumn: (\w+)", block, re.M)
            if dtype and source:
                cols[source.group(1)] = dtype.group(1)
        out[src.groups()] = cols
    return out


def test_imported_tables_match_the_postgres_server():
    """Every partition's table and source column exists on the server with a compatible type (so Refresh
    in Desktop cannot fail on a missing column). Skips when Postgres is not running or not synced."""
    from warehouse.postgres import sync as pg

    s = pg.Settings.from_env()
    reason = pg.available(s)
    if reason:
        pytest.skip(f"Postgres not available: {reason}")
    tables = _imported_tables()
    assert len(tables) >= 30
    with s.connect(reader=True, autocommit=True) as con:
        if not con.execute("select to_regclass('core.dim_dealer')").fetchone()[0]:
            pytest.skip("not synced yet (python -m warehouse.postgres sync)")
        problems = []
        for (schema, table), cols in sorted(tables.items()):
            live = dict(con.execute("select column_name, data_type from information_schema.columns "
                                    "where table_schema = %s and table_name = %s", (schema, table)).fetchall())
            if not live:
                problems.append(f"{schema}.{table}: missing or not readable by the reader login")
                continue
            for c, t in cols.items():
                if c not in live:
                    problems.append(f"{schema}.{table}.{c}: missing")
                elif live[c] not in _COMPATIBLE[t]:
                    problems.append(f"{schema}.{table}.{c}: {live[c]} vs {t}")
    assert problems == []
