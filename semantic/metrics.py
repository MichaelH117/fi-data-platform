"""Read ``dbt/models/metrics.yml`` and turn each governed metric into one DAX measure.

The YAML stays the single definition. The generator only translates it:

==============  ==========================================================================
metric type     DAX
==============  ==========================================================================
simple          the measure's aggregation over its Power BI table (SUM, COUNTROWS, ...)
ratio           ``DIVIDE([numerator], [denominator])``
derived         the expression with every metric name replaced by its measure
cumulative      not a measure: served by a calculation item of the time-intelligence group
==============  ==========================================================================

Measure ``expr`` values must be a plain column or one of three CASE shapes (boolean flag, equality,
IN list). Anything else raises ``TranslationError``, so a formula never gets silently re-invented in
DAX. Custom keys live under ``config.meta.pbi`` so ``dbt parse`` still accepts the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DOMAINS: tuple[str, ...] = ("Sales", "Claims", "Finance", "Compliance")
FORMATS: dict[str, str] = {
    "currency": r"\$#,0;(\$#,0);\$#,0",
    "integer": "#,0",
    "decimal": "#,0.00",
    "percent": "0.0%",
}
# Time-variant words a measure name may not carry: the calculation group owns them.
TIME_WORDS = ("MTD", "QTD", "YTD", "PY", "YOY", "T12", "T12M", "TTM", "KPI", "LY", "PRIOR")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CASE_BOOL = re.compile(r"^case when (\w+) then (1|\w+) else 0 end$", re.I)
_CASE_EQ = re.compile(r"^case when (\w+) = '([^']*)' then 1 else 0 end$", re.I)
_CASE_IN = re.compile(r"^case when (\w+) in \(([^)]*)\) then 1 else 0 end$", re.I)
# "case when <col> = 'v' then <amount> else 0 end" and the IN-list variant: a filtered sum of a column.
_CASE_EQ_SUM = re.compile(r"^case when (\w+) = '([^']*)' then ([a-z_]\w*) else 0 end$", re.I)
_CASE_IN_SUM = re.compile(r"^case when (\w+) in \(([^)]*)\) then ([a-z_]\w*) else 0 end$", re.I)
# Balances never take an open (partial) month: the cap is the last CLOSED period from Reporting Date.
LAST_CLOSED = "'Reporting Date'[Last Closed Period Key]"


class TranslationError(ValueError):
    """A metric or measure the generator cannot express in DAX without re-deriving it."""


@dataclass(frozen=True)
class Measure:
    """A MetricFlow measure on one semantic model."""

    name: str
    agg: str
    expr: str
    model: str
    description: str = ""
    non_additive: str | None = None  # window_choice (min | max) when the measure is a balance


@dataclass(frozen=True)
class SemanticModel:
    name: str
    dbt_model: str  # e.g. fct_gross_premium
    table: str | None  # Power BI table name; None = not exported (V1 compat models)
    domains: tuple[str, ...]
    primary_expr: str | None
    measures: tuple[Measure, ...]


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    type: str
    type_params: dict
    description: str = ""
    pbi: dict = field(default_factory=dict)
    owner: str = ""  # config.meta.owner: the role that answers for the definition
    certified: bool = False  # config.meta.certified: the one definition of record for a headline KPI
    definition_of_record: str = ""  # config.meta.definition_of_record: docs/METRICS.md#section

    @property
    def inputs(self) -> list[str]:
        """Metric names a ratio or derived metric is built from."""
        tp = self.type_params
        if self.type == "ratio":
            return [_ref(tp["numerator"]), _ref(tp["denominator"])]
        if self.type == "derived":
            return [_ref(m) for m in tp.get("metrics", [])]
        return []


def _ref(v: str | dict) -> str:
    return v["name"] if isinstance(v, dict) else str(v)


@dataclass
class Catalog:
    models: dict[str, SemanticModel]
    metrics: dict[str, Metric]
    measures: dict[str, Measure]

    @classmethod
    def load(cls, path: Path) -> Catalog:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        models: dict[str, SemanticModel] = {}
        measures: dict[str, Measure] = {}
        for sm in doc.get("semantic_models", []):
            pbi = (sm.get("config") or {}).get("meta", {}).get("pbi", {})
            primary = next((e for e in sm.get("entities", []) if e.get("type") == "primary"), None)
            ms = []
            for m in sm.get("measures", []):
                nad = m.get("non_additive_dimension")
                ms.append(Measure(
                    name=m["name"], agg=m["agg"], expr=str(m.get("expr", m["name"])), model=sm["name"],
                    description=m.get("description", ""),
                    non_additive=(nad or {}).get("window_choice", "max") if nad else None,
                ))
            ref = re.search(r"ref\('([^']+)'\)", sm["model"])
            model = SemanticModel(
                name=sm["name"], dbt_model=ref.group(1) if ref else sm["model"], table=pbi.get("table"),
                domains=tuple(pbi.get("domains", [])),
                primary_expr=(primary or {}).get("expr", (primary or {}).get("name")),
                measures=tuple(ms),
            )
            bad = set(model.domains) - set(DOMAINS)
            if bad:
                raise TranslationError(f"semantic model {model.name}: unknown domains {sorted(bad)}")
            models[model.name] = model
            for m in ms:
                measures[m.name] = m
        metrics = {}
        for mt in doc.get("metrics", []):
            meta = (mt.get("config") or {}).get("meta") or {}
            pbi = meta.get("pbi", {})
            metrics[mt["name"]] = Metric(
                name=mt["name"], label=mt.get("label", mt["name"]), type=mt["type"],
                type_params=mt.get("type_params", {}), description=mt.get("description", ""), pbi=pbi,
                owner=str(meta.get("owner", "")), certified=meta.get("certified") is True,
                definition_of_record=str(meta.get("definition_of_record", "")),
            )
        return cls(models, metrics, measures)

    # ------------------------------------------------------------------ classification
    def model_of(self, metric: Metric) -> SemanticModel:
        """The semantic model of a simple metric's measure."""
        return self.models[self.measures[metric.type_params["measure"]].model]

    def time_variant(self, metric: Metric) -> tuple[str, str] | None:
        """(calculation item, base metric) when the metric is a time variant served by the calc group."""
        if metric.pbi.get("calculation_item"):
            return metric.pbi["calculation_item"], metric.pbi.get("base_metric", "")
        if metric.type == "cumulative":
            window = str(metric.type_params.get("cumulative_type_params", {}).get("window", ""))
            if window.replace(" ", "") in ("12months", "12month", "1year"):
                measure = metric.type_params.get("measure")
                base = next((m.name for m in self.metrics.values()
                             if m.type == "simple" and m.type_params.get("measure") == measure), "")
                return "T12M", base
            raise TranslationError(f"cumulative metric {metric.name}: only a 12-month window maps to T12M")
        return None

    def domains_of(self, metric: Metric, _seen: frozenset[str] = frozenset()) -> tuple[str, ...]:
        """Domain models a metric is exported to (meta override, else derived from its inputs)."""
        if metric.name in _seen:
            raise TranslationError(f"metric cycle at {metric.name}")
        if metric.pbi.get("domains"):
            return tuple(d for d in DOMAINS if d in metric.pbi["domains"])
        if metric.type == "simple":
            return tuple(d for d in DOMAINS if d in self.model_of(metric).domains)
        sets = [set(self.domains_of(self.metrics[i], _seen | {metric.name})) for i in metric.inputs]
        common = set.intersection(*sets) if sets else set()
        return tuple(d for d in DOMAINS if d in common)

    def exportable(self, metric: Metric) -> str | None:
        """None when the metric becomes a measure, otherwise the reason it does not."""
        if self.time_variant(metric):
            return "time variant: served by the calculation group"
        if metric.type == "simple":
            if self.model_of(metric).table is None:
                return f"V1 semantic model {self.model_of(metric).name} (not exported)"
            return None
        for i in metric.inputs:
            if i not in self.metrics:
                raise TranslationError(f"metric {metric.name}: unknown input metric {i}")
            why = self.exportable(self.metrics[i])
            if why:
                return f"input {i}: {why}"
        return None

    def tables_of(self, metric: Metric) -> set[str]:
        """Power BI fact tables a metric reads, through its inputs."""
        if metric.type == "simple":
            return {self.model_of(metric).table or ""}
        return set().union(*(self.tables_of(self.metrics[i]) for i in metric.inputs))


