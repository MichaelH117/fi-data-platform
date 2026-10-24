"""Tabular-Editor-style documentation export: ``python -m semantic export-docs [--out DIR]``.

Reads the TMDL folders on disk (no Power BI Desktop, no Tabular Editor, no warehouse) and the PBIR
report, and writes four CSVs that an analyst can open in Excel:

``Model_Documentation.csv``
    One row per measure, calculated column / table, calculation item, RLS filter and partition source,
    with its DAX or M, display folder, format string, description and the dbt metric it comes from.
``Model_Relationships.csv``
    Every relationship as a SQL join map: ``from <fact> left join <dim> on <fact>.<col> = <dim>.<col>``,
    using the physical table and column each side is loaded from.
``Report_Field_Usage.csv``
    Report -> page -> visual -> role -> field, read from the PBIR ``definition/`` folder, joined to the
    measure DAX or the physical source column.
``Measure_Report_Map.csv``
    Each measure with the pages and visuals that use it, directly or only through another measure,
    and a dependency status (``used``, ``used_via_measure``, ``unused``, ``no_report``).

Model sets: ``modern_domain`` (``semantic/model/``: the four domain models), ``modern_report`` (the
combined report model in ``powerbi/``, the one the PBIR report binds to) and ``legacy``
(``legacy/semantic_models/``: the one-model-per-schema anti-pattern, kept for comparison). The legacy
reports never existed as PBIR files, so legacy measures show ``no_report``.

Why this exists: Tabular Editor sees only the model, not the report, so it cannot say which visual uses
which measure. Reading the PBIR report-definition layer closes that gap and makes the DAX -> SQL work
(``docs/DAX_TO_SQL.md``) start from the fields a visual really uses.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "site" / "downloads" / "semantic"
MODEL_SETS: tuple[tuple[str, Path], ...] = (
    ("modern_domain", ROOT / "semantic" / "model"),
    ("modern_report", ROOT / "powerbi"),
    ("legacy", ROOT / "legacy" / "semantic_models"),
)
REPORT_ROOTS: tuple[Path, ...] = (ROOT / "powerbi",)
FILES = ("Model_Documentation.csv", "Model_Relationships.csv", "Report_Field_Usage.csv", "Measure_Report_Map.csv")

log = logging.getLogger("semantic.export")


# --------------------------------------------------------------------------- TMDL parsing
@dataclass
class Node:
    """One TMDL object or property line, with its nested block."""

    keyword: str
    name: str = ""
    value: str = ""              # text after "=" (inline or the multi-line expression) or after ":"
    description: str = ""        # the "///" lines above it
    children: list[Node] = field(default_factory=list)

    def props(self) -> dict[str, str]:
        return {c.keyword.rstrip(":"): c.value for c in self.children if c.keyword.endswith(":")}

    def flags(self) -> set[str]:
        return {c.keyword for c in self.children if not c.keyword.endswith(":") and not c.name and not c.value}

    def annotations(self) -> dict[str, str]:
        return {c.name: c.value for c in self.children if c.keyword == "annotation"}

    def kids(self, keyword: str) -> list[Node]:
        return [c for c in self.children if c.keyword == keyword]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def read_name(text: str) -> tuple[str, str]:
    """Split a TMDL object name off the front of ``text``: ``'A ''b'' c' = x`` -> ("A 'b' c", "= x")."""
    text = text.lstrip()
    if text.startswith("'"):
        i, out = 1, []
        while i < len(text):
            if text[i] == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1:]
            out.append(text[i])
            i += 1
        return "".join(out), ""
    m = re.match(r"([^\s=:.]+(?:\.[^\s=:.']+)*)(.*)$", text, re.S)
    return (m.group(1), m.group(2)) if m else ("", text)


def split_ref(ref: str) -> tuple[str, str]:
    """``'Claim Payment Line'.'Claim Key'`` or ``Contracts.fxDealerId`` -> (table, column)."""
    table, rest = read_name(ref) if ref.lstrip().startswith("'") else (ref.split(".", 1)[0], "." + ref.split(".", 1)[1])
    column, _ = read_name(rest.lstrip()[1:])
    return table.strip(), column.strip()


def _dedent(lines: list[str]) -> str:
    rows = [ln.rstrip() for ln in lines]
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        return ""
    cut = min(_indent(r) for r in rows if r.strip())
    return "\n".join(r[cut:] for r in rows)


def _header(text: str) -> Node:
    keyword, _, rest = text.partition(" ")
    if keyword.endswith(":"):
        return Node(keyword, value=rest.strip())
    if keyword == "ref":
        kind, _, rest = rest.partition(" ")
        keyword = f"ref {kind}"
    if keyword == "annotation" or keyword == "extendedProperty":
        name, _, value = rest.partition("=")
        return Node(keyword, name.strip(), value.strip())
    name, rest = read_name(rest) if rest else ("", "")
    rest = rest.strip()
    value = rest[1:].strip() if rest.startswith("=") else rest
    return Node(keyword, name, value)


def parse_block(lines: list[str], level: int) -> list[Node]:
    nodes: list[Node] = []
    desc: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind, text = _indent(line), line.strip()
        if ind != level:  # stray deeper line; parse_block is only called on well-formed blocks
            i += 1
            continue
        if text.startswith("///"):
            desc.append(text[3:].strip())
            i += 1
            continue
        node = _header(text)
        node.description = " ".join(desc)
        desc = []
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > level):
            j += 1
        block = lines[i + 1:j]
        if text.endswith("=") and not node.keyword.endswith(":"):
            k = 0
            while k < len(block) and (not block[k].strip() or _indent(block[k]) >= level + 2):
                k += 1
            # trailing blank lines belong to the next sibling, not to the expression
            while k > 0 and not block[k - 1].strip():
                k -= 1
            node.value = _dedent(block[:k])
            block = block[k:]
        node.children = parse_block(block, level + 1)
        nodes.append(node)
        i = j
    return nodes


def parse_tmdl(text: str) -> list[Node]:
    return parse_block(text.replace("\r\n", "\n").split("\n"), 0)


# --------------------------------------------------------------------------- model loading
@dataclass
class Table:
    name: str
    node: Node
    source_table: str = ""
    columns: dict[str, str] = field(default_factory=dict)  # model column -> physical source column


@dataclass
class Model:
    model_set: str
    name: str
    folder: Path
    tables: dict[str, Table] = field(default_factory=dict)
    relationships: list[Node] = field(default_factory=list)
    roles: list[Node] = field(default_factory=list)

    def measures(self) -> list[tuple[Table, Node]]:
        return [(t, m) for t in self.tables.values() for m in t.node.kids("measure")]


_M_SCHEMA_ITEM = re.compile(r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"')
_M_SNOWFLAKE = re.compile(r'Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"Schema".*?'
                          r'Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"(?:Table|View)"', re.S)


def source_table_of(node: Node) -> str:
    """``schema.table`` a table loads from: the ``source_table`` annotation, else parsed from the M partition."""
    ann = node.annotations().get("source_table")
    if ann:
        return ann
    for p in node.kids("partition"):
        src = next((c.value for c in p.children if c.keyword == "source"), "")
        if m := _M_SCHEMA_ITEM.search(src):
            return f"{m.group(1)}.{m.group(2)}"
        if m := _M_SNOWFLAKE.search(src):
            return f"{m.group(1)}.{m.group(2)}".lower()
    return ""


def _database_name(definition: Path) -> str:
    """The model's display name from ``database.tmdl`` (the folder name may differ: "&" is unsafe in paths)."""
    db = definition / "database.tmdl"
    if db.exists():
        for node in parse_tmdl(db.read_text(encoding="utf-8")):
            if node.keyword == "database":
                return node.name
    return ""


