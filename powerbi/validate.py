"""Validate the Power BI project: Microsoft's JSON schemas, field references and the layout grid.

1. Every JSON file is validated against the schema its ``$schema`` names (fetched from Microsoft and
   cached, ``powerbi.schemas``); the theme against the report-theme schema of the Desktop release.
2. Every field a visual, filter or sort uses (``Column`` / ``Measure`` with an Entity or a filter alias)
   must exist in the semantic model's TMDL, as the right kind (a measure is never referenced as a column).
   Calculation-item filter values must be items of the calculation group, and every formatting
   ``selector.metadata`` must name a field the visual projects.
3. ``pages.json`` lists every page folder; visual names match their folders; visible visuals stay on the
   page and never overlap.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from powerbi import schemas
from powerbi.build import HERE, MODEL_DIR, PBIP, REPORT_DIR

_NAME = r"('(?:[^']|'')+'|[^\s=']+)"


def _unq(n: str) -> str:
    return n[1:-1].replace("''", "'") if n.startswith("'") else n


@dataclass
class Model:
    columns: dict[str, set[str]] = field(default_factory=dict)
    measures: dict[str, set[str]] = field(default_factory=dict)
    calc_items: dict[str, set[str]] = field(default_factory=dict)  # calc group table -> item names

    def has(self, kind: str, entity: str, prop: str) -> bool:
        pool = self.measures if kind == "Measure" else self.columns
        return prop in pool.get(entity, set())


def read_model(model_dir: Path = MODEL_DIR) -> Model:
    """Tables, columns, measures and calculation items from the TMDL files."""
    m = Model()
    for f in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
        table = None
        for line in f.read_text(encoding="utf-8").splitlines():
            if t := re.match(rf"^table {_NAME}\s*$", line):
                table = _unq(t.group(1))
                m.columns.setdefault(table, set())
                m.measures.setdefault(table, set())
            elif table and (c := re.match(rf"^\t(column|measure) {_NAME}|^\t\t(calculationItem) {_NAME}", line)):
                kind = c.group(1) or c.group(3)
                n = _unq(c.group(2) or c.group(4))
                {"column": m.columns, "measure": m.measures}.get(kind, m.calc_items).setdefault(table, set()).add(n)
    return m


def _walk_fields(node, aliases: dict[str, str], out: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("From"), list):
            aliases = {**aliases, **{f["Name"]: f["Entity"] for f in node["From"] if "Entity" in f}}
        for kind in ("Column", "Measure"):
            v = node.get(kind)
            if isinstance(v, dict) and "Property" in v and isinstance(v.get("Expression"), dict):
                ref = v["Expression"].get("SourceRef", {})
                ent = ref.get("Entity") or aliases.get(ref.get("Source", ""), "?" + str(ref.get("Source")))
                out.append((kind, ent, v["Property"]))
        for v in node.values():
            _walk_fields(v, aliases, out)
    elif isinstance(node, list):
        for v in node:
            _walk_fields(v, aliases, out)


def field_refs(doc) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    _walk_fields(doc, {}, out)
    return out


def check_fields(doc: dict, model: Model, where: str) -> list[str]:
    problems = [f"{where}: {kind} {ent}[{prop}] not in the semantic model"
                for kind, ent, prop in field_refs(doc) if not model.has(kind, ent, prop)]
    # Calculation-item filters: the literal values must be items of the group.
    for flt in (doc.get("filterConfig") or {}).get("filters", []):
        col = flt.get("field", {}).get("Column")
        if not col:
            continue
        ent = col["Expression"]["SourceRef"].get("Entity")
        if ent in model.calc_items:
            for cond in flt.get("filter", {}).get("Where", []):
                for row in cond.get("Condition", {}).get("In", {}).get("Values", []):
                    for v in row:
                        item = v.get("Literal", {}).get("Value", "").strip("'")
                        if item not in model.calc_items[ent]:
                            problems.append(f"{where}: {item!r} is not a {ent} calculation item")
    # Formatting selectors must point at a projected field.
    visual = doc.get("visual") or {}
    refs = {p["queryRef"] for ps in (visual.get("query") or {}).get("queryState", {}).values()
            for p in ps.get("projections", [])}
    for ps in (visual.get("query") or {}).get("queryState", {}).values():
        for p in ps.get("projections", []):
            kind = next(iter(p["field"]))
            f = p["field"][kind]
            if p["queryRef"] != f"{f['Expression']['SourceRef'].get('Entity')}.{f['Property']}":
                problems.append(f"{where}: queryRef {p['queryRef']!r} does not match its field")
    for objs in (visual.get("objects") or {}).values():
        for o in objs:
            meta = (o.get("selector") or {}).get("metadata")
            if meta and meta not in refs:
                problems.append(f"{where}: selector metadata {meta!r} is not a projected field")
    return problems


def _overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])


def check_layout(report_dir: Path = REPORT_DIR) -> list[str]:
    problems: list[str] = []
    pages_dir = report_dir / "definition" / "pages"
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8")).get("pageOrder", [])
    folders = sorted(p.name for p in pages_dir.iterdir() if p.is_dir())
    if sorted(order) != folders:
        problems.append(f"pages.json pageOrder {order} != page folders {folders}")
    for pdir in pages_dir.iterdir():
        if not pdir.is_dir():
            continue
        page = json.loads((pdir / "page.json").read_text(encoding="utf-8"))
        if page["name"] != pdir.name:
            problems.append(f"page {pdir.name}: name {page['name']!r} differs from its folder")
        boxes = []
        for vf in sorted((pdir / "visuals").glob("*/visual.json")):
            v = json.loads(vf.read_text(encoding="utf-8"))
            where = f"{page['displayName']} / {vf.parent.name}"
            if v["name"] != vf.parent.name:
                problems.append(f"{where}: name differs from its folder")
            pos = v["position"]
            if pos["x"] < 0 or pos["y"] < 0 or pos["x"] + pos["width"] > page["width"] \
                    or pos["y"] + pos["height"] > page["height"]:
                problems.append(f"{where}: outside the {page['width']}x{page['height']} page")
            if not v.get("isHidden"):
                for other_where, other in boxes:
                    if _overlap(pos, other):
                        problems.append(f"{where}: overlaps {other_where}")
                boxes.append((where, pos))
    return problems


def json_files() -> list[Path]:
    files = [PBIP, *sorted(REPORT_DIR.rglob("*.json")), *sorted(REPORT_DIR.rglob("*.pbir")),
             *sorted(MODEL_DIR.glob("*.pbism"))]
    return [f for f in files if ".pbi" not in f.relative_to(HERE).parts]


def check_schemas(*, offline: bool = False) -> tuple[list[str], list[str]]:
    problems, skipped = [], []
    validators: dict[str, object] = {}
    for f in json_files():
        doc = json.loads(f.read_text(encoding="utf-8"))
        url = doc.get("$schema") or (schemas.THEME_SCHEMA if "RegisteredResources" in f.parts else None)
        rel = str(f.relative_to(HERE))
        if not url:
            problems.append(f"{rel}: no $schema")
            continue
        try:
            if url not in validators:
                validators[url] = schemas.validator(url, offline=offline)
        except schemas.SchemaUnavailable as exc:
            skipped.append(f"{rel} ({exc})")
            continue
        for err in sorted(validators[url].iter_errors(doc), key=lambda e: list(e.absolute_path)):
            path = "/".join(map(str, err.absolute_path))
            problems.append(f"{rel}: {path}: {err.message[:300]}")
    return problems, skipped


def validate_project(*, offline: bool = False) -> tuple[list[str], list[str]]:
    """(problems, schemas skipped because they could not be fetched)."""
    model = read_model()
    problems = []
    for f in sorted(REPORT_DIR.rglob("*.json")):
        if "RegisteredResources" in f.parts or ".pbi" in f.parts:
            continue
        problems += check_fields(json.loads(f.read_text(encoding="utf-8")), model, str(f.relative_to(HERE)))
    problems += check_layout()
    schema_problems, skipped = check_schemas(offline=offline)
    return problems + schema_problems, skipped
