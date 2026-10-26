"""Generate the TMDL-style Power BI model from ``dbt/models/metrics.yml`` plus ``semantic.spec``.

Output, one folder per domain model (``semantic/model/<Domain>.SemanticModel/``)::

    definition.pbism
    definition/database.tmdl  model.tmdl  expressions.tmdl  relationships.tmdl
    definition/tables/<Table>.tmdl     facts, dims, Measures, Time Intelligence, Drill Level, security
    definition/roles/<Role>.tmdl       Internal, Tenant Partner, Dealer User (dynamic RLS)

plus ``semantic/model/manifest.json`` (every measure -> its dbt metric). Output is deterministic (no
timestamps), so ``python -m semantic --check`` can tell whether the committed files are stale.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from semantic import spec
from semantic.metrics import (
    DOMAINS,
    FORMATS,
    Catalog,
    TranslationError,
    col,
    format_of,
    friendly,
    has_time_word,
    measure_dax,
    measure_name,
    metric_dax,
    q,
)
from semantic.tmdl import TAB, annotation, description, join, name, prop_expr

ROOT = Path(__file__).resolve().parents[1]
METRICS_PATH = ROOT / "dbt" / "models" / "metrics.yml"
OUT_DIR = ROOT / "semantic" / "model"
# Source targets for the M partitions (python -m semantic --target). snowflake is the checked-in default
# (semantic/model/); postgres points at the local server that python -m warehouse.postgres sync fills.
TARGETS: tuple[str, ...] = ("snowflake", "postgres")
DEFAULT_TARGET = "snowflake"
TARGET_OUT_DIRS = {"snowflake": OUT_DIR, "postgres": ROOT / "semantic" / "model_postgres"}
LOCK_PATH = ROOT / "semantic" / "columns.lock.json"

Columns = dict[str, list[tuple[str, str]]]  # source table -> [(column, duckdb type)]


# ---------------------------------------------------------------------- column lock
def read_warehouse_columns(con) -> Columns:
    """Columns of every table the spec uses, straight from the warehouse catalog."""
    out: Columns = {}
    for t in spec.ALL_TABLES.values():
        schema, table = t.source.split(".")
        rows = con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = ? and table_name = ? order by ordinal_position", [schema, table]).fetchall()
        if rows:
            out[t.source] = [(c, d) for c, d in rows]
    return out


def load_lock(path: Path = LOCK_PATH) -> Columns:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: [(c, d) for c, d in v] for k, v in raw["tables"].items()}


def write_lock(columns: Columns, path: Path = LOCK_PATH) -> None:
    doc = {"_comment": "Column snapshot of the core marts used by the semantic model. "
                       "Refresh with: python -m semantic --refresh-columns",
           "tables": {k: [list(x) for x in v] for k, v in sorted(columns.items())}}
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def visible_columns(columns: Columns, t: spec.TableSpec) -> list[tuple[str, str]]:
    return [(c, d) for c, d in columns.get(t.source, []) if not spec.is_dropped(t, c)]


# ---------------------------------------------------------------------- plan
@dataclass
class ModelMeasure:
    name: str
    metric: str
    dax: str
    format: str
    folder: str
    description: str
    tables: tuple[str, ...]
    owner: str = ""
    certified: bool = False


@dataclass
class DomainModel:
    domain: str
    measures: list[ModelMeasure] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    dims: list[str] = field(default_factory=list)
    relationships: list[tuple[str, str, str, str]] = field(default_factory=list)  # from t, from c, to t, to c
    inactive: list[tuple[str, str, str, str]] = field(default_factory=list)  # role-playing (USERELATIONSHIP)
    roles: tuple[str, ...] = ()
    drill: list[tuple[str, str, str]] = field(default_factory=list)
    payee_sources: list[tuple[str, str]] = field(default_factory=list)  # (fact, account-holder FK column)
    purpose: str = ""  # set for a reporting model that is not one of the four domains (powerbi/build.py)


@dataclass
class Plan:
    models: dict[str, DomainModel]
    measures: dict[str, ModelMeasure]  # metric name -> measure
    time_variants: dict[str, dict]
    not_exported: dict[str, str]
    metrics_sha256: str


def make_plan(catalog: Catalog, columns: Columns, metrics_text: str = "") -> Plan:
    names: dict[str, str] = {}
    time_variants: dict[str, dict] = {}
    not_exported: dict[str, str] = {}
    exported = []
    for m in catalog.metrics.values():
        tv = catalog.time_variant(m)
        if tv:
            item, base = tv
            if base not in catalog.metrics:
                raise TranslationError(f"time-variant metric {m.name}: base metric {base!r} not found")
            time_variants[m.name] = {"calculation_item": item, "base_metric": base}
            continue
        why = catalog.exportable(m)
        if why:
            not_exported[m.name] = why
            continue
        exported.append(m)
        names[m.name] = measure_name(m)

    seen: dict[str, str] = {}
    for metric, nm in names.items():
        if has_time_word(nm):
            raise TranslationError(f"measure {nm!r} ({metric}) carries a time word: use the calculation group")
        if nm.lower() in seen:
            raise TranslationError(f"measure name {nm!r} used by {seen[nm.lower()]} and {metric}")
        seen[nm.lower()] = metric
    labels: dict[str, str] = {}
    for m in catalog.metrics.values():
        key = " ".join((m.label or "").lower().split())
        if key and key in labels:
            raise TranslationError(f"display label {m.label!r} used by {labels[key]} and {m.name}: "
                                   "give each definition (denominator) its own label")
        labels[key] = m.name
        if m.certified and not (m.owner and m.definition_of_record):
            raise TranslationError(f"certified metric {m.name}: needs config.meta.owner and definition_of_record")

    cols_by_table = {t: {c for c, _ in visible_columns(columns, spec.ALL_TABLES[t])} for t in spec.ALL_TABLES}
    measures: dict[str, ModelMeasure] = {}
    for m in exported:
        if m.type == "simple":
            sm = catalog.model_of(m)
            meas = catalog.measures[m.type_params["measure"]]
            if sm.table not in spec.FACTS:
                raise TranslationError(f"semantic model {sm.name}: Power BI table {sm.table!r} not in spec.FACTS")
            missing = _expr_columns(meas.expr) - cols_by_table[sm.table]
            if missing:
                raise TranslationError(
                    f"measure {meas.name}: columns {sorted(missing)} not in {spec.FACTS[sm.table].source}")
            fact = spec.FACTS[sm.table]
            period_col = spec.PERIOD_LINKS.get(sm.table, ("posting_period_key", ""))[0]
            dax = measure_dax(meas, sm.table, sm.primary_expr, period_column=period_col)
            role = m.pbi.get("date_role")
            if role:
                links = dict(spec.ROLE_PLAYING_PERIODS.get(sm.table, ()))
                if role not in links:
                    raise TranslationError(f"metric {m.name}: date_role {role!r} is not a role-playing period "
                                           f"column of {sm.table} (spec.ROLE_PLAYING_PERIODS)")
                dax = (f"CALCULATE({dax}, USERELATIONSHIP({col(sm.table, role)}, "
                       f"{col(spec.PERIOD, links[role])}))")
            elif fact.period_free and meas.non_additive:
                raise TranslationError(f"measure {meas.name}: {sm.table} has no period, so it cannot be a balance")
            dax = grain_guard(m, sm.table, dax, cols_by_table[sm.table])
            desc = m.description or meas.description
        else:
            dax = metric_dax(m, catalog, names)
            desc = m.description
        measures[m.name] = ModelMeasure(
            name=names[m.name], metric=m.name, dax=dax, format=format_of(m, catalog),
            folder=str(m.pbi.get("folder", "General")), description=(desc or m.label).strip(),
            tables=tuple(sorted(catalog.tables_of(m))), owner=m.owner, certified=m.certified,
        )

    # Domain membership, closed over inputs so every ratio's parts exist in the same model.
    members: dict[str, set[str]] = {d: set() for d in DOMAINS}

    def add(domain: str, metric: str) -> None:
        if metric in members[domain]:
            return
        members[domain].add(metric)
        for i in catalog.metrics[metric].inputs:
            add(domain, i)

    for m in exported:
        doms = catalog.domains_of(m)
        if not doms:
            raise TranslationError(f"metric {m.name}: no domain (set config.meta.pbi.domains)")
        for d in doms:
            add(d, m.name)

    models = {d: _domain_model(d, sorted(members[d]), measures, columns) for d in DOMAINS}
    sha = hashlib.sha256(metrics_text.encode("utf-8")).hexdigest() if metrics_text else ""
    return Plan(models, measures, time_variants, not_exported, sha)


def _dax_value(v) -> str:
    if isinstance(v, bool):
        return "TRUE()" if v else "FALSE()"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '""') + '"'


def grain_guard(metric, table: str, dax: str, columns: set[str]) -> str:
    """Wrap a simple measure in the grain rules of ``config.meta.pbi`` (rules stay in metrics.yml).

    ``single_value: [col, ...]``  BLANK unless each column has one value in context (no adding unlike rows).
    ``window: {column, order_by, last_when: {col: value}}``  across several windows, BLANK, except for rows
        whose aggregation is LAST: those take the latest window (by ``order_by``) instead of a sum.
    ``default_filter: {col: value}``  when the column is not sliced to one value, read only that value
        (e.g. Scope = ITD, so ITD and PERIOD rows are never added together).
    """
    rules = {k: metric.pbi[k] for k in ("single_value", "window", "default_filter") if metric.pbi.get(k)}
    used = set(rules.get("single_value", [])) | set(rules.get("default_filter", {}))
    if "window" in rules:
        w = rules["window"]
        used |= {w["column"], w["order_by"], *w.get("last_when", {})}
    missing = used - columns
    if missing:
        raise TranslationError(f"metric {metric.name}: grain columns {sorted(missing)} not in {table}")
    if "window" in rules:
        w = rules["window"]
        wc, oc = col(table, w["column"]), col(table, w["order_by"])
        last = " && ".join(f"SELECTEDVALUE({col(table, c)}) = {_dax_value(v)}"
                           for c, v in w.get("last_when", {}).items()) or "FALSE()"
        dax = (f"IF(HASONEVALUE({wc}), {dax},\n    IF({last},\n        VAR _end = MAX({oc})\n"
               f"        VAR _w = CALCULATE(MAX({wc}), {oc} = _end)\n"
               f"        RETURN\n            CALCULATE({dax}, {wc} = _w)))")
    if rules.get("single_value"):
        cond = " && ".join(f"HASONEVALUE({col(table, c)})" for c in rules["single_value"])
        dax = f"IF({cond},\n    {dax.replace(chr(10), chr(10) + '    ')})"
    for c, v in rules.get("default_filter", {}).items():
        cc = col(table, c)
        dax = f"IF(HASONEVALUE({cc}), {dax}, CALCULATE({dax}, {cc} = {_dax_value(v)}))"
    return dax


def _expr_columns(expr: str) -> set[str]:
    import re

    words = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", re.sub(r"'[^']*'", "", expr.lower())))
    return words - {"case", "when", "then", "else", "end", "in", "and", "or", "not", "null", "is"}


def _domain_model(domain: str, metric_names: list[str], measures: dict[str, ModelMeasure],
                  columns: Columns, *, allowed: tuple[str, ...] | None = None,
                  roles: tuple[str, ...] | None = None, purpose: str = "") -> DomainModel:
    """One model over ``metric_names``. ``allowed``/``roles``/``purpose`` default to the domain's spec entries;
    a reporting model (``powerbi/build.py``) passes its own."""
    dm = DomainModel(domain, roles=spec.DOMAIN_ROLES[domain] if roles is None else roles, purpose=purpose)
    order = {m: i for i, m in enumerate(measures)}
    dm.measures = [measures[m] for m in sorted(metric_names, key=lambda x: order[x])]
    facts = sorted({t for m in dm.measures for t in m.tables}, key=list(spec.FACTS).index)
    dm.facts = facts
    allowed = spec.DOMAIN_DIMS[domain] if allowed is None else allowed
    dims: set[str] = set()
    rels: list[tuple[str, str, str, str]] = []
    for f in facts:
        fcols = [c for c, _ in columns.get(spec.FACTS[f].source, [])]
        for c in fcols:
            for target in spec.FACT_FK.get(c, ()):
                if target in allowed:
                    dims.add(target)
                    rels.append((f, c, target, spec.DIMS[target].key or c))
        if f in spec.PERIOD_LINKS:
            c, to_c = spec.PERIOD_LINKS[f]
            if c not in fcols:
                raise TranslationError(f"{f}: period column {c} not in {spec.FACTS[f].source}")
            dims.add(spec.PERIOD)
            rels.append((f, c, spec.PERIOD, to_c))
        for c, to_c in spec.ROLE_PLAYING_PERIODS.get(f, ()):
            dm.inactive.append((f, c, spec.PERIOD, to_c))
    for src, c, target in spec.DIM_FK:
        if src in dims and target in allowed:
            dims.add(target)
            rels.append((src, c, target, spec.DIMS[target].key or c))
    dims.add(spec.REPORTING_DATE)
    if any(r != "Internal" for r in dm.roles):
        dims.add(spec.SECURITY)
    dm.dims = [d for d in spec.DIMS if d in dims]
    dm.relationships = rels
    if "Account Holder" in dims and "Dealer User" in dm.roles:
        dm.payee_sources = [(f, c) for f, c, t, _ in rels if t == "Account Holder"]
    present = set(dm.dims)
    dm.drill = [(label, t, c) for label, t, c in spec.DRILL_FIELDS if t in present]
    check_single_paths(dm)
    return dm


def check_single_paths(dm: DomainModel) -> None:
    """Fail when a fact reaches any table by two filter paths (Power BI would mark one inactive)."""
    edges: dict[str, list[str]] = defaultdict(list)
    for f, _, t, _ in dm.relationships:
        edges[f].append(t)
    for fact in dm.facts:
        paths: dict[str, int] = defaultdict(int)
        stack = [fact]
        while stack:
            node = stack.pop()
            for nxt in edges.get(node, []):
                paths[nxt] += 1
                stack.append(nxt)
        dup = sorted(t for t, n in paths.items() if n > 1)
        if dup:
            raise TranslationError(f"{dm.domain}: {fact} reaches {dup} by more than one path")


# ---------------------------------------------------------------------- calculation group
PERIOD_KEY = col(spec.PERIOD, "posting_period_key")
FY = col(spec.PERIOD, "fiscal_year")
FQ = col(spec.PERIOD, "fiscal_quarter")
_P = q(spec.PERIOD)
# The cap is the last CLOSED period (Sep when the as-of date is Oct 21), the same "complete months
# through" rule every site page uses; Current Period Key (the open month) would add a partial month.
LAST_CLOSED = col(spec.REPORTING_DATE, "last_closed_period_key")
_LAST = f"VAR _p = MIN(MAX({PERIOD_KEY}), MAX({LAST_CLOSED}))"
_PY = (f"CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS({_P}),\n"
       f"    TREATAS(SELECTCOLUMNS(VALUES({PERIOD_KEY}), \"@key\", {PERIOD_KEY} - 100), {PERIOD_KEY}))")

# (name, DAX, format string or None). Keys are yyyymm, so "key - 100" is the same month last year.
CALC_ITEMS: tuple[tuple[str, str, str | None], ...] = (
    ("Current", "SELECTEDMEASURE()", None),
    ("MTD", f"{_LAST}\nRETURN\n    CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS({_P}), {PERIOD_KEY} = _p)", None),
    ("QTD", f"{_LAST}\nVAR _fy = CALCULATE(MAX({FY}), REMOVEFILTERS({_P}), {PERIOD_KEY} = _p)\n"
            f"VAR _fq = CALCULATE(MAX({FQ}), REMOVEFILTERS({_P}), {PERIOD_KEY} = _p)\nRETURN\n"
            f"    CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS({_P}), {FY} = _fy, {FQ} = _fq, {PERIOD_KEY} <= _p)",
     None),
    ("YTD", f"{_LAST}\nVAR _fy = CALCULATE(MAX({FY}), REMOVEFILTERS({_P}), {PERIOD_KEY} = _p)\nRETURN\n"
            f"    CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS({_P}), {FY} = _fy, {PERIOD_KEY} <= _p)", None),
    ("PY", _PY, None),
    ("YoY %", f"VAR _cur = SELECTEDMEASURE()\nVAR _py =\n    {_PY.replace(chr(10), chr(10) + '    ')}\n"
              "RETURN\n    IF(NOT ISBLANK(_py), DIVIDE(_cur - _py, ABS(_py)))", '"0.0%"'),
    ("T12M", f"{_LAST}\nRETURN\n    CALCULATE(SELECTEDMEASURE(), REMOVEFILTERS({_P}),\n"
             f"        {PERIOD_KEY} <= _p, {PERIOD_KEY} > _p - 100)", None),
)


# ---------------------------------------------------------------------- RLS
def _user_set(column: str) -> str:
    s = q(spec.SECURITY)
    return (f"CALCULATETABLE(VALUES({col(spec.SECURITY, column)}), "
            f"{s}[{friendly('user_login')}] = USERPRINCIPALNAME())")


UNKNOWN = "-1"  # the unknown member key of the core dims (no tenant, no dealer)
CONTRACT_KEYED = tuple(t for t, spec_t in spec.DIMS.items()
                       if t != "Contract" and spec_t.key == "contract_key")  # the split contract dims


def role_filters(role: str, present: set[str]) -> dict[str, str]:
    """Table -> DAX filter for one role. Internal has none (sees everything in its workspace).

    Every table that carries a contract, dealer or tenant key gets its own filter, or is a fact reached
    only from filtered dims (relationships filter dim -> fact, never back up to a sibling dim). The split
    contract dims sit beside Contract (both hang off the facts), so Contract's filter never reaches them:
    they repeat Contract's rule through the security table, not through a relationship.
    """
    if role == "Internal":
        return {}
    dealers, tenants = _user_set("dealer_key"), _user_set("tenant_key")
    f: dict[str, str] = {spec.SECURITY: f"{q(spec.SECURITY)}[{friendly('user_login')}] = USERPRINCIPALNAME()",
                         "Tenant": f"{col('Tenant', 'tenant_key')} IN {tenants}"}
    by_tenant = ("Dealer", "Dealer Group", "Contract", "Claim", "Account Holder", "Agent", "Participation Entity",
                 spec.PAYEE_ACCESS)
    for t in by_tenant:
        f[t] = f"{col(t, 'tenant_key')} IN {tenants}"
    contract_rule = f"{col('Contract', 'tenant_key')} IN {tenants}"
    if spec.PAYEE_ACCESS in present:
        # Payees (repair shops, the '-1' unknown legacy payee) are shared across tenants (tenant_key 0 or
        # blank), so a tenant_key rule on Account Holder would hide every claim line. Scope payees to the
        # ones paid on the tenant's business instead, through the bridge (evaluated in test_semantic).
        f["Account Holder"] = (f"{col('Account Holder', 'account_holder_key')} IN CALCULATETABLE("
                               f"VALUES({col(spec.PAYEE_ACCESS, 'account_holder_key')}), "
                               f"{col(spec.PAYEE_ACCESS, 'tenant_key')} IN {tenants})")
    if role == "Tenant Partner":
        # Dealers without a group share one group row with no tenant, so groups are scoped through the
        # tenant's dealers (as for Dealer User). The unknown dealer / contract members ('-1', no tenant)
        # stay visible: every fact is also filtered by its own Tenant relationship, so a partner sees
        # its own unattributed rows and the tenant total ties to the facts' tenant_key.
        f["Dealer Group"] = (f"{col('Dealer Group', 'dealer_group_key')} IN CALCULATETABLE("
                             f"VALUES({col('Dealer', 'dealer_group_key')}), "
                             f"{col('Dealer', 'tenant_key')} IN {tenants})")
        f["Dealer"] = f"{col('Dealer', 'tenant_key')} IN {tenants} || {col('Dealer', 'dealer_key')} = \"{UNKNOWN}\""
        contract_rule = (f"{col('Contract', 'tenant_key')} IN {tenants} || "
                         f"{col('Contract', 'contract_key')} = \"{UNKNOWN}\"")
        f["Contract"] = contract_rule
    if role == "Dealer User":
        f["Dealer"] = f"{col('Dealer', 'dealer_key')} IN {dealers}"
        f["Dealer Group"] = (f"{col('Dealer Group', 'dealer_group_key')} IN CALCULATETABLE("
                             f"VALUES({col('Dealer', 'dealer_group_key')}), "
                             f"{col('Dealer', 'dealer_key')} IN {dealers})")
        contract_rule = f"{col('Contract', 'dealer_key')} IN {dealers}"
        f["Contract"] = contract_rule
        f["Claim"] = (f"{col('Claim', 'contract_key')} IN CALCULATETABLE(VALUES({col('Contract', 'contract_key')}), "
                      f"{contract_rule})")
        f["Participation Entity"] = f"{col('Participation Entity', 'dealer_key')} IN {dealers}"
        # Payees: only those paid on this dealer's business (hidden calculated bridge, no relationships).
        f[spec.PAYEE_ACCESS] = f"{col(spec.PAYEE_ACCESS, 'dealer_key')} IN {dealers}"
        f["Account Holder"] = (f"{col('Account Holder', 'account_holder_key')} IN CALCULATETABLE("
                               f"VALUES({col(spec.PAYEE_ACCESS, 'account_holder_key')}), "
                               f"{col(spec.PAYEE_ACCESS, 'dealer_key')} IN {dealers})")
    for t in CONTRACT_KEYED:
        f[t] = (f"{col(t, 'contract_key')} IN CALCULATETABLE(VALUES({col('Contract', 'contract_key')}), "
                f"{contract_rule})")
    out = {t: v for t, v in f.items() if t in present}
    leaked = [t for t in CONTRACT_KEYED if t in present] if "Contract" not in present else []
    if leaked:
        raise TranslationError(f"{role}: {leaked} need the Contract table to apply row-level security")
    return out


ROLE_NOTES = {
    "Internal": "Employees (executive, finance, claims, compliance, field team). No row filter.",
    "Tenant Partner": "White-label partner program: every dealer of the tenants mapped to the signed-in user.",
    "Dealer User": "External dealer: only the dealers mapped to the signed-in user, inside their tenant.",
}
ROLE_GROUPS = {"Internal": "sg-bi-internal-<domain>", "Tenant Partner": "sg-bi-partner-<tenant>",
               "Dealer User": "sg-bi-dealer-users"}


# ---------------------------------------------------------------------- rendering
def _src_col(c: str, target: str) -> str:
    """Physical column name as the source returns it: Snowflake folds to upper case, Postgres keeps lower."""
    return c.lower() if target == "postgres" else c.upper()


def _m_source(t: spec.TableSpec, target: str = DEFAULT_TARGET) -> str:
    if target == "postgres":
        schema, table = t.source.lower().split(".")
        lines = [
            "let",
            "    Source = PostgreSQL.Database(PostgresServer, PostgresDatabase),",
            f'    Data = Source{{[Schema = "{schema}", Item = "{table}"]}}[Data]',
        ]
    else:
        schema, table = t.source.upper().split(".")
        lines = [
            "let",
            '    Source = Snowflake.Databases(SnowflakeServer, SnowflakeWarehouse, [Role = "REPORTER"]),',
            '    Db = Source{[Name = AnalyticsDatabase, Kind = "Database"]}[Data],',
            f'    Schema = Db{{[Name = "{schema}", Kind = "Schema"]}}[Data],',
            f'    Data = Schema{{[Name = "{table}", Kind = "Table"]}}[Data]',
        ]
    if t.incremental:
        pk = _src_col("posting_period_key", target)
        lines[-1] += ","
        lines += ["    Filtered = Table.SelectRows(Data, each",
                  f"        [{pk}] >= Date.Year(RangeStart) * 100 + Date.Month(RangeStart)",
                  f"        and [{pk}] < Date.Year(RangeEnd) * 100 + Date.Month(RangeEnd))",
                  "in", "    Filtered"]
    else:
        lines += ["in", "    Data"]
    return "\n".join(lines)


def render_table(t: spec.TableSpec, columns: Columns, target: str = DEFAULT_TARGET) -> str:
    out = [*description(t.note, 0), f"table {name(t.name)}"]
    for c, d in visible_columns(columns, t):
        dtype = spec.tmdl_type(d)
        out += ["", f"{TAB}column {name(friendly(c))}", f"{TAB * 2}dataType: {dtype}"]
        if spec.is_hidden(t, c, dtype):
            out.append(f"{TAB * 2}isHidden")
        if dtype in ("int64", "double", "decimal"):
            out.append(f"{TAB * 2}summarizeBy: none")
        if dtype == "dateTime":
            out.append(f"{TAB * 2}formatString: yyyy-mm-dd")
        if c == t.key:
            out.append(f"{TAB * 2}isKey")
        out.append(f"{TAB * 2}sourceColumn: {_src_col(c, target)}")
    if t.name in spec.HIERARCHIES:
        h_name, levels = spec.HIERARCHIES[t.name]
        out += ["", f"{TAB}hierarchy {name(h_name)}"]
        for lv in levels:
            out += ["", f"{TAB * 2}level {name(friendly(lv))}", f"{TAB * 3}column: {name(friendly(lv))}"]
    out += ["", f"{TAB}partition {name(t.name)} = m", f"{TAB * 2}mode: import",
            *prop_expr("source", _m_source(t, target), 2)]
    if t.incremental:
        out += ["", f"{TAB}refreshPolicy", f"{TAB * 2}policyType: basic",
                f"{TAB * 2}rollingWindowGranularity: year", f"{TAB * 2}rollingWindowPeriods: 10",
                f"{TAB * 2}incrementalGranularity: month", f"{TAB * 2}incrementalPeriods: 3",
                *prop_expr("sourceExpression", _m_source(t, target), 2)]
    out += ["", annotation("source_table", t.source, 1), annotation("table_kind", t.kind, 1)]
    return join(out)


def render_measures(dm: DomainModel) -> str:
    out = ["/// Every measure is generated from dbt/models/metrics.yml (annotation dbt_metric).",
           f"table {name(spec.MEASURES)}"]
    for m in dm.measures:
        out += ["", *description(m.description, 1), *prop_expr(f"measure {name(m.name)}", m.dax, 1),
                f"{TAB * 2}formatString: {FORMATS[m.format]}", f"{TAB * 2}displayFolder: {m.folder}",
                annotation("dbt_metric", m.metric, 2)]
        if m.owner:
            out.append(annotation("metric_owner", m.owner, 2))
        out.append(annotation("certified", "true" if m.certified else "false", 2))
    out += ["", f"{TAB}column Value", f"{TAB * 2}dataType: string", f"{TAB * 2}isHidden",
            f"{TAB * 2}sourceColumn: [Value]", "",
            f"{TAB}partition {name(spec.MEASURES)} = calculated", f"{TAB * 2}mode: import",
            f'{TAB * 2}source = ROW("Value", BLANK())']
    return join(out)


def render_calc_group() -> str:
    out = ["/// The ONE time-intelligence calculation group. Measures never carry MTD/QTD/YTD/PY copies.",
           "/// Filters Posting Period only; capped at the last CLOSED period (Reporting Date), never the open month.",
           f"table {name(spec.CALC_GROUP)}", f"{TAB}calculationGroup", f"{TAB * 2}precedence: 10"]
    for i, (item, dax, fmt) in enumerate(CALC_ITEMS):
        out += ["", *prop_expr(f"calculationItem {name(item)}", dax, 2), f"{TAB * 3}ordinal: {i}"]
        if fmt:
            out.append(f"{TAB * 3}formatStringDefinition = {fmt}")
    out += ["", f"{TAB}column {name('Time Calculation')}", f"{TAB * 2}dataType: string",
            f"{TAB * 2}summarizeBy: none", f"{TAB * 2}sourceColumn: Name", f"{TAB * 2}sortByColumn: Ordinal",
            "", f"{TAB}column Ordinal", f"{TAB * 2}dataType: int64", f"{TAB * 2}isHidden",
            f"{TAB * 2}summarizeBy: none", f"{TAB * 2}sourceColumn: Ordinal"]
    return join(out)


def render_drill(dm: DomainModel) -> str:
    return render_field_parameter(
        spec.DRILL, dm.drill, "Field parameter: pick the drill level (use it twice for a 1st / 2nd level matrix).")


def render_field_parameter(n: str, fields: list[tuple[str, str, str]], doc: str) -> str:
    """A field parameter table ``n``: one row per (label, table, column), in order."""
    rows = ",\n".join(f'    ("{label}", NAMEOF({col(t, c)}), {i})' for i, (label, t, c) in enumerate(fields))
    out = [f"/// {doc}",
           f"table {name(n)}",
           "", f"{TAB}column {name(n)}", f"{TAB * 2}dataType: string", f"{TAB * 2}summarizeBy: none",
           f"{TAB * 2}sourceColumn: [Value1]", f"{TAB * 2}sortByColumn: {name(n + ' Order')}",
           "", f"{TAB * 2}relatedColumnDetails", f"{TAB * 3}groupByColumn: {name(n + ' Fields')}",
           "", f"{TAB}column {name(n + ' Fields')}", f"{TAB * 2}dataType: string", f"{TAB * 2}isHidden",
           f"{TAB * 2}summarizeBy: none", f"{TAB * 2}sourceColumn: [Value2]", "",
           f"{TAB * 2}extendedProperty ParameterMetadata =", f"{TAB * 4}{{\"version\": 3, \"kind\": 2}}",
           "", f"{TAB}column {name(n + ' Order')}", f"{TAB * 2}dataType: int64", f"{TAB * 2}isHidden",
           f"{TAB * 2}summarizeBy: none", f"{TAB * 2}sourceColumn: [Value3]",
           "", f"{TAB}partition {name(n)} = calculated", f"{TAB * 2}mode: import",
           *prop_expr("source", "{\n" + rows + "\n}", 2), "", annotation("PBI_FieldParameter", "true", 1)]
    return join(out)


def render_payee_access(dm: DomainModel, columns: Columns) -> str:
    """Calculated bridge account holder -> dealer, tenant. Computed at refresh (no RLS), never related."""
    n = spec.PAYEE_ACCESS
    parts = [f'SELECTCOLUMNS({q(f)}, "Account Holder Key", {col(f, c)}, '
             f'"Dealer Key", {col(f, "dealer_key")}, "Tenant Key", {col(f, "tenant_key")})'
             for f, c in dm.payee_sources]
    sep = ",\n    "
    expr = f"DISTINCT(UNION(\n    {sep.join(parts)}))" if len(parts) > 1 else f"DISTINCT({parts[0]})"
    out = ["/// Which dealers paid each account holder (payee). Hidden; Dealer User RLS reads it.",
           f"table {name(n)}", f"{TAB}isHidden"]
    # Column types come from the source fact columns (the keys are VARCHAR hashes, tenant_key BIGINT),
    # so the role filters compare string with string.
    f0, c0 = dm.payee_sources[0]
    src = dict(columns.get(spec.FACTS[f0].source, []))
    types = {"Account Holder Key": src.get(c0), "Dealer Key": src.get("dealer_key"),
             "Tenant Key": src.get("tenant_key")}
    for f, c in dm.payee_sources[1:]:
        other = dict(columns.get(spec.FACTS[f].source, []))
        if spec.tmdl_type(other.get(c, "")) != spec.tmdl_type(types["Account Holder Key"] or ""):
            raise TranslationError(f"{spec.PAYEE_ACCESS}: {f}.{c} type differs from {f0}.{c0}")
    for c in ("Account Holder Key", "Dealer Key", "Tenant Key"):
        if not types[c]:
            raise TranslationError(f"{spec.PAYEE_ACCESS}: no source column for {c} in {f0}")
        out += ["", f"{TAB}column {name(c)}", f"{TAB * 2}dataType: {spec.tmdl_type(types[c])}", f"{TAB * 2}isHidden",
                f"{TAB * 2}summarizeBy: none", f"{TAB * 2}sourceColumn: [{c}]"]
    out += ["", f"{TAB}partition {name(n)} = calculated", f"{TAB * 2}mode: import",
            *prop_expr("source", expr, 2),
            "", annotation("table_kind", "security", 1)]
    return join(out)


def _secured_tables(dm: DomainModel) -> set[str]:
    return set(dm.dims) | set(dm.facts) | ({spec.PAYEE_ACCESS} if dm.payee_sources else set())


def render_role(role: str, dm: DomainModel, extra: dict[str, str] | None = None) -> str:
    """``extra``: more table filters (a reporting model hides its internal-only tables from external roles)."""
    out = [*description(ROLE_NOTES[role], 0), f"role {name(role)}", f"{TAB}modelPermission: read"]
    for table, expr in {**role_filters(role, _secured_tables(dm)), **(extra or {})}.items():
        out += ["", *prop_expr(f"tablePermission {name(table)}", expr, 1)]
    out += ["", annotation("security_group", ROLE_GROUPS[role].replace("<domain>", dm.domain.lower()), 1)]
    return join(out)


def render_relationships(dm: DomainModel) -> str:
    out = []
    for f, fc, t, tc in dm.relationships:
        out += [f"relationship {name(f'{f}.{fc} -> {t}')}",
                f"{TAB}fromColumn: {name(f)}.{name(friendly(fc))}",
                f"{TAB}toColumn: {name(t)}.{name(friendly(tc))}", ""]
    for f, fc, t, tc in dm.inactive:  # role-playing dates: measures turn them on with USERELATIONSHIP
        out += [f"relationship {name(f'{f}.{fc} -> {t}')}", f"{TAB}isActive: false",
                f"{TAB}fromColumn: {name(f)}.{name(friendly(fc))}",
                f"{TAB}toColumn: {name(t)}.{name(friendly(tc))}", ""]
    return join(out)


def render_expressions(target: str = DEFAULT_TARGET) -> str:
    if target == "postgres":
        pg_params = [
            ("PostgresServer", '"localhost:5432"', "Text"),
            ("PostgresDatabase", '"vsc_analytics"', "Text"),
            ("RangeStart", "#datetime(2019, 1, 1, 0, 0, 0)", "DateTime"),
            ("RangeEnd", "#datetime(2030, 1, 1, 0, 0, 0)", "DateTime"),
        ]
        out = ["/// Local PostgreSQL server filled by python -m warehouse.postgres sync. Import mode; sign in with",
               "/// Database authentication as the read-only bi_reader login. Change PostgresServer / PostgresDatabase",
               "/// to point the model at another server."]
        for n, v, typ in pg_params:
            out += [f'expression {n} = {v} meta [IsParameterQuery = true, Type = "{typ}", '
                    f'IsParameterQueryRequired = true]', ""]
        return join(out)
    params = [
        ("SnowflakeServer", '"your-account.snowflakecomputing.com"', "Text"),
        ("SnowflakeWarehouse", '"REPORTING"', "Text"),
        ("AnalyticsDatabase", '"ANALYTICS"', "Text"),
        ("RangeStart", "#datetime(2019, 1, 1, 0, 0, 0)", "DateTime"),
        ("RangeEnd", "#datetime(2030, 1, 1, 0, 0, 0)", "DateTime"),
    ]
    out = ["/// Deployment-pipeline parameters: Dev binds AnalyticsDatabase = ANALYTICS_DEV, Prod = ANALYTICS."]
    for n, v, typ in params:
        out += [f'expression {n} = {v} meta [IsParameterQuery = true, Type = "{typ}", '
                f'IsParameterQueryRequired = true]', ""]
    return join(out)


def render_model(dm: DomainModel) -> str:
    tables = [spec.MEASURES, spec.CALC_GROUP, spec.DRILL, *dm.facts, *dm.dims]
    if dm.payee_sources:
        tables.append(spec.PAYEE_ACCESS)
    if dm.purpose:
        head = f"/// {dm.domain} reporting model: {dm.purpose}"
    else:
        head = f"/// {dm.domain} domain model: {spec.DOMAIN_PURPOSE[dm.domain]}"
    out = [head, "model Model",
           f"{TAB}culture: en-US", f"{TAB}defaultPowerBIDataSourceVersion: powerBI_V3",
           f"{TAB}discourageImplicitMeasures", f"{TAB}sourceQueryCulture: en-US",
           annotation("domain", dm.domain, 1),
           annotation("generated_by", "python -m semantic (from dbt/models/metrics.yml)", 1),
           annotation("storage_mode", "import; incremental refresh on large facts (semantic/README.md)", 1), ""]
    out += [f"ref table {name(t)}" for t in tables]
    out += [""] + [f"ref role {name(r)}" for r in dm.roles]
    return join(out)


def _safe(n: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in n).strip()


def write_domain(dm: DomainModel, columns: Columns, base: Path, put, target: str = DEFAULT_TARGET,
                 role_extra: dict[str, str] | None = None) -> None:
    """Write one ``<name>.SemanticModel`` folder; ``put(path, text)`` does the writing."""
    d = dm.domain
    put(base / "definition.pbism", json.dumps({"version": "4.0", "settings": {}}, indent=2) + "\n")
    defn = base / "definition"
    put(defn / "database.tmdl", join([f"database {d}", f"{TAB}compatibilityLevel: 1601"]))
    put(defn / "model.tmdl", render_model(dm))
    put(defn / "expressions.tmdl", render_expressions(target))
    put(defn / "relationships.tmdl", render_relationships(dm))
    put(defn / "tables" / f"{spec.MEASURES}.tmdl", render_measures(dm))
    put(defn / "tables" / f"{spec.CALC_GROUP}.tmdl", render_calc_group())
    put(defn / "tables" / f"{spec.DRILL}.tmdl", render_drill(dm))
    for t in dm.facts:
        put(defn / "tables" / f"{_safe(t)}.tmdl", render_table(spec.FACTS[t], columns, target))
    for t in dm.dims:
        put(defn / "tables" / f"{_safe(t)}.tmdl", render_table(spec.DIMS[t], columns, target))
    if dm.payee_sources:
        put(defn / "tables" / f"{_safe(spec.PAYEE_ACCESS)}.tmdl", render_payee_access(dm, columns))
    for r in dm.roles:
        put(defn / "roles" / f"{_safe(r)}.tmdl", render_role(r, dm, role_extra if r != "Internal" else None))


def write_model(plan: Plan, columns: Columns, out_dir: Path, target: str = DEFAULT_TARGET) -> list[Path]:
    """(Re)write ``out_dir`` from the plan. Returns every file written."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    written: list[Path] = []

    def put(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)

    for d, dm in plan.models.items():
        write_domain(dm, columns, out_dir / f"{d}.SemanticModel", put, target)
    doc = manifest(plan)
    if target != DEFAULT_TARGET:
        doc["source_target"] = target
    put(out_dir / "manifest.json", json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return written


def manifest(plan: Plan) -> dict:
    return {
        "generated_from": "dbt/models/metrics.yml",
        "metrics_sha256": plan.metrics_sha256,
        "calculation_group": {"name": spec.CALC_GROUP, "items": [i for i, _, _ in CALC_ITEMS]},
        "measures": {m.metric: {"measure": m.name, "folder": m.folder, "format": m.format, "dax": m.dax,
                                "tables": list(m.tables), "owner": m.owner, "certified": m.certified}
                     for m in plan.measures.values()},
        "time_variants": plan.time_variants,
        "not_exported": plan.not_exported,
        "domains": {
            d: {"purpose": spec.DOMAIN_PURPOSE[d], "measures": [m.metric for m in dm.measures], "facts": dm.facts,
                "dims": dm.dims, "roles": list(dm.roles), "relationships": len(dm.relationships),
                "inactive_relationships": len(dm.inactive),
                "drill_levels": [label for label, _, _ in dm.drill],
                "incremental_tables": [f for f in dm.facts if spec.FACTS[f].incremental]}
            for d, dm in plan.models.items()
        },
    }


def generate(out_dir: Path = OUT_DIR, metrics_path: Path = METRICS_PATH, columns: Columns | None = None,
             target: str = DEFAULT_TARGET) -> Plan:
    if target not in TARGETS:
        raise ValueError(f"unknown semantic target {target!r}; choose from {TARGETS}")
    text = metrics_path.read_text(encoding="utf-8")
    catalog = Catalog.load(metrics_path)
    columns = load_lock() if columns is None else columns
    plan = make_plan(catalog, columns, text)
    write_model(plan, columns, out_dir, target)
    return plan


def drift(lock: Columns, live: Columns, plan: Plan) -> tuple[list[str], list[str]]:
    """(errors, warnings) comparing the column lock with the live warehouse."""
    errors, warnings = [], []
    used_tables = {spec.ALL_TABLES[t].source for dm in plan.models.values() for t in dm.facts + dm.dims}
    for src in sorted(used_tables):
        locked, now = dict(lock.get(src, [])), dict(live.get(src, []))
        if not now:
            errors.append(f"{src}: missing from the warehouse")
            continue
        gone = sorted(set(locked) - set(now))
        new = sorted(set(now) - set(locked))
        retyped = sorted(c for c in set(locked) & set(now) if spec.tmdl_type(locked[c]) != spec.tmdl_type(now[c]))
        if gone or retyped:
            errors.append(f"{src}: columns removed {gone} or retyped {retyped}")
        if new:
            warnings.append(f"{src}: new columns not in the lock {new}")
    return errors, warnings