def load_model(model_set: str, folder: Path) -> Model:
    definition = folder / "definition"
    model = Model(model_set, _database_name(definition) or folder.name.removesuffix(".SemanticModel"), folder)
    for path in sorted((definition / "tables").glob("*.tmdl")):
        for node in parse_tmdl(path.read_text(encoding="utf-8")):
            if node.keyword != "table":
                continue
            t = Table(node.name, node, source_table_of(node))
            for c in node.kids("column"):
                src = c.props().get("sourceColumn", "")
                t.columns[c.name] = src
            model.tables[t.name] = t
    rel = definition / "relationships.tmdl"
    if rel.exists():
        model.relationships = [n for n in parse_tmdl(rel.read_text(encoding="utf-8")) if n.keyword == "relationship"]
    for path in sorted((definition / "roles").glob("*.tmdl")) if (definition / "roles").exists() else []:
        model.roles += [n for n in parse_tmdl(path.read_text(encoding="utf-8")) if n.keyword == "role"]
    return model


def discover_models(model_sets=MODEL_SETS) -> list[Model]:
    models = []
    for model_set, root in model_sets:
        for folder in sorted(root.glob("*.SemanticModel")):
            if (folder / "definition").is_dir():
                models.append(load_model(model_set, folder))
    return models


# --------------------------------------------------------------------------- DAX helpers
_MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


