"""Generate docs/operating-model/DATA_DICTIONARY.md from the dbt YAML (and the warehouse, if built).

    python -m reports.dictionary [--warehouse PATH] [--out PATH]

Descriptions, tests, grain and PII flags come from every ``dbt/models/**/*.yml`` and
``dbt/seeds/**/*.yml``. When the warehouse exists, every column of every mart, seed and snapshot is
listed with its type, so the dictionary covers columns nobody has documented yet (shown as such).
Underscore-prefixed pipeline-metadata columns are left out (docs/V3_DESIGN.md 2.6). Staging and
intermediate models are internal; they are listed by name and description only.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = ROOT / "dbt"
OUT = ROOT / "docs" / "operating-model" / "DATA_DICTIONARY.md"
METRICS_YML = DBT_DIR / "models" / "metrics.yml"
METRICS_MD = ROOT / "docs" / "METRICS.md"
CATALOG_START, CATALOG_END = "<!-- metric-catalog:start -->", "<!-- metric-catalog:end -->"
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))

# Consumer-facing layers, in document order: (warehouse schema, model folder under dbt/, heading).
PUBLIC_LAYERS: list[tuple[str, str, str]] = [
    ("core", "models/marts/core", "Core star schema (`core`)"),
    ("finance", "models/marts/finance", "Finance marts (`finance`)"),
    ("claims", "models/marts/claims", "Claims marts (`claims`)"),
    ("dealer", "models/marts/dealer", "Dealer marts (`dealer`)"),
    ("regulatory", "models/marts/regulatory", "Regulatory marts (`regulatory`)"),
    ("seeds", "seeds", "Seeds: counting rules and reference tables (`seeds`)"),
    ("snapshots", "snapshots", "Snapshots: daily SCD2 history (`snapshots`)"),
]
INTERNAL_LAYERS: list[tuple[str, str]] = [
    ("models/staging", "Staging (`staging`, internal)"),
    ("models/intermediate", "Intermediate (`intermediate`, internal)"),
]


@dataclass
class Column:
    name: str
    type: str = ""
    description: str = ""
    tests: list[str] = field(default_factory=list)


@dataclass
class Model:
    name: str
    folder: str  # path relative to dbt/
    description: str = ""
    meta: dict = field(default_factory=dict)
    columns: dict[str, Column] = field(default_factory=dict)


def _test_name(t) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict) and t:
        return next(iter(t))
    return str(t)


def load_yaml_models(dbt_dir: Path = DBT_DIR) -> dict[str, Model]:
    """Every model, seed and snapshot documented in dbt YAML, keyed by name."""
    models: dict[str, Model] = {}
    for path in sorted(dbt_dir.rglob("*.yml")):
        rel = path.relative_to(dbt_dir).as_posix()
        if not rel.startswith(("models/", "seeds/", "snapshots/")):
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for kind in ("models", "seeds", "snapshots"):
            for m in doc.get(kind) or []:
                if not isinstance(m, dict) or "name" not in m:
                    continue
                meta = dict(m.get("meta") or {}) | dict((m.get("config") or {}).get("meta") or {})
                model = Model(m["name"], str(Path(rel).parent.as_posix()), (m.get("description") or "").strip(), meta)
                for c in m.get("columns") or []:
                    tests = [_test_name(t) for t in (c.get("data_tests") or c.get("tests") or [])]
                    model.columns[c["name"]] = Column(c["name"], c.get("data_type", ""),
                                                      (c.get("description") or "").strip(), tests)
                models[m["name"]] = model
    return models


def warehouse_columns(warehouse: Path, schemas: list[str]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(schema, table) -> [(column, type)] in ordinal order, for the given schemas. Empty if no warehouse."""
    if not warehouse.exists():
        return {}
    import duckdb

    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    with duckdb.connect(str(warehouse), read_only=True) as con:
        rows = con.execute(
            f"""select table_schema, table_name, column_name, data_type
            from information_schema.columns where table_schema in ({", ".join("?" for _ in schemas)})
            order by table_schema, table_name, ordinal_position""",
            schemas,
        ).fetchall()
    for schema, table, col, typ in rows:
        out.setdefault((schema, table), []).append((col, typ))
    return out