# ---------------------------------------------------------------------- naming and formats
def measure_name(metric: Metric) -> str:
    """Business-friendly measure name: meta ``name``, else the metric name in title case."""
    if metric.pbi.get("name"):
        return str(metric.pbi["name"])
    return " ".join("Average" if w == "avg" else w.capitalize() for w in metric.name.split("_"))


def has_time_word(name: str) -> bool:
    tokens = re.split(r"[\s_()\-/%]+", name.upper())
    return any(t in TIME_WORDS for t in tokens)


def format_of(metric: Metric, catalog: Catalog) -> str:
    key = metric.pbi.get("format")
    if not key:
        if metric.type == "ratio":
            key = "percent"
        elif metric.type == "simple":
            agg = catalog.measures[metric.type_params["measure"]].agg
            key = {"count": "integer", "count_distinct": "integer", "sum_boolean": "integer",
                   "average": "decimal"}.get(agg, "currency")
        else:
            key = "currency"
    if key not in FORMATS:
        raise TranslationError(f"metric {metric.name}: unknown format {key!r}")
    return key


# ---------------------------------------------------------------------- DAX
def friendly(column: str) -> str:
    """snake_case warehouse column -> Power BI column name ('dealer_name' -> 'Dealer Name')."""
    return " ".join(w.upper() if w in ("id", "vin", "apr", "msrp", "pii") else w.capitalize()
                    for w in column.split("_"))


