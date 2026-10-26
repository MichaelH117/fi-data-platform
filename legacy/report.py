"""Turn the benchmark JSON into the pain-point catalog: ``docs/LEGACY_BENCHMARK.md`` and the site page data.

One entry per legacy pain point (docs/V3_DESIGN.md 8.2): what it was, the measured impact (numbers read
from the last ``python -m legacy demo`` run, never typed in), the root cause in the foundation, the fix
and where it lives. ``reports/legacy`` renders the same entries as the "Legacy vs better" site page.

The last measured JSON is also kept at ``reports/legacy/benchmark_snapshot.json`` (committed), so the
doc and the page still render, with their run date, where no PostgreSQL server exists (CI).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from legacy import ROOT, STATE_DIR

DOC = ROOT / "docs" / "LEGACY_BENCHMARK.md"
SNAPSHOT = ROOT / "reports" / "legacy" / "benchmark_snapshot.json"
STATE_JSON = STATE_DIR / "legacy_benchmark.json"


# --------------------------------------------------------------------------- reading the JSON
class Result:
    """Convenience accessors over one benchmark JSON."""

    def __init__(self, data: dict):
        self.data = data
        self.q = {q["id"]: q for q in data.get("questions", [])}

    def has(self, qid: str) -> bool:
        return qid in self.q

    def side(self, qid: str, side: str = "legacy") -> dict:
        return self.q.get(qid, {}).get(side, {})

    def step(self, qid: str, n: int) -> dict:
        """Legacy step n (1-based); a single-file question has only step 1."""
        q = self.q.get(qid, {})
        steps = q.get("legacy_steps") or [q.get("legacy", {})]
        return steps[n - 1] if 0 < n <= len(steps) else {}

    def val(self, qid: str, col: str, side: str = "legacy", step: int | None = None):
        r = self.step(qid, step) if step else self.side(qid, side)
        cols, row = r.get("columns") or [], r.get("first_row") or []
        return row[cols.index(col)] if col in cols and len(row) > cols.index(col) else None

    def ms(self, qid: str, side: str = "legacy", step: int | None = None) -> float | None:
        r = self.step(qid, step) if step else self.side(qid, side)
        return r.get("ms")


def load(path: Path | None = None) -> dict | None:
    """The newest benchmark JSON: an explicit path, else the local state copy, else the committed snapshot."""
    for p in ([path] if path else [STATE_JSON, SNAPSHOT]):
        if p and p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


# --------------------------------------------------------------------------- formatting
def n(v, digits: int = 0) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):,.{digits}f}"


def money(v) -> str:
    return "n/a" if v is None else f"${float(v):,.0f}"


def ms(v) -> str:
    if v is None:
        return "n/a"
    return f"{v / 1000:,.1f} s" if v >= 1000 else f"{v:,.0f} ms"


def timing(r: dict) -> str:
    """'timed out at 60 s' or the elapsed time."""
    if r.get("timed_out"):
        return r.get("note") or f"timed out at {r.get('timeout')}"
    return ms(r.get("ms"))


def scans(r: dict) -> str:
    k = r.get("plan_scans") or {}
    return ", ".join(f"{v} {t}" for t, v in sorted(k.items(), key=lambda kv: -kv[1])) or "n/a"


# --------------------------------------------------------------------------- the catalog
@dataclass(frozen=True)
class PainPoint:
    num: int
    title: str
    what: str
    root_cause: str
    fix: str
    where: str
    questions: tuple[str, ...]
    failure: str  # lost | doubled | zeroed | sign | stale | timeout/slow | skewed | extra step | clutter | sprawl
    measured_by: str = ""  # only for entries with no benchmark question (default: the schema-registry check)


def _impact_1(r: Result) -> str:
    leg, mod = r.side("q07"), r.side("q07", "modern")
    spine = next((x for x in leg.get("plan_top", []) if x.startswith("Append")), "")
    m = re.search(r"rows=(\d[\d,]*\d|\d)", spine)
    read = f" The plan appends {m.group(1)} spine rows to answer it." if m else ""
    ratio = (f" ({leg['ms'] / mod['ms']:,.0f}x slower)" if leg.get("ms") and mod.get("ms")
             and not leg.get("timed_out") else "")
    who = "" if leg.get("timed_out") else f" of {n(r.val('q07', 'contracts'))} contracts"
    return (f"A provider-scoped lookup{who} took {timing(leg)} on the union-spine view against a "
            f"{leg.get('timeout', '?')} interactive budget, vs {timing(mod)} on the modern contract table{ratio}."
            f"{read} The cost grows with the whole book, not with the population asked for.")


def _impact_2(r: Result) -> str:
    s1, s2, mod = r.step("q10", 1), r.step("q10", 2), r.side("q10", "modern")
    return (f"The text-normalized correlated VIN lookup {timing(s1)}. The fast exact-text join matched "
            f"{n(r.val('q10', 'match_rate_pct', step=2), 2)}% of {n(r.val('q10', 'stock_rows', step=2))} stock rows "
            f"({n(r.val('q10', 'unmatched_rows', step=2))} lost) in {timing(s2)}. The modern key join finds "
            f"{n(r.val('q10', 'match_rate_pct', 'modern'), 2)}% of contracts' vehicles in {timing(mod)}.")


def _impact_3(r: Result) -> str:
    one, allid = r.val("q01", "in_force_contracts"), r.val("q01", "in_force_all_identities")
    lost = (allid - one) if one is not None and allid is not None else None
    share = f" ({100 * lost / allid:.0f}% of the book)" if lost is not None and allid else ""
    return (f"Filtering dealer id 37 found {n(one)} in-force contracts; the same dealer under its "
            f"{n(r.val('q01', 'dealer_ids_for_one_dealer'))} ids holds {n(allid)}, so {n(lost)} were lost{share}. "
            f"Coverage cost shown: {money(r.val('q01', 'coverage_cost'))} vs "
            f"{money(r.val('q01', 'coverage_cost', 'modern'))} on the modern dealer key "
            f"({n(r.val('q01', 'in_force_contracts', 'modern'))} in-force contracts).")


def _impact_4(r: Result) -> str:
    got, book = r.val("q02", "contracts_counted"), r.val("q02", "contracts_on_book")
    lost = book - got if got is not None and book is not None else None
    return (f"The inner join to the stale product dim kept {n(got)} of {n(book)} contracts ({n(lost)} lost, "
            f"{100 * lost / book:.1f}%) and reported {r.val('q02', 'latest_plan_year_seen')} as the latest plan year. "
            f"Modern: {n(r.val('q02', 'contracts_counted', 'modern'))} reportable contracts, latest plan year "
            f"{r.val('q02', 'latest_plan_year_seen', 'modern')}." if lost is not None and book else "not measured")


def _impact_5(r: Result) -> str:
    return (f"Appending the ops layer ({money(r.val('q03', 'ops_layer'))}) and the reporting layer "
            f"({money(r.val('q03', 'reporting_layer'))}) gave {money(r.val('q03', 'paid_claims_appended_layers'))} "
            f"of paid claims over {n(r.val('q03', 'months'))} months. The modern claim fact says "
            f"{money(r.val('q03', 'paid_claims', 'modern'))}.")


def _impact_6(r: Result) -> str:
    return (f"Filtering the ops \"Producers\" column by the provider's name returned "
            f"{n(r.val('q04', 'contracts'))} contracts; the right column holds {n(r.val('q04', 'right_column'))}. "
            f"Modern provider-entity key: {n(r.val('q04', 'contracts', 'modern'))}; the gap to the right legacy "
            "column is the converted book, which carries no provider id in the legacy layer at all.")


def _impact_7(r: Result) -> str:
    leg, mod = r.side("q05"), r.side("q05", "modern")
    return (f"Entered-year 2019 loss ratio: {n(r.val('q05', 'loss_ratio_pct'), 2)}% with the date-range filter, "
            f"{n(r.val('q05', 'loss_ratio_with_sentinel_rows'), 2)}% once the "
            f"{money(r.val('q05', 'earned_on_sentinel_date'))} earned on the sentinel date is put back. "
            f"Legacy plan: {scans(leg)} ({timing(leg)}); modern: {scans(mod)} ({timing(mod)}), "
            f"loss ratio {n(r.val('q05', 'loss_ratio_pct', 'modern'), 2)}% on the modern earning pattern."
            + (" At this scale the legacy query is the faster one: its cohort filter reaches the earnings rows "
               "through the contract-id index, while the modern fact is scanned in parallel. The date index pays "
               "off on date-range questions; the win here is the correct earned base, not speed."
               if (mod.get("ms") or 0) > (leg.get("ms") or 0) else ""))


def _impact_8(r: Result) -> str:
    return (f"The filing pulled {n(r.val('q06', 'filing_rows'))} rows for {n(r.val('q06', 'distinct_contracts'))} "
            f"contracts: {n(r.val('q06', 'rows_doubled_by_fanout'))} rows doubled by the rep join, "
            f"{n(r.val('q06', 'test_contracts'))} API test contracts and {n(r.val('q06', 'demo_dealer_contracts'))} "
            f"demo-dealer contracts included. Modern reportable population: "
            f"{n(r.val('q06', 'filing_rows', 'modern'))} rows, one per contract.")


def _impact_9(r: Result) -> str:
    s1 = r.step("q08", 1)
    err = s1.get("error_text") or s1.get("error") or "n/a"
    return (f"The main-schema query failed on the mirror ({err}). Rewritten, the mirror shows "
            f"{n(r.val('q08', 'in_force_contracts'))} in-force contracts "
            f"(newest {r.val('q08', 'newest_contract_in_mirror')})"
            f" vs {n(r.val('q08', 'in_force_contracts', 'modern'))} live (newest "
            f"{r.val('q08', 'newest_contract', 'modern')}).")


def _impact_10(r: Result) -> str:
    c = r.data.get("clutter") or {}
    leg, mod = c.get("legacy") or {}, c.get("modern") or {}
    if not leg:
        return "not measured (run python -m legacy demo)"
    rules = ", ".join(f"{k} {v}" for k, v in sorted(leg.get("by_rule", {}).items()))
    return (f"The schema-registry check finds {n(leg.get('findings'))} findings in the legacy database ({rules}); "
            f"{n(mod.get('findings'))} in the modern serving database.")


def _impact_11(r: Result) -> str:
    s1, s2 = r.step("q09", 1), r.step("q09", 2)
    err = s1.get("error_text") or s1.get("error") or "n/a"
    return (f"The one-query join failed ({err}). Step two, the pivot in the admin database, found "
            f"{n(r.val('q09', 'contracts_with_chassis_make', step=2))} contracts with a chassis make "
            f"({n(r.val('q09', 'contracts_missing_a_field', step=2))} missing a field) in {timing(s2)}, and still "
            f"has to be exported and re-imported to meet the contract attributes. Modern: one query, "
            f"{n(r.val('q09', 'contracts_with_chassis_make', 'modern'))} reportable contracts, "
            f"{timing(r.side('q09', 'modern'))}.")


def _impact_12(r: Result) -> str:
    # Structure only: the BI service tenant is not rebuilt here, so there is no query and no number to read.
    return ("Not measured by a query: the BI service itself is not rebuilt in this repo. Each copy is one more "
            "place where the same number can disagree; a fix made in one generation copy or client workspace does "
            "not reach the others; an item owned by a person has no owner after that person leaves; and with no "
            "endorsement or sensitivity label, a reader cannot tell the certified model from a test copy.")


PAIN_POINTS: tuple[PainPoint, ...] = (
    PainPoint(1, "One semantic model per source schema, and a contract \"dim\" that is a query",
              "Three source-named schemas each backed their own model; finance carried _gen2/_gen3 copies that "
              "disagreed mid-migration; the reporting contract dim was a UNION ALL spine of ids over four facts "
              "with a query-time DISTINCT and a full coverage-cost aggregate.",
              "Schemas were organized by where data came from, not by business entity, and the dim was never "
              "materialized, so no filter could be pushed down.",
              "One contract dim table with a declared primary key; one shared conformed semantic model feeding "
              "domain models and thin reports.",
              "dbt/models/marts/core/dim_contract.sql, _core_keys.yml; semantic/ (docs/SEMANTIC_STRATEGY.md)",
              ("q07",), "timeout"),
    PainPoint(2, "No primary or foreign keys; joins on normalized text",
              "No PK, FK or comment anywhere. Tables were held together by joins on upper(trim(VIN)) and on the "
              "dealer number instead of an id.",
              "Keys were never modeled, so every consumer re-invented the join (and its normalization).",
              "Surrogate keys, enforced PK/FK in Postgres, dbt contracts and a relationships test on every key; "
              "VINs normalized and hashed once in staging.",
              "dbt/models/marts/core/_core_keys.yml; warehouse/postgres/sync.py (FKs NOT VALID then VALIDATE)",
              ("q10",), "lost"),
    PainPoint(3, "Duplicate dealer identities",
              "One dealer existed as an active row, an inactive upper-case copy and a prefixed re-onboarding, each "
              "with its own id; contracts landed on all three.",
              "No dealer master: onboarding created new ids and nothing merged them.",
              "Survivorship merges every identity into one dealer_key; a crosswalk maps each source id to it.",
              "dbt/models/intermediate/int_dealer_survivorship.sql, marts/core/dim_dealer_crosswalk.sql",
              ("q01",), "lost"),
    PainPoint(4, "A stale product dimension",
              "The product table stopped refreshing after one plan year, and the house pattern inner-joined to it.",
              "Dims loaded by a separate job with no freshness check, and joins that drop orphans silently.",
              "A complete dim_product, an unknown member (-1) on every dim, and a build test that fails when more "
              "than 2% of a fact's rows land on -1.",
              "dbt/models/marts/core/dim_product.sql; dbt/tests/core/core_unknown_member_share.sql",
              ("q02",), "lost"),
    PainPoint(5, "Claim sign differs by layer",
              "The ops layer showed claim amounts negative, the reporting layer positive.",
              "Each layer applied its own presentation rule; no sign convention at the source boundary.",
              "The sign is fixed once in staging (paid amounts positive everywhere) with a >= 0 test.",
              "dbt/models/staging/legacy_admin/ (claim staging); gold.fct_claim.is_amount_sign_fixed",
              ("q03",), "sign"),
    PainPoint(6, "Two \"producer\" columns that mean different things",
              "\"Producers\" in ops is the selling dealer's name; \"Producer\" in reporting is the provider entity.",
              "Column names copied from source screens, with no role attached.",
              "dim_producer with producer_role (selling_dealer | provider_entity); the contract dim carries "
              "selling_dealer_key and provider_entity_key.",
              "dbt/models/marts/core/dim_producer.sql, dim_contract.sql",
              ("q04",), "zeroed"),
    PainPoint(7, "A huge earnings series with no date index and a sentinel date",
              "Millions of monthly earnings rows keyed by an int YYYYMMDD with an index only on contract id; "
              "converted contracts carried 19000101 for pre-conversion months.",
              "The date was stored as a code, not a date, and placeholder values were never cleaned at the source.",
              "Sentinel dates mapped to NULL plus an is_sentinel_date flag once in staging; a BRIN index on "
              "month_start for the big facts (loaded in date order).",
              "dbt/macros/foundations.sql; warehouse/postgres/sync.py (BRIN on month_start)",
              ("q05",), "skewed"),
    PainPoint(8, "API test contracts and demo dealers in the reportable book",
              "Contracts from an API test harness (customer TEST CUSTOMER, vehicle price 1) and training dealers "
              "sat next to real business; every report had to remember to filter them.",
              "Exclusions lived in each report, not in the data.",
              "Test, API and demo contracts are excluded once, in int_reportable_scope (rules in a seed).",
              "dbt/models/intermediate/int_reportable_scope.sql; dbt/seeds/core/demo_exclusions.csv",
              ("q06",), "doubled"),
    PainPoint(9, "A second tenant as a mirror schema",
              "Tenant B was a full copy of the ops schema, refreshed at an earlier cutoff, with one column renamed "
              "and one dropped.",
              "Multi-tenancy was solved by copying, so every copy drifts and lags.",
              "tenant_key is a column on one set of tables, protected by row-level security.",
              "core.dim_tenant; warehouse/postgres (security.apply_row_policies); semantic RLS roles",
              ("q08",), "stale"),
    PainPoint(10, "Clutter: backups, empty shells, a frozen schema, a personal sandbox, no owners",
              "*_bak_YYYYMMDD tables, empty tbl_*_new shells, a frozen report schema and a personal staging "
              "schema sat next to serving objects with no owner metadata.",
              "Nothing recorded who owned a schema or what it was for, so nothing was ever safe to delete.",
              "A schema registry (owner, tier, purpose, retention) and a CI clutter check that fails on "
              "unregistered schemas, backup names, empty tables and sandbox objects in serving tiers.",
              "pipeline/schema_registry.yml; pipeline/clutter_check.py; .github/workflows/ci.yml",
              (), "clutter"),
    PainPoint(11, "Custom fields in a separate admin database (EAV)",
              "RV chassis fields lived as text key-value rows in the admin system's own database; a blank field "
              "was a missing row.",
              "The admin system's extension mechanism was exposed raw instead of being modeled.",
              "The custom fields are pivoted in staging into typed columns on dim_contract_vehicle.",
              "dbt/models/staging/admin_core/stg_admin_core__contract_custom_field.sql; dim_contract_vehicle.sql",
              ("q09",), "extra step"),
    PainPoint(12, "Workspace sprawl in the BI service",
              "Generation copies of the same reports (Gen1 next to Gen2), DEV, UAT and POC copies living inside "
              "production workspaces, one workspace per client for custom reports, a semantic model per report, "
              "items owned by individual people, no endorsement or sensitivity labels, and refreshes started by "
              "hand.",
              "Publishing was open to anyone, so every new need became a new copy; promotion, ownership and "
              "certification were never part of how an item reached production.",
              "One certified shared model that thin reports connect to live; items reach Test and Prod only through "
              "the deployment pipeline; every item is owned by a security group, not a person; client views are "
              "row-level security on the same model, not a workspace per client.",
              "docs/ACCESS_MATRIX.md (workspaces and apps); docs/SEMANTIC_STRATEGY.md; "
              "docs/operating-model/RELEASE.md; semantic/model/",
              (), "sprawl",
              "a structure review of the BI service (no query: the service is not rebuilt here)"),
)
IMPACT = {1: _impact_1, 2: _impact_2, 3: _impact_3, 4: _impact_4, 5: _impact_5, 6: _impact_6, 7: _impact_7,
          8: _impact_8, 9: _impact_9, 10: _impact_10, 11: _impact_11, 12: _impact_12}


def impact(p: PainPoint, r: Result) -> str:
    if p.questions and not all(r.has(q) for q in p.questions):
        return "not measured in the last run"
    try:
        return IMPACT[p.num](r)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        return "not measured in the last run"


def entries(data: dict) -> list[dict]:
    r = Result(data)
    return [{"num": p.num, "title": p.title, "what": p.what, "impact": impact(p, r), "root_cause": p.root_cause,
             "fix": p.fix, "where": p.where, "questions": list(p.questions), "failure": p.failure,
             "measured_by": p.measured_by}
            for p in PAIN_POINTS]


def question_rows(data: dict) -> list[list[str]]:
    """One row per benchmark question: answers, timings, plan kind, observed failure."""
    rows = []
    for q in data.get("questions", []):
        leg, mod = q["legacy"], q["modern"]
        la = "timed out" if leg.get("timed_out") else n(leg.get("answer"), 2 if leg.get("answer") % 1 else 0) \
            if isinstance(leg.get("answer"), (int, float)) else "n/a"
        ma = n(mod.get("answer"), 2 if mod.get("answer") % 1 else 0) \
            if isinstance(mod.get("answer"), (int, float)) else "n/a"
        steps = q.get("legacy_steps") or [leg]
        rows.append([q["id"].upper(), q["question"], (leg.get("columns") or ["answer"])[0].replace("_", " "), la, ma,
                     " + ".join(timing(s) for s in steps), timing(mod), q["observed_failure"]])
    return rows


# --------------------------------------------------------------------------- the markdown doc
def markdown(data: dict) -> str:
    r = Result(data)
    lines = [
        "# Legacy benchmark: how it was, and how we fixed it",
        "",
        "<!-- generated by `python -m legacy demo` (legacy/report.py); edit the catalog there, not here -->",
        "",
        f"Measured {data.get('generated_at', '?')} on one local PostgreSQL 16 server: the legacy databases "
        f"`{data.get('legacy_database')}` / `{data.get('legacy_admin_database', 'vsc_legacy_admin')}` against the "
        f"modern marts in `{data.get('modern_database')}`. Both sides are built from the same synthetic raw rows, so "
        "every difference below comes from structure, not data. All data is synthetic.",
        "",
        "How to read it: each question is answered the way the old warehouse forced an analyst to answer it "
        "(`sql/playbook/benchmark/qNN_legacy*.sql`) and the way the modern star schema answers it "
        "(`qNN_modern.sql`). Timings are the faster of two runs for quick queries. Plans are "
        f"`EXPLAIN (ANALYZE, BUFFERS)` for queries that finished under {n(data.get('analyze_max_s', 60))} s and "
        "plain `EXPLAIN` for anything slower or timed out, so nothing ran past its budget. Rerun with "
        "`python -m legacy demo`; with PostgreSQL down it exits 2 and this file keeps the last measured numbers.",
        "",
        "## The questions",
        "",
        "| Q | Business question | Headline | Legacy | Modern | Legacy time | Modern time | Failure |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    lines += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in question_rows(data)]
    if data.get("skipped"):
        lines += ["", f"Skipped (no SQL pair yet): {', '.join(data['skipped'])}."]
    lines += ["", "## Pain points", ""]
    for e in entries(data):
        lines += [f"### {e['num']}. {e['title']}", "",
                  f"- **What it was:** {e['what']}",
                  f"- **Measured impact:** {e['impact']}",
                  f"- **Root cause in the foundation:** {e['root_cause']}",
                  f"- **The fix:** {e['fix']}",
                  "- **Where it lives:** " + "; ".join(f"`{w.strip()}`" for w in e["where"].split(";")),
                  (f"- **Measured by:** {', '.join(q.upper() for q in e['questions'])}" if e["questions"]
                   else f"- **Measured by:** {e['measured_by']}" if e["measured_by"]
                   else "- **Measured by:** the schema-registry check (`python -m pipeline.clutter_check --postgres`)"),
                  ""]
    lines += ["## Plans", "", "Top plan nodes per question (legacy, then modern).", ""]
    for q in data.get("questions", []):
        lines.append(f"**{q['id'].upper()} {q['question']}**")
        lines.append("")
        for label, s in [*(((f"legacy step {i}", st) for i, st in enumerate(q.get('legacy_steps', []), 1))
                           if q.get("legacy_steps") else [("legacy", q["legacy"])]), ("modern", q["modern"])]:
            if s.get("error_text"):
                body = f"expected error: {s['error_text']}"
            elif s.get("error"):
                body = f"error: {s['error']}"
            else:
                body = " > ".join(s.get("plan_top") or []) or "no plan"
            lines.append(f"- {label} (`{s.get('file', '?')}`, {s.get('plan_kind') or 'no plan'}, {timing(s)}): "
                         f"{body}")
        lines.append("")
    c = data.get("clutter") or {}
    if c.get("legacy"):
        lines += ["## Clutter findings (legacy database)", ""]
        lines += [f"- {f}" for f in c["legacy"].get("items", [])]
        lines.append("")
    lines += ["## Not measured here", "",
              "- Timings are at synthetic scale (about 60k contracts, 3.3M earnings rows). The legacy costs grow "
              "with the whole book, so the gaps widen with real volumes; the row-level differences do not depend "
              "on scale.",
              f"- {_note_q7(r)}",
              ""]
    return "\n".join(lines)


def _note_q7(r: Result) -> str:
    leg = r.side("q07")
    if leg.get("timed_out"):
        return f"Q7 hit its {leg.get('timeout')} interactive budget and was cancelled, so its plan is an estimate."
    return (f"Q7 finished inside its {leg.get('timeout', '?')} budget at this scale ({timing(leg)}); the plan shows "
            "the whole spine is rebuilt for every lookup, which is what times out on a production-sized book.")


def write(data: dict, doc: Path = DOC, snapshot: Path = SNAPSHOT) -> list[Path]:
    doc.write_text(markdown(data), encoding="utf-8")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return [doc, snapshot]