def measure_refs(dax: str, measure_names: set[str]) -> set[str]:
    """Bare ``[Name]`` references in ``dax`` that are measures of the same model."""
    return {m for m in _MEASURE_REF.findall(dax) if m in measure_names}


def sql_ident(name: str) -> str:
    """Quote an identifier unless it is plain snake case (``"fxDealerId"``, ``"Claim Amount"``).

    Upper snake case (Snowflake's folded ``CLAIM_KEY``) is written in lower case, which both engines resolve
    to the same column unquoted."""
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
        name = name.lower()
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def sql_table(name: str) -> str:
    return ".".join(sql_ident(p) for p in name.split(".")) if name else "?"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- CSV 1: documentation
DOC_COLUMNS = ("model_set", "model", "object_type", "table", "name", "expression", "display_folder", "format_string",
               "description", "dbt_metric", "source_table", "extra")


def documentation_rows(models: list[Model]) -> list[dict]:
    rows = []
    for m in models:
        for t in m.tables.values():
            base = {"model_set": m.model_set, "model": m.name, "table": t.name, "source_table": t.source_table}
            for meas in t.node.kids("measure"):
                p, a = meas.props(), meas.annotations()
                rows.append({**base, "object_type": "measure", "name": meas.name, "expression": meas.value,
                             "display_folder": p.get("displayFolder", ""), "format_string": p.get("formatString", ""),
                             "description": meas.description, "dbt_metric": a.get("dbt_metric", ""),
                             "extra": "; ".join(f"{k}={v}" for k, v in sorted(a.items()) if k != "dbt_metric")})
            for col in t.node.kids("column"):
                if col.value:  # calculated column
                    rows.append({**base, "object_type": "calculated_column", "name": col.name, "expression": col.value,
                                 "description": col.description})
            for cg in t.node.kids("calculationGroup"):
                prec = cg.props().get("precedence", "")
                for item in cg.kids("calculationItem"):
                    rows.append({**base, "object_type": "calculation_item", "name": item.name, "expression": item.value,
                                 "description": t.node.description,
                                 "extra": f"ordinal={item.props().get('ordinal', '')}; precedence={prec}"})
            for part in t.node.kids("partition"):
                src = next((c.value for c in part.children if c.keyword == "source"), "")
                kind = "calculated_table" if part.value == "calculated" else "partition"
                rows.append({**base, "object_type": kind, "name": part.name, "expression": src,
                             "description": t.node.description if kind == "calculated_table" else "",
                             "extra": f"kind={part.value}; mode={part.props().get('mode', '')}"})
        for role in m.roles:
            perm = role.props().get("modelPermission", "")
            group = role.annotations().get("security_group", "")
            if not role.kids("tablePermission"):  # a role that sees every row still gets documented
                rows.append({"model_set": m.model_set, "model": m.name, "object_type": "rls_filter",
                             "table": "(all tables)", "name": role.name, "expression": "(no row filter)",
                             "description": role.description,
                             "extra": f"modelPermission={perm}; security_group={group}"})
            for tp in role.kids("tablePermission"):
                rows.append({"model_set": m.model_set, "model": m.name, "object_type": "rls_filter",
                             "table": tp.name, "name": role.name, "expression": tp.value,
                             "description": role.description,
                             "source_table": m.tables[tp.name].source_table if tp.name in m.tables else "",
                             "extra": f"modelPermission={perm}; security_group={group}"})
    return rows


# --------------------------------------------------------------------------- CSV 2: relationships
REL_COLUMNS = ("model_set", "model", "relationship", "from_table", "from_column", "to_table", "to_column",
               "cardinality", "cross_filter", "is_active", "from_source", "to_source", "key_enforced", "sql_join")


KEY_LEGACY = "no: no primary or foreign key in the source schema"
KEY_MODERN = "yes: declared key, dbt relationship test"