def q(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def col(table: str, column: str) -> str:
    return f"{q(table)}[{friendly(column)}]"


def _listed(raw: str) -> str:
    return ", ".join(f'"{v.strip().strip(chr(39))}"' for v in raw.split(","))


def measure_dax(m: Measure, table: str, primary_expr: str | None, period_table: str = "Posting Period",
                period_column: str = "posting_period_key", cap: str | None = LAST_CLOSED) -> str:
    """DAX for one MetricFlow measure on ``table``.

    ``period_column`` is the fact's yyyymm period column (a balance takes its last value there); ``cap``
    keeps a balance at or before the last closed period (None: no cap).
    """
    expr = m.expr.strip()
    agg = m.agg
    if _IDENT.match(expr):
        c = col(table, expr)
        if agg == "sum":
            base = f"SUM({c})"
        elif agg == "count":
            base = f"COUNTROWS({q(table)})" if expr == primary_expr else f"COUNT({c})"
        elif agg == "count_distinct":
            base = f"DISTINCTCOUNT({c})"
        elif agg == "average":
            base = f"AVERAGE({c})"
        elif agg in ("min", "max"):
            base = f"{agg.upper()}({c})"
        elif agg == "sum_boolean":
            base = f"CALCULATE(COUNTROWS({q(table)}), {c} = TRUE())"
        else:
            raise TranslationError(f"measure {m.name}: aggregation {agg!r} has no DAX mapping")
    elif agg == "sum" and (hit := _CASE_EQ_SUM.match(expr)) and hit.group(3) != "1":
        base = f'CALCULATE(SUM({col(table, hit.group(3))}), {col(table, hit.group(1))} = "{hit.group(2)}")'
    elif agg == "sum" and (hit := _CASE_IN_SUM.match(expr)) and hit.group(3) != "1":
        base = f"CALCULATE(SUM({col(table, hit.group(3))}), {col(table, hit.group(1))} IN {{{_listed(hit.group(2))}}})"
    elif agg == "sum" and (hit := _CASE_EQ.match(expr)):
        base = f'CALCULATE(COUNTROWS({q(table)}), {col(table, hit.group(1))} = "{hit.group(2)}")'
    elif agg == "sum" and (hit := _CASE_IN.match(expr)):
        values = [v.strip().strip("'") for v in hit.group(2).split(",")]
        listed = ", ".join(f'"{v}"' for v in values)
        base = f"CALCULATE(COUNTROWS({q(table)}), {col(table, hit.group(1))} IN {{{listed}}})"
    elif agg == "sum" and (hit := _CASE_BOOL.match(expr)):
        flag, value = hit.group(1), hit.group(2)
        inner = f"COUNTROWS({q(table)})" if value == "1" else f"SUM({col(table, value)})"
        base = f"CALCULATE({inner}, {col(table, flag)} = TRUE())"
    else:
        raise TranslationError(
            f"measure {m.name}: expr {expr!r} is not a column or a supported CASE shape; "
            "add a column upstream in dbt instead of re-deriving it in DAX")
    if m.non_additive:
        # A balance (in force, unearned premium): take the last (or first) posted period in context.
        # Capped at the last closed period: the default card never shows a mid-month balance, and an open
        # month on its own is blank (the site reports complete months only).
        fn = "MAX" if m.non_additive == "max" else "MIN"
        key = col(table, period_column)
        pick = (f"VAR _cap = MAX({cap})\nVAR _period = CALCULATE({fn}({key}), {key} <= _cap)" if cap
                else f"VAR _period = {fn}({key})")
        return (f"{pick}\nRETURN\n    CALCULATE({base}, "
                f"{col(period_table, 'posting_period_key')} = _period)")
    return base


_NAME_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def metric_dax(metric: Metric, catalog: Catalog, names: dict[str, str]) -> str:
    """DAX for a ratio or derived metric, referencing the generated measures by name."""
    if metric.type == "ratio":
        num, den = metric.inputs
        return f"DIVIDE([{names[num]}], [{names[den]}])"
    if metric.type == "derived":
        expr = metric.type_params["expr"]
        used = set(metric.inputs)
        unknown = {t for t in _NAME_TOKEN.findall(expr)} - used
        if unknown:
            raise TranslationError(f"metric {metric.name}: unsupported tokens {sorted(unknown)} in {expr!r}")

        def sub(text: str) -> str:
            return _NAME_TOKEN.sub(lambda mm: f"[{names[mm.group(0)]}]", text)

        parts = expr.split("/")
        if len(parts) == 2:
            return f"DIVIDE({sub(parts[0].strip())}, {sub(parts[1].strip())})"
        if len(parts) > 2:
            raise TranslationError(f"metric {metric.name}: chained division is ambiguous; split the metric")
        return sub(expr)
    raise TranslationError(f"metric {metric.name}: type {metric.type} is not a measure")