# Naming conventions (docs/V3_DESIGN.md 2.4-2.6): a description for columns the YAML leaves blank.
CONVENTIONS: list[tuple[str, str]] = [
    ("posting_period_key", "Accounting month of the post date, INTEGER yyyymm (FK `dim_posting_period`)"),
    ("tenant_key", "Administrator (white-label tenant) id (FK `dim_tenant`)"),
    ("_date_key", "Date as INTEGER yyyymmdd (FK `dim_date`)"),
    ("_key", "Surrogate key, md5 string; `'-1'` is the unknown member"),
    ("_id", "Natural id in the source system"),
    ("is_", "Yes/no flag"),
    ("has_", "Yes/no flag"),
]


def convention(col: str) -> str:
    for pattern, text in CONVENTIONS:
        if (pattern.endswith("_") and col.startswith(pattern)) or col == pattern or (
                pattern.startswith("_") and col.endswith(pattern)):
            return text
    return ""


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _folder_matches(model: Model, folder: str) -> bool:
    return model.folder == folder or model.folder.startswith(folder + "/")


def render(models: dict[str, Model], wh: dict[tuple[str, str], list[tuple[str, str]]]) -> str:
    lines = [
        "# Data dictionary",
        "",
        "Generated by `python -m reports.dictionary` from the dbt YAML"
        + (" and the built warehouse" if wh else "") + ". **Do not edit by hand**: change the model's YAML",
        "description and regenerate. Columns marked *undocumented* exist in the warehouse but have no",
        "YAML description yet. Pipeline-metadata columns (`_loaded_at`, `_built_at`, ...) are omitted.",
        "Keys are md5 strings and `'-1'` is the unknown member; `posting_period_key` is `yyyymm`.",
        "",
        "Metric formulas are not repeated here; they are in [docs/METRICS.md](../METRICS.md).",
        "",
    ]
    total = documented = 0
    for schema, folder, heading in PUBLIC_LAYERS:
        names = {m.name for m in models.values() if _folder_matches(m, folder)}
        names |= {t for (s, t) in wh if s == schema}
        if not names:
            continue
        lines += [f"## {heading}", ""]
        for name in sorted(names):
            m = models.get(name, Model(name, folder))
            cols = wh.get((schema, name)) or [(c.name, c.type) for c in m.columns.values()]
            cols = [(c, t) for c, t in cols if not c.startswith("_") and not c.startswith("dbt_")]
            flags = []
            if m.meta.get("grain"):
                flags.append(f"grain: {m.meta['grain']}")
            if m.meta.get("contains_pii"):
                flags.append("**contains PII (fake)**")
            lines += [f"### `{schema}.{name}`", ""]
            if m.description or flags:
                lines += [_cell(" · ".join([x for x in [m.description, *flags] if x])), ""]
            if cols:
                lines += ["| Column | Type | Description | Tests |", "|---|---|---|---|"]
                for col, typ in cols:
                    c = m.columns.get(col, Column(col))
                    total += 1
                    if c.description:
                        desc, documented = _cell(c.description), documented + 1
                    elif conv := convention(col):
                        desc, documented = f"{conv} *(naming convention)*", documented + 1
                    else:
                        desc = "*undocumented*"
                    lines.append(f"| `{col}` | {typ or c.type} | {desc} | {', '.join(c.tests)} |")
                lines.append("")
    lines.insert(9, f"Column coverage: {documented:,} of {total:,} consumer-facing columns described "
                    f"({documented / total:.0%}).\n" if total else "")
    for folder, heading in INTERNAL_LAYERS:
        internal = sorted((m for m in models.values() if _folder_matches(m, folder)), key=lambda m: m.name)
        if not internal:
            continue
        lines += [f"## {heading}", "", "| Model | Description |", "|---|---|"]
        for m in internal:
            pii = " **contains PII (fake)**" if m.meta.get("contains_pii") else ""
            lines.append(f"| `{m.name}` | {_cell(m.description) or '*undocumented*'}{pii} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- metric catalog
def _ref_name(model: str) -> str:
    return model.split("'")[1] if "'" in model else model


def _ref(x) -> str:
    return x.get("name", "") if isinstance(x, dict) else str(x)


def metric_catalog(metrics_yml: Path = METRICS_YML) -> list[dict[str, str]]:
    """One row per metric in metrics.yml: name, label, definition, source fact, display folder."""
    doc = yaml.safe_load(metrics_yml.read_text(encoding="utf-8")) or {}
    measures: dict[str, tuple[str, str, str]] = {}
    for sm in doc.get("semantic_models") or []:
        for m in sm.get("measures") or []:
            measures[m["name"]] = (m.get("agg", ""), str(m.get("expr", m["name"])), _ref_name(sm.get("model", "")))
    rows = []
    for m in doc.get("metrics") or []:
        tp, kind = m.get("type_params") or {}, m.get("type", "")
        source = ""
        if kind in ("simple", "cumulative"):
            agg, expr, source = measures.get(tp.get("measure", ""), ("", tp.get("measure", ""), ""))
            definition = f"{agg}({expr})" if agg else expr
            if kind == "cumulative":
                definition += f" over {(tp.get('cumulative_type_params') or {}).get('window', 'all time')}"
        elif kind == "ratio":
            definition = f"{_ref(tp.get('numerator'))} / {_ref(tp.get('denominator'))}"
        elif kind == "derived":
            definition = str(tp.get("expr", ""))
        else:
            definition = kind
        meta = (m.get("config") or {}).get("meta") or {}
        pbi = meta.get("pbi") or {}
        rows.append({"name": m["name"], "label": m.get("label", ""), "type": kind, "definition": definition,
                     "source": source, "folder": pbi.get("folder", ""), "owner": str(meta.get("owner", "")),
                     "certified": "yes" if meta.get("certified") is True else ""})
    return rows


def render_metric_catalog(rows: list[dict[str, str]]) -> str:
    lines = [CATALOG_START,
             "<!-- Generated by `python -m reports.dictionary` from dbt/models/metrics.yml. Do not edit. -->",
             "", "| Metric | Label | Type | Definition | Source fact | Folder | Owner | Certified |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        source = f"`{r['source']}`" if r["source"] else ""
        lines.append(f"| `{r['name']}` | {_cell(r['label'])} | {r['type']} | `{_cell(r['definition'])}` | "
                     f"{source} | {r['folder']} | {_cell(r.get('owner', ''))} | {r.get('certified', '')} |")
    return "\n".join([*lines, "", CATALOG_END])


def update_metrics_md(path: Path = METRICS_MD, metrics_yml: Path = METRICS_YML) -> bool:
    """Replace the catalog block in docs/METRICS.md. Returns True when the file changed."""
    text = path.read_text(encoding="utf-8")
    if CATALOG_START not in text or CATALOG_END not in text:
        return False
    head, rest = text.split(CATALOG_START, 1)
    tail = rest.split(CATALOG_END, 1)[1]
    new = head + render_metric_catalog(metric_catalog(metrics_yml)) + tail
    if new != text:
        path.write_text(new, encoding="utf-8")
    return new != text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reports.dictionary", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    models = load_yaml_models()
    wh = warehouse_columns(args.warehouse, [s for s, _, _ in PUBLIC_LAYERS])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(models, wh), encoding="utf-8")
    print(f"wrote {args.out} ({len(models)} documented models, {len(wh)} warehouse tables)")
    if update_metrics_md():
        print(f"refreshed the metric catalog in {METRICS_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
