"""Core warehouse layer, V7 round (docs/V3_DESIGN.md 10.4): Data Engineering Manager edits.

Fast checks run on the repo alone (seeds, model SQL, the generator's vocabularies). The
``needs_v7_warehouse`` checks run against a warehouse built with the V7 core models
(``WAREHOUSE_PATH``, e.g. a dev copy) and skip on one built before them.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

import duckdb
import pytest

from generator import catalog

ROOT = Path(__file__).resolve().parents[1]
DBT = ROOT / "dbt"
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


def _seed(path: str) -> list[dict[str, str]]:
    with (DBT / "seeds" / path).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _sql(name: str) -> str:
    return next(DBT.glob(f"models/**/{name}.sql")).read_text(encoding="utf-8")


def _norm(reason: str) -> str:
    """Python twin of the cancel_reason_norm macro (dbt/macros/text.sql)."""
    s = re.sub(r"\s*-\s*", " - ", reason.strip().upper())
    return re.sub(r"\s+", " ", s)


def _map(source_system: str, reason: str) -> dict[str, str] | None:
    """The crosswalk row int_cancellations_conformed picks: exact first, else the longest prefix."""
    rows = [r for r in _seed("core/cancel_reason_crosswalk.csv") if r["source_system"] == source_system]
    n = _norm(reason)
    exact = [r for r in rows if r["match_type"] == "exact" and _norm(r["source_reason"]) == n]
    if exact:
        return exact[0]
    pref = [r for r in rows if r["match_type"] == "prefix" and n.startswith(_norm(r["source_reason"]))]
    return max(pref, key=lambda r: len(_norm(r["source_reason"]))) if pref else None


# --------------------------------------------------------------------------- edit 41: cancel reasons
VOCAB_SYSTEM = {"core_default": "admin_core", "core_alt": "admin_core", "legacy": "legacy_admin"}


def test_every_generated_cancel_reason_maps_to_the_same_group_in_every_system():
    vocab = getattr(catalog, "CANCEL_REASON_VOCAB", {"core_default": {r: r for r in catalog.CANCEL_REASONS}})
    canonical = {r: _map("admin_core", r)["reason_group"] for r in catalog.CANCEL_REASONS}
    for name, labels in vocab.items():
        for reason, label in labels.items():
            row = _map(VOCAB_SYSTEM[name], label)
            assert row, f"{name}: {label!r} has no row in cancel_reason_crosswalk.csv"
            assert row["reason_group"] == canonical[reason], (name, label)
            assert row["is_admin_correction"] == "false", (name, label)


def test_override_coded_reasons_are_admin_corrections():
    corrections = getattr(catalog, "ADMIN_CORRECTION_REASONS", {})
    for name, labels in corrections.items():
        for label in labels:
            row = _map(VOCAB_SYSTEM[name], label)
            assert row and row["is_admin_correction"] == "true", (name, label)
    assert _map("admin_core", "x-  dup entry")["reason_group"] == "ADMIN_CORRECTION"
    assert _map("admin_core", "Something new") is None  # fails core_cancel_reason_mapped in dbt


def test_cancel_reasons_are_mapped_in_one_place():
    sql = _sql("int_cancellations_conformed")
    assert "ref('cancel_reason_crosswalk')" in sql and "is_reason_mapped" in sql
    assert (DBT / "tests" / "core" / "core_cancel_reason_mapped.sql").exists()
    metrics = (DBT / "models" / "metrics.yml").read_text(encoding="utf-8")
    assert "name: cancel_rate_excl_admin" in metrics


# --------------------------------------------------------------------------- edit 21: disbursement group
def test_disbursement_group_fallback_matches_the_generator_default():
    sql = _sql("fct_disbursement")
    block = sql.split("upper(coalesce(d.source_disbursement_group,", 1)[1].split("as disbursement_group", 1)[0]
    found = dict(re.findall(r"when '([A-Z_]+)' then '([A-Z_]+)'", block))
    default = getattr(catalog, "DISBURSEMENT_GROUP_DEFAULT", None)
    if default is None:
        pytest.skip("generator has no disbursement groups yet")
    assert found == default
    accepted = set(re.search(r"values: \[(PREMIUM[^\]]*)\]", (DBT / "models" / "marts" / "core" / "_core_keys.yml")
                             .read_text(encoding="utf-8")).group(1).replace(" ", "").split(","))
    assert set(catalog.DISBURSEMENT_GROUPS) <= accepted


# --------------------------------------------------------------------------- edit 50: product lines
def test_external_product_line_and_bundles():
    rows = {r["product_code"]: r for r in _seed("core/product_taxonomy_crosswalk.csv")}
    assert rows["LTW"]["external_product_line"] == "ANCILLARY_OTHER"  # a limited warranty is not a VSC
    assert rows["TW"]["external_product_line"] == "TIRE_WHEEL" and rows["BNDL"]["is_bundle"] == "true"
    assert {r["external_product_line"] for r in rows.values()} <= {
        "VSC", "GAP", "TIRE_WHEEL", "PREPAID_MAINTENANCE", "ANCILLARY_OTHER"}
    bundle = re.compile(r"\b(bundle|pkg|package|combo)\b")
    for name in getattr(catalog, "BUNDLE_PRODUCT_NAMES", {}).values():
        assert bundle.search(name.lower()), name


# --------------------------------------------------------------------------- edit 45: tenant-safe ids
def test_no_model_joins_on_admin_company_id_alone():
    for p in (DBT / "models").rglob("*.sql"):
        text = re.sub(r"--[^\n]*|\{#.*?#\}", "", p.read_text(encoding="utf-8"), flags=re.S)
        for m in re.finditer(r"[\w.]*admin_company_id\s*=\s*[\w.]*admin_company_id", text, re.I):
            around = text[max(0, m.start() - 300): m.end() + 300].lower()
            assert "tenant" in around, f"{p.name}: join on admin_company_id without the tenant"


# --------------------------------------------------------------------------- warehouse checks
def _cols(con, table: str) -> set[str]:
    schema, name = table.split(".")
    return {r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_schema = ? and table_name = ?",
        [schema, name]).fetchall()}


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip("no warehouse")
    c = duckdb.connect(str(WAREHOUSE), read_only=True)
    if "reason_group" not in _cols(c, "core.fct_cancellation"):
        c.close()
        pytest.skip("warehouse built before the V7 core models")
    yield c
    c.close()


def test_admin_corrections_and_reversals_never_count_as_effective_cancels(con):
    bad = con.execute(
        "select count(*) from core.fct_cancellation where is_effective_cancel_excl_admin "
        "and (is_admin_correction or is_reversed or is_reversal_row)").fetchone()[0]
    assert bad == 0
    unmapped = con.execute("select count(*) from core.fct_cancellation where reason_group = 'UNMAPPED'").fetchone()[0]
    assert unmapped == 0


def test_every_disbursement_line_has_a_group(con):
    n = con.execute("select count(*) from core.fct_disbursement where disbursement_group is null").fetchone()[0]
    assert n == 0
