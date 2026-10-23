"""Semantic layer: generated from metrics.yml, one calc group, dynamic RLS, no time-suffixed copies."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from semantic import spec
from semantic.__main__ import main as semantic_main
from semantic.build import CALC_ITEMS, generate, load_lock, make_plan, read_warehouse_columns, role_filters
from semantic.metrics import Catalog, Measure, Metric, TranslationError, has_time_word, measure_dax, metric_dax

ROOT = Path(__file__).resolve().parents[1]
METRICS = ROOT / "dbt" / "models" / "metrics.yml"
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
MEASURE_RE = re.compile(r"^\tmeasure (?:'((?:[^']|'')+)'|(\S+)) =", re.M)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("semantic") / "model"
    plan = generate(out)
    return out, plan


def _measures_in(tmdl: str) -> list[tuple[str, str]]:
    """(measure name, dbt_metric annotation) for every measure in a Measures.tmdl file."""
    found = []
    blocks = re.split(r"\n(?=\t(?:/// |measure ))", tmdl)
    for b in blocks:
        m = MEASURE_RE.search(b)
        if m:
            ann = re.search(r"annotation dbt_metric = (\w+)", b)
            found.append(((m.group(1) or m.group(2)).replace("''", "'"), ann.group(1) if ann else ""))
    return found


# --------------------------------------------------------------------------- the core contract
def test_every_measure_maps_to_a_metric(built):
    out, _ = built
    metrics = {m["name"] for m in yaml.safe_load(METRICS.read_text(encoding="utf-8"))["metrics"]}
    total = 0
    for f in out.glob(f"*.SemanticModel/definition/tables/{spec.MEASURES}.tmdl"):
        found = _measures_in(f.read_text(encoding="utf-8"))
        assert found, f
        for name, metric in found:
            assert metric in metrics, f"{f.parent.parent.parent.name}: measure {name!r} -> {metric!r}"
        total += len(found)
    assert total == sum(len(dm.measures) for dm in built[1].models.values())


def test_no_measure_is_a_time_suffixed_copy(built):
    out, _ = built
    for f in out.glob(f"*.SemanticModel/definition/tables/{spec.MEASURES}.tmdl"):
        for name, _ in _measures_in(f.read_text(encoding="utf-8")):
            assert not has_time_word(name), name
            assert not re.search(r"(MTD|QTD|YTD|PY|YoY|KPI|T12M?|TTM)\b", name, re.I), name


def test_every_metric_is_a_measure_or_a_calc_item_or_explained(built):
    _, plan = built
    cat = Catalog.load(METRICS)
    for name in cat.metrics:
        assert name in plan.measures or name in plan.time_variants or name in plan.not_exported, name
    items = {i for i, _, _ in CALC_ITEMS}
    for name, tv in plan.time_variants.items():
        assert tv["calculation_item"] in items, name
        assert tv["base_metric"] in plan.measures, f"{name}: base {tv['base_metric']} is not a measure"
    # Only deprecated V1 models are skipped; every V3 core fact is exported.
    assert all("V1" in why for why in plan.not_exported.values()), plan.not_exported


def test_four_domain_models_each_with_exactly_one_calc_group(built):
    out, plan = built
    assert set(plan.models) == {"Sales", "Claims", "Finance", "Compliance"}
    for d in plan.models:
        tables = out / f"{d}.SemanticModel" / "definition" / "tables"
        groups = [p for p in tables.glob("*.tmdl") if "calculationGroup" in p.read_text(encoding="utf-8")]
        assert [p.stem for p in groups] == [spec.CALC_GROUP], d
        text = groups[0].read_text(encoding="utf-8")
        for item in ("Current", "MTD", "QTD", "YTD", "PY", "'YoY %'", "T12M"):
            assert f"calculationItem {item} =" in text, (d, item)
        assert (out / f"{d}.SemanticModel" / "definition.pbism").exists()
        model = (out / f"{d}.SemanticModel" / "definition" / "model.tmdl").read_text(encoding="utf-8")
        assert "discourageImplicitMeasures" in model
        for t in [spec.MEASURES, spec.CALC_GROUP, spec.DRILL, *plan.models[d].facts, *plan.models[d].dims]:
            assert (tables / f"{t}.tmdl").exists(), (d, t)


def test_ratio_inputs_live_in_the_same_model(built):
    _, plan = built
    cat = Catalog.load(METRICS)
    for d, dm in plan.models.items():
        present = {m.metric for m in dm.measures}
        for m in dm.measures:
            assert set(cat.metrics[m.metric].inputs) <= present, (d, m.metric)


def test_relationships_point_at_real_columns_and_single_paths(built):
    _, plan = built
    lock = load_lock()
    for d, dm in plan.models.items():
        tables = set(dm.facts) | set(dm.dims)
        for f, fc, t, tc in dm.relationships:
            assert f in tables and t in tables, (d, f, t)
            assert fc in dict(lock[spec.ALL_TABLES[f].source]), (d, f, fc)
            assert tc in dict(lock[spec.ALL_TABLES[t].source]), (d, t, tc)
        # star: each fact relates to the period table (calc group) and, for external roles, Dealer + Tenant
        for f in dm.facts:
            targets = {t for ff, _, t, _ in dm.relationships if ff == f}
            assert spec.PERIOD in targets or spec.FACTS[f].period_free, (d, f)
            if set(dm.roles) - {"Internal"}:
                assert {"Dealer", "Tenant"} <= targets, f"{d}: {f} would leak past RLS"


def test_dynamic_rls_roles(built):
    out, plan = built
    for d, dm in plan.models.items():
        roles = out / f"{d}.SemanticModel" / "definition" / "roles"
        assert {p.stem for p in roles.glob("*.tmdl")} == set(dm.roles)
        if d == "Compliance":
            assert dm.roles == ("Internal",) and spec.SECURITY not in dm.dims
            continue
        dealer = (roles / "Dealer User.tmdl").read_text(encoding="utf-8")
        assert "USERPRINCIPALNAME()" in dealer
        for table in ("Tenant", "Dealer", "Contract", spec.SECURITY):
            assert re.search(rf"tablePermission '?{table}'? =", dealer), (d, table)
        sec = (out / f"{d}.SemanticModel" / "definition" / "tables" / f"{spec.SECURITY}.tmdl").read_text("utf-8")
        assert "isHidden" in sec
        assert not any(spec.SECURITY in (f, t) for f, _, t, _ in dm.relationships), "security table is disconnected"
    assert role_filters("Internal", {"Dealer", "Tenant"}) == {}
    partner = role_filters("Tenant Partner", {"Dealer", "Tenant", "Contract"})
    assert "Tenant Key" in partner["Dealer"] and "[Dealer Key] IN" not in partner["Dealer"]


PERMISSION_RE = re.compile(r"^\ttablePermission (?:'((?:[^']|'')+)'|(\S+)) = (.*)$", re.M)
SCOPE_KEYS = {"contract_key", "dealer_key", "tenant_key"}


def _permissions(path: Path) -> dict[str, str]:
    return {(m.group(1) or m.group(2)).replace("''", "'"): m.group(3)
            for m in PERMISSION_RE.finditer(path.read_text(encoding="utf-8"))}


def test_rls_covers_every_table_with_a_contract_dealer_or_tenant_key(built):
    """No row leak: every keyed table has its own filter in each external role, or is a fact that a
    filtered dim reaches (filters flow dim -> fact only, never across to a sibling dim)."""
    out, plan = built
    lock = load_lock()
    for d, dm in plan.models.items():
        keyed = {t for t in dm.facts + dm.dims if SCOPE_KEYS & set(dict(lock.get(spec.ALL_TABLES[t].source, [])))}
        if dm.payee_sources:
            keyed.add(spec.PAYEE_ACCESS)
        for role in set(dm.roles) - {"Internal"}:
            perms = _permissions(out / f"{d}.SemanticModel" / "definition" / "roles" / f"{role}.tmdl")
            covered = set(perms)
            for f, _, t, _ in dm.relationships:
                if f in dm.facts and t in perms:
                    covered.add(f)
            assert not keyed - covered, f"{d}/{role}: unfiltered {sorted(keyed - covered)}"
            for t in ("Contract Terms", "Contract Finance", "Contract Vehicle", "Contract Actuarial Class",
                      "Contract Claim Summary"):
                if t in dm.dims:
                    assert "'Contract'[Contract Key]" in perms[t], (d, role, t)
                    key = "Dealer Key" if role == "Dealer User" else "Tenant Key"
                    assert f"'Contract'[{key}]" in perms[t], (d, role, t)
            if role == "Dealer User" and "Account Holder" in dm.dims:
                assert "Dealer Key" in perms["Account Holder"], f"{d}: payees not scoped to the dealer"
                bridge = out / f"{d}.SemanticModel" / "definition" / "tables" / f"{spec.PAYEE_ACCESS}.tmdl"
                text = bridge.read_text(encoding="utf-8")
                assert "= calculated" in text and "isHidden" in text
                assert not any(spec.PAYEE_ACCESS in (f, t) for f, _, t, _ in dm.relationships)
    with pytest.raises(TranslationError, match="Contract"):
        role_filters("Dealer User", {"Contract Finance", "Dealer"})


def test_display_labels_are_unique_and_loss_ratios_name_their_denominator(tmp_path):
    cat = Catalog.load(METRICS)
    labels = [" ".join(m.label.lower().split()) for m in cat.metrics.values()]
    assert len(labels) == len(set(labels))
    for m in cat.metrics.values():
        if "loss ratio" in m.label.lower():
            assert "(on " in m.label, f"{m.name}: label must name the denominator, e.g. '(on earned premium)'"
    plan = make_plan(cat, load_lock())
    names = [mm.name for mm in plan.measures.values() if "loss ratio" in mm.name.lower()]
    assert all("(on " in n for n in names) and len(set(names)) == len(names), names
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    doc["metrics"].append({"name": "loss_ratio_copy", "label": "Loss ratio (on earned premium)", "type": "ratio",
                           "type_params": {"numerator": "incurred_claims", "denominator": "earned_reserve"}})
    bad = tmp_path / "metrics.yml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TranslationError, match="display label"):
        make_plan(Catalog.load(bad), load_lock())


def test_field_parameter_drill_levels_reference_model_columns(built):
    out, plan = built
    lock = load_lock()
    for d, dm in plan.models.items():
        text = (out / f"{d}.SemanticModel" / "definition" / "tables" / f"{spec.DRILL}.tmdl").read_text("utf-8")
        assert "ParameterMetadata" in text and "NAMEOF(" in text
        assert len(dm.drill) >= 5, d
        for _, t, c in dm.drill:
            assert t in dm.dims and c in dict(lock[spec.ALL_TABLES[t].source]), (d, t, c)


def test_incremental_refresh_on_large_facts(built):
    out, _ = built
    text = (out / "Finance.SemanticModel" / "definition" / "tables" / "Earned Disbursement.tmdl").read_text("utf-8")
    assert "refreshPolicy" in text and "RangeStart" in text and "POSTING_PERIOD_KEY" in text
    small = (out / "Finance.SemanticModel" / "definition" / "tables" / "Commission.tmdl").read_text("utf-8")
    assert "refreshPolicy" not in small and "mode: import" in small


def test_no_pipeline_metadata_or_pii_columns_exposed(built):
    out, _ = built
    for f in out.glob("*.SemanticModel/definition/tables/*.tmdl"):
        text = f.read_text(encoding="utf-8")
        assert "sourceColumn: _" not in text, f
        assert "CUSTOMER_KEY" not in text, f


def test_output_is_deterministic_and_committed_copy_is_current(built, tmp_path):
    out, _ = built
    again = tmp_path / "again"
    generate(again)
    a = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    b = {p.relative_to(again): p.read_bytes() for p in again.rglob("*") if p.is_file()}
    assert a == b
    assert semantic_main(["--check"]) == 0, "semantic/model is stale: run python -m semantic"


# --------------------------------------------------------------------------- DAX translation units
def _m(agg: str, expr: str, nad: str | None = None) -> Measure:
    return Measure(name="x", agg=agg, expr=expr, model="m", non_additive=nad)


@pytest.mark.parametrize(("agg", "expr", "dax"), [
    ("sum", "paid_amount", "SUM('F'[Paid Amount])"),
    ("count", "line_key", "COUNTROWS('F')"),
    ("count", "claim_key", "COUNT('F'[Claim Key])"),
    ("count_distinct", "claim_key", "DISTINCTCOUNT('F'[Claim Key])"),
    ("average", "process_days", "AVERAGE('F'[Process Days])"),
    ("sum_boolean", "is_late", "CALCULATE(COUNTROWS('F'), 'F'[Is Late] = TRUE())"),
    ("sum", "case when to_status = 'OPEN' then 1 else 0 end", "CALCULATE(COUNTROWS('F'), 'F'[To Status] = \"OPEN\")"),
    ("sum", "case when s in ('A', 'B') then 1 else 0 end", "CALCULATE(COUNTROWS('F'), 'F'[S] IN {\"A\", \"B\"})"),
    ("sum", "case when is_advance then amount else 0 end", "CALCULATE(SUM('F'[Amount]), 'F'[Is Advance] = TRUE())"),
])
def test_measure_dax(agg, expr, dax):
    assert measure_dax(_m(agg, expr), "F", "line_key") == dax


def test_balance_measure_takes_last_closed_period():
    dax = measure_dax(_m("sum", "unearned_premium_eom", "max"), "F", None)
    assert "MAX('F'[Posting Period Key])" in dax and "'Posting Period'[Posting Period Key] = _period" in dax
    # capped at the last CLOSED period, never the open month
    assert "MAX('Reporting Date'[Last Closed Period Key])" in dax and "'F'[Posting Period Key] <= _cap" in dax


@pytest.mark.parametrize(("expr", "dax"), [
    ("case when status_group = 'paid' then paid_amount else 0 end",
     "CALCULATE(SUM('F'[Paid Amount]), 'F'[Status Group] = \"paid\")"),
    ("case when t in ('A', 'B') then amt else 0 end", "CALCULATE(SUM('F'[Amt]), 'F'[T] IN {\"A\", \"B\"})"),
])
def test_filtered_sum_dax(expr, dax):
    assert measure_dax(_m("sum", expr), "F", None) == dax


def test_calc_group_and_balances_cap_at_last_closed_period(built):
    """Every capped item uses Last Closed Period Key (Sep for an Oct 21 as-of), never the open month."""
    out, plan = built
    capped = [i for i, dax, _ in CALC_ITEMS if "VAR _p =" in dax]
    assert set(capped) == {"MTD", "QTD", "YTD", "T12M"}
    for _, dax, _ in CALC_ITEMS:
        assert "Current Period Key" not in dax
        if "VAR _p =" in dax:
            assert "MAX('Reporting Date'[Last Closed Period Key])" in dax
    for metric in ("contracts_in_force", "unearned_premium_balance", "dealers_on_watch"):
        assert "'Reporting Date'[Last Closed Period Key]" in plan.measures[metric].dax, metric
    text = (out / "Sales.SemanticModel" / "definition" / "tables" / f"{spec.CALC_GROUP}.tmdl").read_text("utf-8")
    assert "Last Closed Period Key" in text and "Current Period Key" not in text


def test_payee_access_column_types_come_from_the_source(built):
    out, plan = built
    lock = load_lock()
    for d, dm in plan.models.items():
        if not dm.payee_sources:
            continue
        text = (out / f"{d}.SemanticModel" / "definition" / "tables" / f"{spec.PAYEE_ACCESS}.tmdl").read_text("utf-8")
        types = dict(re.findall(r"column '([^']+)'\n\t\tdataType: (\w+)", text))
        f, c = dm.payee_sources[0]
        src = dict(lock[spec.FACTS[f].source])
        assert types == {"Account Holder Key": spec.tmdl_type(src[c]), "Dealer Key": spec.tmdl_type(src["dealer_key"]),
                         "Tenant Key": spec.tmdl_type(src["tenant_key"])}, d
        # the role filter compares Account Holder Key (string) with the bridge column: same type
        acct = dict(lock[spec.DIMS["Account Holder"].source])["account_holder_key"]
        assert types["Account Holder Key"] == spec.tmdl_type(acct) == "string"


def test_every_site_headline_kpi_maps_to_a_measure(built):
    _, plan = built
    for label, metric in spec.HEADLINE_KPIS.items():
        assert metric in plan.measures, f"site KPI {label!r}: metric {metric!r} is not a measure"
    assert spec.HEADLINE_KPIS_OUTSIDE, "keep the reasons for KPIs that stay outside the model"
    # the claims headline measures come from the claim-grain fact that covers both admin systems
    for metric in ("claims_opened", "denial_rate", "paid_claim_severity", "open_claims_aged_30_plus"):
        assert "Claim Summary" in plan.measures[metric].tables, metric
    assert "USERELATIONSHIP('Claim Summary'[Reported Month]" in plan.measures["claims_opened"].dax
    claims = plan.models["Claims"]
    assert ("Claim Summary", "reported_month", spec.PERIOD, "period_start") in claims.inactive
    comp = plan.models["Compliance"]
    assert {"Funded Reserve Filing", "Regulatory Line Value"} <= set(comp.facts)
    assert ("Funded Reserve Filing", "provider_key", "Obligor", "obligor_key") in comp.relationships


# --------------------------------------------------------------------------- metric governance
OWNER_ROLES = {"Financial Analyst II", "Financial Analyst I", "Senior Data Analyst (dealer & claims)",
               "Senior Data Analyst (regulatory)", "Data Scientist", "Senior Power BI Specialist",
               "Reporting Manager"}
OVERVIEW_FUNCS = ("overview_money", "page_overview", "overview_dealer_watch_kpi")


def _slug(heading: str) -> str:
    """GitHub-style heading anchor."""
    return re.sub(r"[^a-z0-9 -]", "", heading.strip().lower()).replace(" ", "-")


def _norm_tile(label: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", label)


def overview_tile_labels() -> set[str]:
    """Every Kpi label the executive overview builds (f-string parts become ``{}``)."""
    import ast

    tree = ast.parse((ROOT / "reports" / "build.py").read_text(encoding="utf-8"))
    labels: set[str] = set()
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name in OVERVIEW_FUNCS):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Kpi" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant):
                    labels.add(str(a.value))
                elif isinstance(a, ast.Name):  # a shared label constant (reports.build or reports.dealer.watch)
                    import reports.build
                    import reports.dealer.watch

                    mods = (reports.build, reports.dealer.watch)
                    labels.add(next(str(getattr(m, a.id)) for m in mods if hasattr(m, a.id)))
                elif isinstance(a, ast.JoinedStr):
                    labels.add("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in a.values))
                else:
                    raise AssertionError(f"{fn.name}: Kpi label must be a literal, a constant or an f-string")
    return labels


def test_every_metric_names_an_owner_and_a_certification_status():
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    anchors = {_slug(h) for h in re.findall(r"^#+ (.+)$", (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8"),
                                            re.M)}
    cat = Catalog.load(METRICS)
    for m in doc["metrics"]:
        meta = (m.get("config") or {}).get("meta") or {}
        assert meta.get("owner") in OWNER_ROLES, f"{m['name']}: config.meta.owner must be a role title"
        assert isinstance(meta.get("certified"), bool), f"{m['name']}: config.meta.certified must be true/false"
        if meta["certified"]:
            mt = cat.metrics[m["name"]]
            desc = mt.description or (
                cat.measures[mt.type_params["measure"]].description if mt.type == "simple" else "")
            assert desc.strip(), f"certified metric {m['name']}: needs a description"
            path, _, anchor = str(meta.get("definition_of_record", "")).partition("#")
            assert path == "docs/METRICS.md" and anchor in anchors, \
                f"certified metric {m['name']}: definition_of_record must be a docs/METRICS.md section"


def test_no_two_certified_metrics_share_a_label_or_measure_name(built):
    _, plan = built
    cat = Catalog.load(METRICS)
    certified = [m for m in cat.metrics.values() if m.certified]
    labels = [" ".join(m.label.lower().split()) for m in certified]
    assert len(labels) == len(set(labels)), sorted(lb for lb in labels if labels.count(lb) > 1)
    names = [plan.measures[m.name].name.lower() for m in certified if m.name in plan.measures]
    assert len(names) == len(set(names))


def test_every_headline_tile_maps_to_a_certified_metric_with_an_owner(built):
    out, plan = built
    cat = Catalog.load(METRICS)
    backed = {**spec.HEADLINE_KPIS, **spec.HEADLINE_KPIS_METRIC_ONLY}
    for label, metric in backed.items():
        m = cat.metrics.get(metric)
        assert m and m.certified and m.owner, f"headline tile {label!r}: {metric!r} must be certified with an owner"
    for metric in spec.HEADLINE_KPIS_METRIC_ONLY.values():
        assert metric not in plan.measures, f"{metric} is a model measure: move it to HEADLINE_KPIS"
    # nothing on the executive overview invents its own number
    known = {_norm_tile(k) for k in [*backed, *spec.HEADLINE_KPIS_OUTSIDE]}
    tiles = overview_tile_labels()
    assert len(tiles) >= 8, tiles
    assert tiles <= known, f"overview tiles with no certified metric: {sorted(tiles - known)}"
    # the generated model carries owner and certification on every certified measure
    measures_tmdl = "\n".join(f.read_text(encoding="utf-8")
                              for f in out.glob(f"*.SemanticModel/definition/tables/{spec.MEASURES}.tmdl"))
    for metric in spec.HEADLINE_KPIS.values():
        mm = plan.measures[metric]
        assert mm.certified and mm.owner
        block = measures_tmdl.split(f"annotation dbt_metric = {metric}\n", 1)[1].split("\n\n", 1)[0]
        assert f"annotation metric_owner = {mm.owner}" in block and "annotation certified = true" in block
    manifest = __import__("json").loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["measures"]["combined_ratio"]["owner"] == "Financial Analyst II"


def test_certified_metric_without_an_owner_is_rejected(tmp_path):
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    for m in doc["metrics"]:
        if m["name"] == "combined_ratio":
            m["config"]["meta"].pop("owner")
    bad = tmp_path / "metrics.yml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TranslationError, match="certified metric combined_ratio"):
        make_plan(Catalog.load(bad), load_lock())


def test_combined_ratio_cost_buckets_match_the_pnl_seed():
    import csv

    rows = list(csv.DictReader((ROOT / "dbt" / "seeds" / "finance" / "pnl_line_map.csv").open(encoding="utf-8")))
    cat = Catalog.load(METRICS)
    for measure, group in (("earned_acquisition_cost", "ACQUISITION"), ("earned_admin_fee", "ADMIN")):
        want = {r["disbursement_type"] for r in rows if r["pnl_group"] == group}
        got = set(re.findall(r"'([A-Z_]+)'", cat.measures[measure].expr))
        assert got == want, (measure, got, want)


def test_untranslatable_expression_fails_loudly():
    with pytest.raises(TranslationError):
        measure_dax(_m("sum", "paid_amount * 1.1"), "F", None)
    with pytest.raises(TranslationError):
        measure_dax(_m("percentile", "paid_amount"), "F", None)


def test_ratio_and_derived_dax():
    names = {"a": "A", "b": "B", "c": "C"}
    ratio = Metric("r", "r", "ratio", {"numerator": "a", "denominator": {"name": "b"}})
    assert metric_dax(ratio, None, names) == "DIVIDE([A], [B])"
    derived = Metric("d", "d", "derived", {"expr": "a / b", "metrics": [{"name": "a"}, {"name": "b"}]})
    assert metric_dax(derived, None, names) == "DIVIDE([A], [B])"
    minus = Metric("d", "d", "derived", {"expr": "a - b", "metrics": [{"name": "a"}, {"name": "b"}]})
    assert metric_dax(minus, None, names) == "[A] - [B]"
    with pytest.raises(TranslationError):
        metric_dax(Metric("d", "d", "derived", {"expr": "a / b / c", "metrics": [{"name": n} for n in "abc"]}),
                   None, names)


def test_time_suffixed_metric_is_rejected(tmp_path):
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    doc["metrics"].append({"name": "claims_paid_ytd", "label": "x", "type": "simple",
                           "type_params": {"measure": "claims_paid"}})
    bad = tmp_path / "metrics.yml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TranslationError, match="time word"):
        make_plan(Catalog.load(bad), load_lock())


def test_measure_on_a_missing_column_is_rejected(tmp_path):
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    sm = next(s for s in doc["semantic_models"] if s["name"] == "cash_transaction")
    sm["measures"].append({"name": "ghost_amount", "agg": "sum", "expr": "no_such_column"})
    doc["metrics"].append({"name": "ghost_amount", "label": "x", "type": "simple",
                           "type_params": {"measure": "ghost_amount"}})
    bad = tmp_path / "metrics.yml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TranslationError, match="no_such_column"):
        make_plan(Catalog.load(bad), load_lock())


# --------------------------------------------------------------------------- grain guards
FILING = "'Funded Reserve Filing'"
LINES = "'Regulatory Line Value'"


def test_filing_measures_default_to_itd_unless_one_scope_is_sliced(built):
    """ITD and PERIOD filing rows never add up: with no single Scope in context the measure reads ITD."""
    _, plan = built
    for metric in ("calculated_reserve", "current_reserve", "reserve_shortfall", "complaints_received"):
        dax = plan.measures[metric].dax
        assert dax.startswith(f"IF(HASONEVALUE({FILING}[Scope]), "), metric
        assert f'{FILING}[Scope] = "ITD"))' in dax and f"{FILING}[Is Default Basis] = TRUE()" in dax, metric


def test_filing_line_value_is_blank_unless_one_line_and_last_lines_take_the_latest_window(built):
    _, plan = built
    dax = plan.measures["filing_line_value"].dax
    for c in ("Report ID", "Line Item", "Scope", "Basis"):
        assert f"HASONEVALUE({LINES}[{c}])" in dax, c
    assert dax.startswith("IF(HASONEVALUE(") and "SUM(" in dax
    # several windows: only a LAST line shows, at its latest window (never a sum over windows)
    assert f"IF(HASONEVALUE({LINES}[Window ID]), SUM({LINES}[Value])," in dax
    assert f'SELECTEDVALUE({LINES}[Aggregation]) = "LAST"' in dax
    assert f"VAR _end = MAX({LINES}[Window End])" in dax and f"{LINES}[Window ID] = _w" in dax


def test_grain_guard_on_a_missing_column_is_rejected(tmp_path):
    doc = yaml.safe_load(METRICS.read_text(encoding="utf-8"))
    m = next(x for x in doc["metrics"] if x["name"] == "filing_line_value")
    m["config"]["meta"]["pbi"]["single_value"] = ["no_such_column"]
    bad = tmp_path / "metrics.yml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(TranslationError, match="no_such_column"):
        make_plan(Catalog.load(bad), load_lock())


# scorecard ratio metric -> the mart's trailing-12 column the site shows (what T12M must reproduce)
SCORECARD_RATIOS = {"scorecard_loss_ratio": "t12_loss_ratio", "scorecard_cancel_rate": "t12_cancel_rate",
                    "scorecard_late_remit_rate": "t12_late_remit_rate", "scorecard_avg_days_late": "t12_avg_days_late"}


def test_dealer_scorecard_kpis_are_measures_and_rolling_columns_are_not_imported(built):
    """Every site scorecard KPI is a Sales measure over monthly additive columns; the rolling t12_* and
    precomputed ratio columns (wrong when summed over months) are left out, except the peer rank."""
    out, plan = built
    sales = {m.metric for m in plan.models["Sales"].measures}
    assert set(SCORECARD_RATIOS) | {"loss_ratio_peer_percentile"} <= sales
    for metric in SCORECARD_RATIOS:
        assert plan.measures[metric].dax.startswith("DIVIDE([Scorecard "), metric
    peer = plan.measures["loss_ratio_peer_percentile"].dax
    assert peer.startswith("IF(HASONEVALUE('Dealer Scorecard'[Dealer Key]),")
    assert "'Reporting Date'[Last Closed Period Key]" in peer
    text = (out / "Sales.SemanticModel" / "definition" / "tables" / "Dealer Scorecard.tmdl").read_text("utf-8")
    cols = set(re.findall(r"^\tcolumn (?:'([^']+)'|(\S+))$", text, re.M))
    cols = {a or b for a, b in cols}
    assert {c for c in cols if c.startswith("T12")} == {"T12 Loss Ratio Peer Pct"}, cols
    assert not cols & {"Loss Ratio", "Cancel Rate", "Late Remit Rate", "Avg Days Late", "Early Claim Rate"}
    assert {"Incurred Claims", "Earned Claims Reserve", "Contracts Written", "Days Late Total"} <= cols


# --------------------------------------------------------------------------- metrics.yml stays valid dbt
def test_metrics_yml_passes_dbt_parse(tmp_path):
    env = {**os.environ, "WAREHOUSE_PATH": str(tmp_path / "parse.duckdb"),
           "DBT_TARGET_PATH": str(tmp_path / "target"), "DBT_LOG_PATH": str(tmp_path / "logs"),
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "False"}
    r = subprocess.run([sys.executable, "-m", "dbt.cli.main", "parse", "--project-dir", str(ROOT / "dbt"),
                        "--profiles-dir", str(ROOT / "dbt")], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


def test_access_matrix_names_the_generated_roles():
    text = (ROOT / "docs" / "ACCESS_MATRIX.md").read_text(encoding="utf-8")
    for needle in ("Workspaces and apps", "Audiences", "Workspace roles", "Security groups", "Dev vs prod",
                   "Admin", "Member", "Contributor", "Viewer", "Internal", "Tenant Partner", "Dealer User"):
        assert needle in text, needle


def test_report_plugin_page(tmp_path):
    from reports import semantic as page_mod
    from reports.build import Page

    pages = page_mod.build(None, tmp_path)
    assert len(pages) == 1 and isinstance(pages[0], Page) and pages[0].path == "semantic.html"
    assert (tmp_path / "downloads" / "semantic" / page_mod.ZIP_NAME).stat().st_size > 1000


# --------------------------------------------------------------------------- against a built warehouse
def _has_core() -> bool:
    if not WAREHOUSE.exists():
        return False
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            return bool(con.execute("select count(*) from information_schema.tables "
                                    "where table_schema = 'core' and table_name = 'fct_claim_payment_line'"
                                    ).fetchone()[0])
    except duckdb.Error:
        return False


needs_warehouse = pytest.mark.skipif(not _has_core(), reason="warehouse not built (run python -m pipeline.run)")


@needs_warehouse
def test_column_lock_matches_the_warehouse():
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        live = read_warehouse_columns(con)
    lock = load_lock()
    for src, cols in lock.items():
        assert set(dict(cols)) <= set(dict(live.get(src, []))), f"{src}: run python -m semantic --refresh-columns"


@needs_warehouse
def test_every_simple_measure_runs_as_sql_and_claims_paid_reconciles():
    """Each measure expression is valid SQL on its mart; claims paid ties to the per-contract roll-up."""
    cat = Catalog.load(METRICS)
    sql_agg = {"sum": "sum({})", "count": "count({})", "count_distinct": "count(distinct {})",
               "average": "avg({})", "sum_boolean": "sum(case when {} then 1 else 0 end)",
               "max": "max({})", "min": "min({})"}
    totals = {}
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        for sm in cat.models.values():
            if sm.table is None:
                continue
            src = spec.FACTS[sm.table].source
            for m in sm.measures:
                totals[m.name] = con.execute(f"select {sql_agg[m.agg].format(m.expr)} from {src}").fetchone()[0]
        rollup = con.execute("select sum(paid_to_date) from core.dim_contract_claim_summary").fetchone()[0]
        orphan = con.execute("select coalesce(sum(paid_amount), 0) from core.fct_claim_payment_line "
                             "where contract_key = '-1'").fetchone()[0]
    # Claims with no contract sit on the '-1' member: in the measure, not in the per-contract roll-up.
    assert abs(totals["claims_paid"] - orphan - rollup) < 0.01
    assert orphan <= 0.02 * totals["claims_paid"]
    assert totals["contracts_written"] > 0 and totals["claims_opened"] >= totals["claims_denied"]


@needs_warehouse
def test_t12m_window_is_the_last_twelve_closed_months():
    """The calc group's T12M with no period filter = the site's T12 (complete months only)."""
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        last_closed, current = con.execute(
            "select last_closed_period_key, current_period_key from core.dim_reporting_date").fetchone()
        top = con.execute("select max(posting_period_key) from core.dim_posting_period").fetchone()[0]
        p = min(top, last_closed)  # VAR _p = MIN(MAX('Posting Period'[Key]), MAX('Reporting Date'[Last Closed]))
        window = [r[0] for r in con.execute(
            "select posting_period_key from core.dim_posting_period where posting_period_key <= ? "
            "and posting_period_key > ? order by 1", [p, p - 100]).fetchall()]
        as_of = con.execute("select as_of_date from core.dim_reporting_date").fetchone()[0]
    assert current not in window and len(window) == 12
    if str(as_of) == "2026-10-21":  # the committed synthetic as-of date
        assert (window[0], window[-1]) == (202510, 202609)


def _yyyymm(col: str) -> str:
    return f"(year({col}) * 100 + month({col}))"


@needs_warehouse
def test_claim_measures_cover_both_admin_systems_and_match_the_site():
    """Claims Opened per year = dim_claim reported counts (legacy included); denial rate and paid severity
    over the T12 window equal the claims KPI page source (mart_claims_kpi)."""
    cat = Catalog.load(METRICS)
    src = spec.FACTS["Claim Summary"].source
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        # every loss / reported month joins to exactly one Posting Period row (the relationships hold)
        assert con.execute(f"select count(*) from {src} c left join core.dim_posting_period p "
                           "on p.period_start = c.loss_month where p.period_start is null").fetchone()[0] == 0
        assert con.execute(f"select count(*) from {src} c left join core.dim_posting_period p "
                           "on p.period_start = c.reported_month where p.period_start is null").fetchone()[0] == 0
        opened = dict(con.execute(f"select year(reported_month), count({cat.measures['claims_opened'].expr}) "
                                  f"from {src} group by 1").fetchall())
        reported = dict(con.execute("select year(reported_date), count(*) from core.dim_claim group by 1").fetchall())
        assert opened == reported
        assert all(opened.get(y, 0) > 0 for y in (2019, 2020, 2021, 2022)), opened
        lines = con.execute("select count(distinct claim_key) from core.fct_claim_payment_line").fetchone()[0]
        assert con.execute(f"select count(*) from {src}").fetchone()[0] == lines

        p = con.execute("select last_closed_period_key from core.dim_reporting_date").fetchone()[0]
        lo = p - 100
        m = cat.measures
        den, rep, paid, npaid = con.execute(
            f"select sum({m['claims_denied'].expr}), count(*), sum({m['paid_on_paid_claims'].expr}), "
            f"sum({m['claims_closed_paid'].expr}) from {src} "
            f"where {_yyyymm('loss_month')} <= ? and {_yyyymm('loss_month')} > ?", [p, lo]).fetchone()
        kpi = con.execute(
            "select sum(claims_denied), sum(claims_reported), sum(paid_amount_on_paid_claims), sum(paid_claim_count) "
            "from claims.mart_claims_kpi where loss_month_key <= ? and loss_month_key > ?", [p, lo]).fetchone()
    assert (den, rep, npaid) == (kpi[0], kpi[1], kpi[3])
    assert den / rep == pytest.approx(kpi[0] / kpi[1], rel=1e-9)
    assert paid / npaid == pytest.approx(kpi[2] / kpi[3], rel=1e-6)


# --------------------------------------------------------------------------- RLS evaluated as SQL
_REF = r"('(?:[^']|'')+'|\w+)\[([^\]]+)\]"


def _tname(raw: str) -> str:
    return raw[1:-1].replace("''", "'") if raw.startswith("'") else raw


def _snake(column: str) -> str:
    return column.lower().replace(" ", "_")


def _set_sql(expr: str, user: str) -> str:
    """CALCULATETABLE(VALUES('T'[C]), <predicate>) -> (select c from T where <predicate>)."""
    m = re.fullmatch(rf"CALCULATETABLE\(VALUES\({_REF}\), (.*)\)", expr.strip(), re.S)
    assert m, expr
    return f"(select {_snake(m.group(2))} from {_rel(_tname(m.group(1)))} where {_pred_sql(m.group(3), user)})"


def _pred_sql(expr: str, user: str) -> str:
    expr = expr.strip()
    m = re.fullmatch(rf"(.*) \|\| {_REF} = \"([^\"]*)\"", expr, re.S)
    if m:  # "<rule> || 'T'[Key] = "-1"": the unknown member stays visible
        return f"({_pred_sql(m.group(1), user)} or {_snake(m.group(3))} = '{m.group(4)}')"
    m = re.fullmatch(rf"{_REF} = USERPRINCIPALNAME\(\)", expr)
    if m:
        return f"{_snake(m.group(2))} = '{user}'"
    m = re.fullmatch(rf"{_REF} IN (.*)", expr, re.S)
    assert m, f"untranslated RLS filter: {expr}"
    return f"{_snake(m.group(2))} in {_set_sql(m.group(3), user)}"


def _rel(table: str) -> str:
    if table == spec.PAYEE_ACCESS:  # the calculated bridge, as its DAX source builds it
        return ("(select distinct payee_account_holder_key as account_holder_key, dealer_key, tenant_key "
                "from core.fct_claim_payment_line)")
    return spec.ALL_TABLES[table].source


def _rows_sql(table: str, perms: dict[str, str], rels: list, user: str, seen=()) -> str:
    """Rows of ``table`` visible under the role: its own filter AND every filtered table it relates to."""
    where = [_pred_sql(perms[table], user)] if table in perms else []
    for f, fc, t, tc in rels:
        if f == table and t not in seen:
            inner = _rows_sql(t, perms, rels, user, seen + (table,))
            if inner != _rel(t):
                where.append(f"{fc} in (select {tc} from {inner} as x)")
    return f"(select * from {_rel(table)} where {' and '.join(where)})" if where else _rel(table)


def _visible_total(model: str, role: str, fact: str, expr: str, user: str, built) -> float:
    out, plan = built
    dm = plan.models[model]
    perms = _permissions(out / f"{model}.SemanticModel" / "definition" / "roles" / f"{role}.tmdl")
    sql = f"select coalesce(sum({expr}), 0) from {_rows_sql(fact, perms, dm.relationships, user)} as v"
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        return con.execute(sql).fetchone()[0]


@needs_warehouse
def test_rls_roles_evaluated_for_a_sample_user_match_the_dealer_packet(built):
    """Craft rule: test RLS with a user from each role. The generated role filters, translated to SQL,
    give a dealer user exactly what the publish packet rule gives (rows with BOTH the dealer's
    dealer_key and tenant_key, the T12 claims KPIs from the dealer scorecard), and a partner user
    exactly their tenant. A user with no mapping sees nothing."""
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        login, dealer_key, tenant_key = con.execute(
            "select user_login, dealer_key, tenant_key from core.dim_user_dealer_access a "
            "where exists (select 1 from core.fct_claim_payment_line f where f.dealer_key = a.dealer_key) "
            "order by user_login limit 1").fetchone()

        def direct(sql: str, *params) -> float:
            return con.execute(sql, list(params)).fetchone()[0] or 0

        pkt = "dealer_key = ? and tenant_key = ?"
        dealer_paid = direct(f"select sum(paid_amount) from core.fct_claim_payment_line where {pkt}",
                             dealer_key, tenant_key)
        dealer_written = direct(f"select sum(contracts_written) from core.fct_gross_premium where {pkt}",
                                dealer_key, tenant_key)
        p = direct("select last_closed_period_key from core.dim_reporting_date")
        t12 = f"month_key <= {p} and month_key > {p - 100}"
        dealer_claims_t12 = direct("select sum(claims_reported) from dealer.mart_dealer_scorecard "
                                   f"where {pkt} and {t12}",
                                   dealer_key, tenant_key)
        # The split contract dims (Contract Vehicle, ...) have no '-1' unknown member, so a partner cannot see
        # claim lines with no contract (the blank member of an RLS-filtered table is hidden). Expected
        # total = the tenant's lines on a known contract.
        tenant_paid = direct("select sum(paid_amount) from core.fct_claim_payment_line where tenant_key = ? "
                             "and contract_key in (select contract_key from core.dim_contract_vehicle)", tenant_key)
        tenant_written = direct("select sum(contracts_written) from core.fct_gross_premium where tenant_key = ?",
                                tenant_key)
        all_paid = direct("select sum(paid_amount) from core.fct_claim_payment_line")

    paid = _visible_total("Claims", "Dealer User", "Claim Payment Line", "paid_amount", login, built)
    written = _visible_total("Sales", "Dealer User", "Gross Premium", "contracts_written", login, built)
    claims_t12 = _visible_total("Sales", "Dealer User", "Dealer Scorecard",
                                f"case when {t12} then claims_reported else 0 end", login, built)
    assert 0 < paid < all_paid
    assert paid == pytest.approx(dealer_paid, abs=0.01)
    assert written == dealer_written and claims_t12 == dealer_claims_t12

    t_paid = _visible_total("Claims", "Tenant Partner", "Claim Payment Line", "paid_amount", login, built)
    t_written = _visible_total("Sales", "Tenant Partner", "Gross Premium", "contracts_written", login, built)
    assert t_paid == pytest.approx(tenant_paid, abs=0.01) and t_written == tenant_written
    assert _visible_total("Claims", "Dealer User", "Claim Payment Line", "paid_amount", "nobody@example.com",
                          built) == 0


# --------------------------------------------------------------------------- grain guards against the warehouse
@needs_warehouse
def test_unsliced_filing_measures_match_the_regulatory_headline():
    """With no Scope slicer the filing measures equal the regulatory index (default basis, ITD), and
    the guard matters: adding the PERIOD rows would overstate the reserve and double-count complaints."""
    from reports.regulatory.pages import _default_itd

    cat = Catalog.load(METRICS)
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        summary = con.execute("select * from regulatory.mart_funded_reserve_summary").df()

        def measure_sql(metric: str, where: str) -> float:
            expr = cat.measures[cat.metrics[metric].type_params["measure"]].expr
            return con.execute(f"select coalesce(sum({expr}), 0) from regulatory.mart_funded_reserve_summary "
                               f"where {where}").fetchone()[0]

        itd = _default_itd(summary)
        site = {"calculated_reserve": "calculated_reserve", "current_reserve": "current_reserve",
                "reserve_shortfall": "reserve_shortfall", "complaints_received": "complaints_received"}
        for metric, column in site.items():
            guarded = measure_sql(metric, "scope = 'ITD'")  # default_filter {scope: ITD}
            assert guarded == pytest.approx(float(itd[column].sum()), abs=0.01), metric
        unguarded = measure_sql("calculated_reserve", "true")
    assert unguarded > float(itd.calculated_reserve.sum()) + 1, "PERIOD rows exist, so the guard is needed"
    assert cat.metrics["calculated_reserve"].pbi["default_filter"] == {"scope": "ITD"}


@needs_warehouse
def test_filing_line_value_last_lines_read_one_window():
    """The in-force line (seed aggregation LAST) at the latest market-survey window, never summed over years."""
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        rows = con.execute(
            "select window_id, window_end, sum(value) from regulatory.mart_regulatory_line_values "
            "where report_id = 'MARKET_SURVEY' and line_item = 'IN_FORCE' and aggregation = 'LAST' "
            "group by 1, 2 order by 2").fetchall()
    assert len(rows) > 1, "several survey windows: a plain SUM would add each year's in-force count"
    latest = rows[-1][2]
    # what the generated DAX returns with no window slicer: _end = MAX(Window End), _w = that window
    assert latest < sum(r[2] for r in rows)


@needs_warehouse
def test_t12m_scorecard_ratios_equal_the_site_per_dealer_and_portfolio():
    """Scorecard measures under T12M (the 12 closed months up to the last closed one), evaluated as SQL
    from their metrics.yml definitions, equal the mart's t12 columns the site shows for every dealer, and
    the portfolio loss ratio on the Dealers page (sum of T12 incurred / sum of T12 earned claims reserve)."""
    cat = Catalog.load(METRICS)

    def expr(metric: str) -> str:
        return cat.measures[cat.metrics[metric].type_params["measure"]].expr

    parts = {r: (cat.metrics[r].inputs[0], cat.metrics[r].inputs[1]) for r in SCORECARD_RATIOS}
    sums = ", ".join(f"sum({expr(n)}) as {r}_n, sum({expr(d)}) as {r}_d" for r, (n, d) in parts.items())
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        p = con.execute("select last_closed_period_key from core.dim_reporting_date").fetchone()[0]
        t12 = con.execute(f"select tenant_key, dealer_key, {sums} from dealer.mart_dealer_scorecard "
                          f"where month_key <= {p} and month_key > {p - 100} group by 1, 2").df()
        site = con.execute(f"select tenant_key, dealer_key, {', '.join(SCORECARD_RATIOS.values())} "
                           f"from dealer.mart_dealer_scorecard where month_key = {p}").df()
        portfolio = con.execute("select sum(t12_incurred_claims) / sum(t12_earned_claims_reserve) "
                                f"from dealer.mart_dealer_scorecard where month_key = {p}").fetchone()[0]
    joined = site.merge(t12, on=["tenant_key", "dealer_key"], how="left")
    assert len(joined) == len(site) > 0
    for ratio, column in SCORECARD_RATIOS.items():
        den = joined[f"{ratio}_d"]
        got = (joined[f"{ratio}_n"] / den.where(den != 0)).astype(float)
        want = joined[column].astype(float)
        both = want.notna() & got.notna()
        assert (want.isna() == got.isna()).all(), ratio
        assert (got[both] - want[both]).abs().max() < 1e-9, ratio
    assert t12["scorecard_loss_ratio_n"].sum() / t12["scorecard_loss_ratio_d"].sum() == pytest.approx(portfolio)