def relationship_rows(models: list[Model]) -> list[dict]:
    rows = []
    for m in models:
        for r in m.relationships:
            p = r.props()
            ft, fc = split_ref(p.get("fromColumn", ""))
            tt, tc = split_ref(p.get("toColumn", ""))
            f_tab, t_tab = m.tables.get(ft), m.tables.get(tt)
            f_src = f_tab.source_table if f_tab else ""
            t_src = t_tab.source_table if t_tab else ""
            f_col = (f_tab.columns.get(fc) if f_tab else "") or fc
            t_col = (t_tab.columns.get(tc) if t_tab else "") or tc
            active = p.get("isActive", "true") != "false"
            join = (f"from {sql_table(f_src)} as f left join {sql_table(t_src)} as d "
                    f"on f.{sql_ident(f_col)} = d.{sql_ident(t_col)}")
            if not active:
                join += "  -- inactive: only inside USERELATIONSHIP(...)"
            rows.append({"model_set": m.model_set, "model": m.name, "relationship": r.name,
                         "from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc,
                         "cardinality": f"{p.get('fromCardinality', 'many')}-to-{p.get('toCardinality', 'one')}",
                         "cross_filter": p.get("crossFilteringBehavior", "oneDirection"),
                         "is_active": str(active).lower(), "from_source": f_src, "to_source": t_src,
                         "key_enforced": KEY_LEGACY if m.model_set == "legacy" else KEY_MODERN,
                         "sql_join": join})
    return rows


# --------------------------------------------------------------------------- PBIR report
@dataclass
class FieldUse:
    report: str
    page: str
    page_order: int
    visual: str
    visual_type: str
    visual_title: str
    role: str
    entity: str
    field: str
    kind: str              # column | measure | aggregation | hierarchy_level
    filter_values: str = ""


_AGG = {0: "Sum", 1: "Avg", 2: "Count", 3: "Min", 4: "Max", 5: "CountNonNull", 6: "Median"}


def field_ref(expr: dict) -> tuple[str, str, str] | None:
    """(entity, property, kind) of a PBIR field expression, or None."""
    for kind in ("Measure", "Column"):
        if kind in expr:
            node = expr[kind]
            entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            return entity, node.get("Property", ""), kind.lower()
    if "Aggregation" in expr:
        inner = field_ref(expr["Aggregation"].get("Expression", {}))
        if inner:
            fn = _AGG.get(expr["Aggregation"].get("Function"), "Agg")
            return inner[0], f"{fn}({inner[1]})", "aggregation"
    if "HierarchyLevel" in expr:
        hl = expr["HierarchyLevel"]
        h = hl.get("Expression", {}).get("Hierarchy", {})
        entity = h.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        return entity, f"{h.get('Hierarchy', '')}.{hl.get('Level', '')}", "hierarchy_level"
    return None


