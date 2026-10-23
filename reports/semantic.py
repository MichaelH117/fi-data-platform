"""Semantic model page (``semantic.html``): what the generated Power BI model contains.

A report plug-in like any area package: ``build(con, out_dir) -> list[Page]``. It regenerates the
model from ``dbt/models/metrics.yml`` (cheap and deterministic, so it never depends on step order),
zips the TMDL folders to ``site/downloads/semantic/`` and lists every measure next to its dbt metric.
It reads no warehouse tables.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from reports.build import Kpi, Page, Section
from semantic import spec
from semantic.build import CALC_ITEMS, generate, role_filters

NAV_LABEL = "Semantic model"
ZIP_NAME = "semantic_model_tmdl.zip"


def build(con, out_dir: Path) -> list[Page]:
    dl = out_dir / "downloads" / "semantic"
    dl.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp) / "model"
        plan = generate(model_dir)
        with zipfile.ZipFile(dl / ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(model_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(model_dir).as_posix())

    domains_of: dict[str, list[str]] = {}
    for d, dm in plan.models.items():
        for m in dm.measures:
            domains_of.setdefault(m.metric, []).append(d)

    measure_rows = [[m.name, m.metric, m.folder, ", ".join(domains_of.get(m.metric, [])), m.format,
                     m.dax.split("\n")[0][:90]] for m in plan.measures.values()]
    domain_rows = []
    for d, dm in plan.models.items():
        incr = [f for f in dm.facts if spec.FACTS[f].incremental]
        domain_rows.append([d, str(len(dm.measures)), ", ".join(dm.facts), str(len(dm.dims)),
                            str(len(dm.relationships)), ", ".join(dm.roles), ", ".join(incr) or "none"])
    role_rows = []
    for d, dm in plan.models.items():
        present = set(dm.dims) | set(dm.facts)
        for r in dm.roles:
            filters = role_filters(r, present)
            role_rows.append([f"{d} / {r}", str(len(filters)), ", ".join(filters) or "no row filter"])
    tv_rows = [[k, v["calculation_item"], v["base_metric"]] for k, v in sorted(plan.time_variants.items())]
    skip_rows = [[k, v] for k, v in sorted(plan.not_exported.items())]

    n_measures = sum(len(dm.measures) for dm in plan.models.values())
    kpis = [Kpi("Domain models", str(len(plan.models))),
            Kpi("Distinct measures", str(len(plan.measures))),
            Kpi("Measures across models", str(n_measures)),
            Kpi("Calculation items", str(len(CALC_ITEMS)))]
    sections = [
        Section("Domain models", "One thin model per audience instead of one monolith. Facts relate straight to "
                "conformed dimensions (a star), so every filter has exactly one path.",
                cols=["Model", "Measures", "Facts", "Dims", "Relationships", "Roles", "Incremental refresh"],
                rows=domain_rows, wide=True, table_open=True),
        Section("Measures and their dbt metric", "Every measure is generated from dbt/models/metrics.yml. "
                "Nothing here is hand-written DAX, and no measure is a time-suffixed copy.",
                cols=["Measure", "dbt metric", "Folder", "Models", "Format", "DAX (first line)"],
                rows=measure_rows, wide=True, table_open=True,
                note=f"Download the TMDL folders: downloads/semantic/{ZIP_NAME} "
                     "(open in Tabular Editor or a Power BI project)."),
        Section("Time intelligence", "One calculation group on the posting-period calendar, capped at the last "
                "closed period (never the open month). Trailing-12 metrics in metrics.yml are served by its T12M "
                "item instead of being separate measures.",
                cols=["Metric", "Calculation item", "Applied to"], rows=tv_rows, table_open=True,
                note="Items: " + ", ".join(i for i, _, _ in CALC_ITEMS)),
        Section("Row-level security", "Dynamic roles read the user -> tenant, dealer map (Security User Access). "
                "See docs/ACCESS_MATRIX.md for audiences, workspaces and groups.",
                cols=["Model / role", "Filtered tables", "Tables"], rows=role_rows, wide=True, table_open=True),
    ]
    kpi_rows = [[label, plan.measures[metric].name, metric] for label, metric in spec.HEADLINE_KPIS.items()
                if metric in plan.measures]
    kpi_rows += [[label, "not in the model", f"{metric} (metrics.yml, V1 model not imported)"]
                 for label, metric in spec.HEADLINE_KPIS_METRIC_ONLY.items()]
    kpi_rows += [[label, "not in the model", why] for label, why in spec.HEADLINE_KPIS_OUTSIDE.items()]
    sections.append(Section("Site headline KPIs", "Each headline tile on this site and the measure that reproduces "
                            "it (same definition, same basis). Time windows come from the calculation group, capped "
                            "at the last closed month like every page.",
                            cols=["Site KPI", "Measure", "dbt metric / reason"], rows=kpi_rows, table_open=True))
    if skip_rows:
        sections.append(Section("Metrics not exported", "V1 metrics on deprecated models stay in metrics.yml for "
                                "one release but are not part of the Power BI model.",
                                cols=["Metric", "Reason"], rows=skip_rows, table_open=True))
    lede = ("The Power BI-ready semantic layer: four domain models generated from the governed metric "
            "definitions, one time-intelligence calculation group, a field-parameter drill path and dynamic RLS.")
    return [Page("semantic.html", "Semantic model", lede, kpis, sections)]
