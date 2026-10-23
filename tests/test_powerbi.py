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


def test_five_pages_in_order():
    pages_dir = build.REPORT_DIR / "definition" / "pages"
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    names = [json.loads((pages_dir / p / "page.json").read_text(encoding="utf-8"))["displayName"] for p in order]
    assert names == ["Executive overview", "Dealer scorecard", "Claims & IBNR", "Regulatory funded reserve",
                     "Finance"]


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