def _literals(obj) -> list[str]:
    out = []
    if isinstance(obj, dict):
        if "Literal" in obj and isinstance(obj["Literal"], dict):
            out.append(str(obj["Literal"].get("Value", "")).strip("'"))
        for v in obj.values():
            out += _literals(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _literals(v)
    return out


def _filters(config: dict | None) -> list[tuple[str, str, str, str]]:
    found = []
    for f in (config or {}).get("filters", []):
        ref = field_ref(f.get("field", {}))
        if ref:
            found.append((*ref, ", ".join(_literals(f.get("filter", {}).get("Where", [])))))
    return found


def _title(visual: dict) -> str:
    for bucket in ("visualContainerObjects", "objects"):
        for entry in visual.get(bucket, {}).get("title", []):
            text = entry.get("properties", {}).get("text", {})
            vals = _literals(text)
            if vals:
                return vals[0]
    return ""


def _walk_fields(obj, seen: list[tuple[str, str, str]]) -> None:
    if isinstance(obj, dict):
        ref = field_ref(obj)
        if ref and ref[0]:
            seen.append(ref)
            return
        for v in obj.values():
            _walk_fields(v, seen)
    elif isinstance(obj, list):
        for v in obj:
            _walk_fields(v, seen)


def report_uses(report_dir: Path) -> tuple[str, Path | None, list[FieldUse]]:
    """Read a ``<Name>.Report`` folder. Returns (report name, bound model folder, field uses)."""
    name = report_dir.name.removesuffix(".Report")
    bound = None
    pbir = report_dir / "definition.pbir"
    if pbir.exists():
        ref = json.loads(pbir.read_text(encoding="utf-8")).get("datasetReference", {}).get("byPath", {}).get("path")
        bound = (report_dir / ref).resolve() if ref else None
    if bound is not None:  # the display name ("F&I ..."), not the path-safe folder name
        name = _database_name(bound / "definition") or name
    definition = report_dir / "definition"
    uses: list[FieldUse] = []
    report_json = definition / "report.json"
    if report_json.exists():
        for ent, prop, kind, vals in _filters(json.loads(report_json.read_text(encoding="utf-8")).get("filterConfig")):
            uses.append(FieldUse(name, "(all pages)", -1, "", "report_filter", "", "filter", ent, prop, kind, vals))
    pages_dir = definition / "pages"
    order_file = pages_dir / "pages.json"
    order = json.loads(order_file.read_text(encoding="utf-8")).get("pageOrder", []) if order_file.exists() else []
    page_dirs = sorted(pages_dir.glob("*/page.json"), key=lambda p: (order.index(p.parent.name)
                                                                     if p.parent.name in order else 999, p.parent.name))
    for idx, page_json in enumerate(page_dirs):
        page = json.loads(page_json.read_text(encoding="utf-8"))
        pname = page.get("displayName", page_json.parent.name)
        for ent, prop, kind, vals in _filters(page.get("filterConfig")):
            uses.append(FieldUse(name, pname, idx, "", "page_filter", "", "filter", ent, prop, kind, vals))
        for vj in sorted((page_json.parent / "visuals").glob("*/visual.json")):
            container = json.loads(vj.read_text(encoding="utf-8"))
            visual = container.get("visual", {})
            vid, vtype, title = container.get("name", vj.parent.name), visual.get("visualType", ""), _title(visual)
            row = dict(report=name, page=pname, page_order=idx, visual=vid, visual_type=vtype, visual_title=title)
            seen_here: set[tuple[str, str, str]] = set()

            def add(role: str, ref: tuple[str, str, str], vals: str = "", row=row, seen_here=seen_here) -> None:
                key = (role, ref[0], ref[1])
                if key not in seen_here:
                    seen_here.add(key)
                    uses.append(FieldUse(**row, role=role, entity=ref[0], field=ref[1], kind=ref[2],
                                         filter_values=vals))

            query = visual.get("query", {})
            for role, bucket in query.get("queryState", {}).items():
                for proj in bucket.get("projections", []):
                    ref = field_ref(proj.get("field", {}))
                    if ref:
                        add(role, ref)
            for sort in query.get("sortDefinition", {}).get("sort", []):
                ref = field_ref(sort.get("field", {}))
                if ref:
                    add("sort", ref)
            for ent, prop, kind, vals in _filters(container.get("filterConfig")):
                add("filter", (ent, prop, kind), vals)
            fmt: list[tuple[str, str, str]] = []
            _walk_fields(visual.get("objects", {}), fmt)
            for ref in fmt:
                add("formatting", ref)
    return name, bound, uses


def discover_reports(roots=REPORT_ROOTS) -> list[tuple[str, Path | None, list[FieldUse]]]:
    return [report_uses(d) for root in roots for d in sorted(root.glob("*.Report")) if d.is_dir()]


# --------------------------------------------------------------------------- CSV 3 + 4
USAGE_COLUMNS = ("report", "page", "page_order", "visual", "visual_type", "visual_title", "role", "entity", "field",
                 "field_kind", "filter_values", "model", "dax", "source_table", "source_column")
MAP_COLUMNS = ("model_set", "model", "table", "measure", "dbt_metric", "display_folder", "depends_on",
               "used_by_measures", "direct_visuals", "pages", "visuals", "via_measures", "status")


def usage_rows(reports, models: list[Model]) -> list[dict]:
    by_folder = {m.folder.resolve(): m for m in models}
    rows = []
    for _name, bound, uses in reports:
        model = by_folder.get(bound) if bound else None
        for u in uses:
            dax = src_tab = src_col = ""
            if model and u.entity in model.tables:
                t = model.tables[u.entity]
                src_tab = t.source_table
                meas = next((x for x in t.node.kids("measure") if x.name == u.field), None)
                cg_items = [i for cg in t.node.kids("calculationGroup") for i in cg.kids("calculationItem")]
                if meas is not None:
                    dax = _one_line(meas.value)
                elif cg_items:
                    dax = "calculation group: " + ", ".join(i.name for i in cg_items)
                else:
                    src_col = t.columns.get(re.sub(r"^\w+\((.*)\)$", r"\1", u.field), "")
            rows.append({"report": u.report, "page": u.page, "page_order": u.page_order, "visual": u.visual,
                         "visual_type": u.visual_type, "visual_title": u.visual_title, "role": u.role,
                         "entity": u.entity, "field": u.field, "field_kind": u.kind, "filter_values": u.filter_values,
                         "model": model.name if model else "", "dax": dax, "source_table": src_tab,
                         "source_column": src_col})
    return rows


def measure_map_rows(reports, models: list[Model]) -> list[dict]:
    """Each measure with its direct and indirect report use. Domain models share measures with the report
    model through the dbt metric (a measure is defined once, in metrics.yml), so their use is matched on it."""
    by_folder = {m.folder.resolve(): m for m in models}
    used: dict[tuple[str, str], list] = {}  # (model folder, measure) -> uses
    used_metric: dict[str, list] = {}      # dbt_metric -> uses in any report
    for _name, bound, uses in reports:
        model = by_folder.get(bound) if bound else None
        if not model:
            continue
        metric_of = {meas.name: meas.annotations().get("dbt_metric", "") for _t, meas in model.measures()}
        for u in uses:
            if u.kind == "measure" and u.field in metric_of:
                used.setdefault((str(model.folder), u.field), []).append(u)
                if metric_of[u.field]:
                    used_metric.setdefault(metric_of[u.field], []).append(u)
    reported_models = {str(b) for _n, b, _u in reports if b}
    rows = []
    for m in models:
        names = {meas.name for _t, meas in m.measures()}
        deps = {meas.name: measure_refs(meas.value, names) - {meas.name} for _t, meas in m.measures()}
        used_by: dict[str, set[str]] = {n: set() for n in names}
        for n, ds in deps.items():
            for d in ds:
                used_by[d].add(n)

        def direct(name: str, meas: Node, m=m) -> list:
            if str(m.folder.resolve()) in reported_models:
                return used.get((str(m.folder), name), [])
            metric = meas.annotations().get("dbt_metric", "")
            return used_metric.get(metric, []) if metric else []

        by_name = {meas.name: meas for _t, meas in m.measures()}
        for t, meas in m.measures():
            uses = direct(meas.name, meas)
            # walk up: measures that (transitively) depend on this one and are used in a report
            via, stack, seen = set(), list(used_by[meas.name]), set()
            while stack:
                parent = stack.pop()
                if parent in seen:
                    continue
                seen.add(parent)
                if direct(parent, by_name[parent]):
                    via.add(parent)
                stack += used_by[parent]
            if uses:
                status = "used"
            elif via:
                status = "used_via_measure"
            elif m.model_set == "legacy":
                status = "no_report"
            else:
                status = "unused"
            rows.append({"model_set": m.model_set, "model": m.name, "table": t.name, "measure": meas.name,
                         "dbt_metric": meas.annotations().get("dbt_metric", ""),
                         "display_folder": meas.props().get("displayFolder", ""),
                         "depends_on": "; ".join(sorted(deps[meas.name])),
                         "used_by_measures": "; ".join(sorted(used_by[meas.name])),
                         "direct_visuals": len({(u.report, u.page, u.visual) for u in uses}),
                         "pages": "; ".join(sorted({u.page for u in uses})),
                         "visuals": "; ".join(sorted({f"{u.page} / {u.visual_title or u.visual_type} ({u.visual})"
                                                      for u in uses})),
                         "via_measures": "; ".join(sorted(via)), "status": status})
    return rows


# --------------------------------------------------------------------------- write
def _write(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def export(out_dir: Path = DEFAULT_OUT, model_sets=MODEL_SETS, report_roots=REPORT_ROOTS) -> dict[str, int]:
    """Write the four CSVs to ``out_dir``. Returns row counts per file. Deterministic for the same inputs."""
    models = discover_models(model_sets)
    reports = discover_reports(report_roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = {
        "Model_Documentation.csv": (DOC_COLUMNS, documentation_rows(models)),
        "Model_Relationships.csv": (REL_COLUMNS, relationship_rows(models)),
        "Report_Field_Usage.csv": (USAGE_COLUMNS, usage_rows(reports, models)),
        "Measure_Report_Map.csv": (MAP_COLUMNS, measure_map_rows(reports, models)),
    }
    for fname, (cols, rows) in tables.items():
        _write(out_dir / fname, cols, rows)
    return {f: len(rows) for f, (_c, rows) in tables.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m semantic export-docs", description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (default: %(default)s)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    counts = export(args.out)
    for fname, n in counts.items():
        log.info("%-26s %6d rows", fname, n)
    log.info("wrote %d files -> %s", len(counts), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
