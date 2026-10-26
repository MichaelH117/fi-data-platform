"""HTML pages of the regulatory area, built with the report shell's Page / Section model."""

from __future__ import annotations

import pandas as pd

from reports.build import Kpi, Page, Section, link, money, num
from reports.regulatory.data import (
    COUNT_LINES,
    SUMMARY_COLUMN,
    RegulatoryData,
    filing_lines,
    form_code_coverage,
    line_notes,
    split_notes,
    survey_notes,
)
from reports.regulatory.excel import survey_matrix

INDEX = "regulatory/index.html"
CRUMB = "All regulatory filings"
BASIS = {"COVERAGE_COST": "coverage cost", "RETAIL": "retail"}
MONEY_ITEMS = {"REVENUE_COVERAGE_COST", "REVENUE_PURCHASE_PRICE", "CLAIMS_PAID_AMOUNT"}


def filing_href(state: str) -> str:
    return f"regulatory/funded-reserve-{state.lower()}.html"


def survey_href(state: str) -> str:
    return f"regulatory/market-survey-{state.lower()}.html"


def inforce_href(state: str) -> str:
    return f"regulatory/inforce-list-{state.lower()}.html"


def pct(v: float) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:.0%}"


def _status(final: bool) -> dict:
    return {"text": "Final" if final else "Preliminary", "status": "good" if final else "warning"}


def _default_itd(summary: pd.DataFrame) -> pd.DataFrame:
    return summary[(summary.scope == "ITD") & summary.is_default_basis]


TOTALS_SUB = "Sum of each state's latest filing (period ends vary; default basis per state)"
TOP_UP = "a top-up deposit into the funded reserve for the shortfall"


def shortfall_cell(shortfall: float, final: bool) -> dict:
    """A filed (Final) shortfall reads 'Filed - shortfall'; only an open filing reads as action needed."""
    if shortfall <= 0:
        return {"text": money(shortfall), "status": "good"}
    if final:
        return {"text": f"{money(shortfall)} · Filed – shortfall", "status": "warning"}
    return {"text": f"{money(shortfall)} · Action needed", "status": "critical"}


def shortfall_cause(basis: str, shortfall: float) -> str:
    """Plain-words reason for a state's shortfall; empty when there is none."""
    if shortfall <= 0:
        return ""
    if basis == "RETAIL":
        return ("Basis effect: the requirement is measured on retail price net of claims, but the reserve held "
                "was funded at sale on coverage cost, so a retail-basis state shows a shortfall by design. "
                f"Expected action: {TOP_UP}.")
    return f"Funding gap on the coverage-cost basis the reserve is funded on. Expected action: {TOP_UP}."


def index_page(d: RegulatoryData, files: dict[str, str], inforce: pd.DataFrame | None = None) -> Page:
    m = d.methods
    filed = sorted(d.summary.state.unique())
    skipped = m.loc[~m.has_requirement, "state"].tolist()
    itd = _default_itd(d.summary)
    by_state = itd.groupby("state").agg(
        in_force=("contracts_in_force", "sum"), gross=("gross_consideration", "sum"),
        reserve=("calculated_reserve", "sum"), current=("current_reserve", "sum"),
        shortfall=("reserve_shortfall", "sum"), final=("is_final", "all"), period_end=("period_end", "max"),
        basis=("basis", "first"),
    )
    meth = m.set_index("state")
    rows = []
    for st in filed:
        r, x = by_state.loc[st], meth.loc[st]
        rows.append([
            link(f"{st} · {x.state_name}", filing_href(st)), x.method_code.replace("_", " ").title(),
            f"{x.reserve_rate:.0%}", f"{pd.Timestamp(r.period_end):%b %d, %Y}", _status(bool(r.final)),
            num(r.in_force), money(r.gross), money(r.reserve), money(r.current),
            BASIS[r.basis], shortfall_cell(float(r.shortfall), bool(r.final)),
            {"text": shortfall_cause(r.basis, float(r.shortfall)), "cls": "text wrap"},
            link("Excel", files[f"filing:{st}"]),
        ])
    survey_rows = []
    for st, g in d.survey.groupby("state", sort=True):
        issued = g[g.item == "NEW_ISSUED"].value.sum()
        survey_rows.append([link(f"{st} · {meth.loc[st].state_name}", survey_href(st)),
                            f"{g.calendar_year.min()} to {g.year_label.iloc[g.calendar_year.argmax()]}",
                            num(g.provider_key.nunique()), num(issued), link("Excel", files[f"survey:{st}"])])
    total_short = by_state.shortfall.sum()
    retail = by_state.basis == "RETAIL"
    short = by_state.shortfall > 0
    split = (f"retail-basis states {money(by_state.shortfall[retail].sum())}, "
             f"coverage-cost states {money(by_state.shortfall[~retail].sum())}")
    open_short = bool((short & ~by_state.final).any())
    short_status = "good" if total_short <= 0 else ("critical" if open_short else "warning")
    kpis = [
        Kpi("States filed", num(len(filed)), f"of {len(m)} in the state method matrix"),
        Kpi("States skipped", num(len(skipped)), "no funded-reserve requirement"),
        Kpi("Calculated reserve", money(by_state.reserve.sum()), TOTALS_SUB),
        Kpi("Reserve shortfall", money(total_short),
            f"{TOTALS_SUB}; {split}" + ("; every shortfall filing is Final (filed – shortfall)"
                                         if total_short > 0 and not open_short else ""),
            short_status),
    ]
    basis_rows = []
    for label, mask in (("Retail-basis states", retail), ("Coverage-cost states", ~retail)):
        g = by_state[mask]
        basis_rows.append([label, ", ".join(g.index) or "none", money(g.reserve.sum()), money(g.current.sum()),
                           shortfall_cell(float(g.shortfall.sum()), bool(g.final.all()))])
    sections = [
        Section("Funded-reserve filings by state",
                "One filing per state with a requirement, summed over providers, inception to the end of the "
                "latest regulatory year, on the state's default basis. Open a state for Section 2 and Section 3 "
                "by provider; the Excel file has the Summary plus ITD and Period contract and claim tabs.",
                cols=["State", "Method", "Rate", "Period end", "Status", "In force", "Gross consideration",
                      "Calculated reserve", "Current reserve", "Basis", "Shortfall", "Shortfall cause", "Workbook"],
                rows=rows, wide=True, table_open=True,
                note="Rates are illustrative synthetic values for this portfolio demo, not legal guidance."),
        Section("Shortfall by basis",
                "Current reserve held is the claims reserve funded at sale on coverage cost, minus claims paid. "
                "Coverage-cost states measure the requirement on that same basis. Retail-basis states measure "
                "it on retail price net of claims, which is larger than coverage cost, so they show a shortfall "
                f"even when funding follows the rules: a basis effect, met by {TOP_UP}. Totals add each "
                "state's latest filing, so period ends differ.",
                cols=["Group", "States", "Calculated reserve", "Current reserve", "Shortfall"],
                rows=basis_rows, table_open=True),
        Section("States with no filing requirement",
                "These states are in the method matrix with has_requirement = false, so the filing skips them "
                "on purpose (not because data is missing).",
                cols=["States skipped"], rows=[[", ".join(skipped)]], wide=True, table_open=True),
        Section("State market surveys",
                "Numbered regulator questions by provider and calendar year, for the states that asked.",
                cols=["State", "Years", "Providers", "Contracts issued", "Workbook"], rows=survey_rows,
                table_open=True),
        *([inforce_index_section(d, files, inforce)] if inforce is not None and len(inforce) else []),
        Section("Counting rules",
                "Which statuses count, ITD versus period scope and coverage-cost versus retail basis are seed "
                "tables, tested by dbt, not spreadsheet comments.",
                cols=["Rule tables"], rows=[[link("Report definitions, status sets and state methods",
                                                 "regulatory/report-definitions.html")]], table_open=True),
    ]
    return Page(INDEX, "Regulatory filings",
                f"State funded-reserve / provider renewal filings and state market surveys, as of {d.as_of:%B %d, %Y}.",
                kpis, sections)


def filing_page(d: RegulatoryData, state: str, workbook: str) -> Page:
    s = d.summary[d.summary.state == state]
    x = d.methods.set_index("state").loc[state]
    s2, s3 = filing_lines(d.definitions, 2), filing_lines(d.definitions, 3)
    itd = _default_itd(s)
    per = s[(s.scope == "PERIOD") & s.is_default_basis]
    final = bool(s.is_final.all())
    kpis = [
        Kpi("Contracts in force", num(itd.contracts_in_force.sum()), f"at {itd.period_end.max():%b %d, %Y}"),
        Kpi("Written this regulatory year", num(per.new_contracts_written.sum()), str(per.period_label.iloc[0])),
        Kpi("Calculated reserve", money(itd.calculated_reserve.sum()), f"ITD, {BASIS[x.default_basis]} basis"),
        Kpi("Filing status", "Final" if final else "Preliminary", "closed posting period" if final else
            "period not yet closed", "good" if final else "warning"),
    ]
    shortfall = float(itd.reserve_shortfall.sum())
    sections = []
    if shortfall > 0:
        cell = shortfall_cell(shortfall, final)
        kpis.append(Kpi("Reserve shortfall", money(shortfall),
                        f"ITD, {BASIS[x.default_basis]} basis; " + ("Filed – shortfall" if final else "open filing"),
                        cell["status"]))
        alt = s[(s.scope == "ITD") & ~s.is_default_basis]
        alt_short = float(alt.reserve_shortfall.sum())
        sections.append(Section(
            "Why this filing shows a shortfall",
            shortfall_cause(x.default_basis, shortfall)
            + (" On the coverage-cost basis the same reserve held covers the requirement."
               if x.default_basis == "RETAIL" and alt_short <= 0 else ""),
            cols=["Measure", f"ITD ({BASIS[x.default_basis]}, filed)",
                  "ITD (coverage cost)" if x.default_basis == "RETAIL" else "ITD (retail)"],
            rows=[["Calculated reserve", money(itd.calculated_reserve.sum()), money(alt.calculated_reserve.sum())],
                  ["Current reserve held", money(itd.current_reserve.sum()), money(alt.current_reserve.sum())],
                  ["Shortfall", cell, shortfall_cell(alt_short, final)]],
            table_open=True))
    other = "RETAIL" if x.default_basis == "COVERAGE_COST" else "COVERAGE_COST"
    notes = line_notes(d.definitions)
    fmt = {True: num, False: money}

    def note(item: str) -> dict:
        return {"text": notes.get(item, ""), "cls": "text wrap"}

    for provider, g in s.groupby("provider_name", sort=True):
        def pick(scope: str, basis: str, g=g) -> pd.Series:
            return g[(g.scope == scope) & (g.basis == basis)].iloc[0]

        a, b = pick("ITD", x.default_basis), pick("PERIOD", x.default_basis)
        ao, bo = pick("ITD", other), pick("PERIOD", other)
        sections.append(Section(
            f"Section 2 · Business activity · {provider}",
            "Counts do not depend on the basis. In force is a point-in-time count at the period end. "
            "Lines 2.4 and 2.6 cover all in-scope contracts, so they pair as count and dollars.",
            cols=["Line", "Item", "ITD", "Period", "Note"],
            rows=[[no, label, fmt[i in COUNT_LINES](a[SUMMARY_COLUMN[i]]), fmt[i in COUNT_LINES](b[SUMMARY_COLUMN[i]]),
                   note(i)] for no, i, label in s2],
            wide=True, table_open=True))
        sections.append(Section(
            f"Section 3 · Computation · {provider}",
            f"Both bases side by side; the state default is {BASIS[x.default_basis]}. Line 3.2 is the in-force "
            "block only (a subset of line 2.6), so it is not comparable to the claim count on line 2.4.",
            cols=["Line", "Item", f"ITD ({BASIS[x.default_basis]})", f"Period ({BASIS[x.default_basis]})",
                  f"ITD ({BASIS[other]})", f"Period ({BASIS[other]})", "Note"],
            rows=[[no, label] + [fmt[i in COUNT_LINES](r[SUMMARY_COLUMN[i]]) for r in (a, b, ao, bo)] + [note(i)]
                  for no, i, label in s3],
            wide=True, table_open=True))
        common, diffs = split_notes([a, b, ao, bo])
        sections.append(Section(
            f"Methodology notes · {provider}",
            "The notes column of the filing: what applies to every scope and basis once, then only what "
            "differs by scope or basis.",
            cols=["Applies to", "Note"],
            rows=[["All scopes and bases", {"text": "; ".join(common), "cls": "text wrap"}]]
                 + [[f"{scope} · {BASIS[basis]}", {"text": "; ".join(extra), "cls": "text wrap"}]
                    for scope, basis, extra in diffs],
            wide=True, table_open=True))
    sections.append(Section(
        "Download", "Summary, ITD Contracts, Period Contracts, ITD Claims and Period Claims tabs.",
        cols=["Workbook"], rows=[[link(workbook.rsplit("/", 1)[-1], workbook)]], table_open=True,
        note=str(x.notes)))
    return Page(filing_href(state), f"Funded-reserve filing · {x.state_name}",
                f"Regulatory year {per.period_label.iloc[0]}; ITD runs from inception to the same period end. "
                f"Method: {x.method_code.replace('_', ' ').lower()} at {x.reserve_rate:.0%}.",
                kpis, sections, nav=INDEX, crumb=CRUMB)


def survey_page(d: RegulatoryData, state: str, workbook: str) -> Page:
    s = d.survey[d.survey.state == state]
    name = d.methods.set_index("state").loc[state, "state_name"]
    items = s.drop_duplicates("item_no").set_index("item_no")["item"]
    notes = survey_notes(d.definitions)
    sections = []
    for provider, g in s.groupby("provider_name", sort=True):
        m = survey_matrix(g)
        rows = [[r[0], r[1]] + [(money if items[r[0]] in MONEY_ITEMS else num)(v) for v in r[2:]]
                + [{"text": notes.get(items[r[0]], ""), "cls": "text wrap"}]
                for r in m.itertuples(index=False)]
        sections.append(Section(provider, "Item 1 is a year-end count (Total = latest year); the rest are "
                                          "flows within each calendar year (Total = sum).",
                                cols=[str(c) for c in m.columns] + ["Note"], rows=rows, wide=True, table_open=True))
    sections.append(Section("Download", "Provider tabs plus contract, cancel and claim detail tabs.",
                            cols=["Workbook"], rows=[[link(workbook.rsplit("/", 1)[-1], workbook)]],
                            table_open=True))
    return Page(survey_href(state), f"Market survey · {name}",
                f"Numbered regulator questions by provider and calendar year; the current year is year-to-date "
                f"through the last closed posting period, so open-period activity is not filed. "
                f"As of {d.as_of:%B %d, %Y}.", [], sections, nav=INDEX, crumb=CRUMB)


def definitions_page(d: RegulatoryData) -> Page:
    def blank(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)

    sets = d.status_sets.groupby(["status_set_id", "applies_to"])["status"].apply(", ".join).reset_index()
    defs = [[r.report_id, f"{r.section}.{r.line_no}", r.label, r.measure, blank(r.date_rule), blank(r.status_set_id),
             r.scope, blank(r.basis), blank(r.category_filter), blank(r.subcategory_exclude),
             {"text": blank(r.notes), "cls": "text wrap"}] for r in d.definitions.itertuples()]
    meth = [[r.state, r.state_name, "Yes" if r.has_requirement else "No", r.method_code,
             "" if pd.isna(r.reserve_rate) else f"{r.reserve_rate:.0%}",
             "" if pd.isna(r.security_deposit_rate) else f"{r.security_deposit_rate:.0%}",
             blank(r.default_basis), str(r.period_start_month), "Yes" if r.market_survey else "No"]
            for r in d.methods.itertuples()]
    return Page("regulatory/report-definitions.html", "Regulatory counting rules",
                "The seeds behind every regulatory number: status sets, report line definitions and the "
                "state method matrix. Change a rule here and dbt rebuilds the filings.", [], [
                    Section("Status sets", "seeds/regulatory/report_status_sets.csv",
                            cols=["Status set", "Applies to", "Statuses"],
                            rows=[list(r) for r in sets.itertuples(index=False)], table_open=True),
                    Section("Report line definitions", "seeds/regulatory/report_definitions.csv",
                            cols=["Report", "Line", "Label", "Measure", "Date rule", "Status set", "Scope", "Basis",
                                  "Products", "Excluded", "Notes"], rows=defs, wide=True, table_open=True),
                    Section("State method matrix (50 states + DC)", "seeds/regulatory/state_reserve_method.csv",
                            cols=["State", "Name", "Requirement", "Method", "Reserve rate", "Deposit rate",
                                  "Default basis", "Year starts (month)", "Market survey"],
                            rows=meth, wide=True, table_open=True,
                            note="Illustrative synthetic rules for a portfolio project, not legal guidance."),
                ], nav=INDEX, crumb=CRUMB)


def inforce_index_section(d: RegulatoryData, files: dict[str, str], inforce: pd.DataFrame) -> Section:
    """Index rows: one per state with its in-force count, form-code coverage and the Excel list."""
    names = d.methods.set_index("state")["state_name"]
    rows = []
    for st, g in inforce.groupby("state", sort=True):
        n = int(g.contract_count.sum())
        rows.append([link(f"{st} · {names.get(st, st)}", inforce_href(st)), num(n),
                     pct(g.form_code_source.eq("document").mean()), link("Excel", files[f"inforce:{st}"])])
    as_of = pd.Timestamp(inforce.list_as_of_date.iloc[0])
    return Section("State in-force contract lists",
                   f"Every in-scope service contract in force in a state on {as_of:%b %d, %Y}, for regulators: "
                   "contract date, contract number, product, dealer, lienholder, form code and obligor.",
                   cols=["State", "Contracts in force", "With form code", "Workbook"], rows=rows, table_open=True)


def inforce_page(d: RegulatoryData, state: str, rows: pd.DataFrame, workbook: str) -> Page:
    """One state's in-force list: counts and form-code coverage here; the contract rows are in the Excel."""
    name = d.methods.set_index("state")["state_name"].get(state, state)
    as_of = pd.Timestamp(rows.list_as_of_date.iloc[0])
    n = int(rows.contract_count.sum())
    has = rows.form_code_source.eq("document")
    kpis = [
        Kpi("Contracts in force", num(n), f"on {as_of:%b %d, %Y}"),
        Kpi("With form code", pct(has.mean()), "from the latest contract document"),
        Kpi("Missing form code", num(int((~has).sum())), "no document form code; never inferred",
            "good" if has.all() else "warning"),
    ]
    prod = rows.groupby(["category", "product_name"], dropna=False).contract_count.sum().reset_index()
    cov = form_code_coverage(rows)
    obl = rows.groupby(["obligor_name", "obligor_kind"], dropna=False).contract_count.sum().reset_index()
    sections = [
        Section("Contracts in force by product", "Filing product scope (the same scope as the funded-reserve "
                                                 "filing).", cols=["Category", "Product", "Contracts"],
                rows=[[r.category, r.product_name, num(r.contract_count)] for r in prod.itertuples()],
                table_open=True),
        Section("Form code coverage by contract year",
                "The form code comes from the contract's latest non-deleted contract document. Older contracts "
                "often have none; the list leaves those blank instead of guessing from product and state.",
                cols=["Contract year", "Contracts", "With form code", "Missing", "Share"],
                rows=[[int(r[0]), num(r[1]), num(r[2]), num(r[3]), pct(r[4])]
                      for r in cov.itertuples(index=False)], table_open=True),
        Section("Contracts in force by obligor", "The obligor backing each contract and its kind.",
                cols=["Obligor", "Kind", "Contracts"],
                rows=[[r.obligor_name, "" if pd.isna(r.obligor_kind) else str(r.obligor_kind).replace("_", " "),
                       num(r.contract_count)] for r in obl.itertuples()], table_open=True),
        Section("Download", "In Force Contracts (one row per contract), By Product and Form Code Coverage tabs.",
                cols=["Workbook"], rows=[[link(workbook.rsplit("/", 1)[-1], workbook)]], table_open=True),
    ]
    return Page(inforce_href(state), f"In-force contract list · {name}",
                f"Every in-scope service contract in force in {name} on {as_of:%B %d, %Y}: in-force statuses, "
                "effective on or before the date, not expired or cancelled at that date.",
                kpis, sections, nav=INDEX, crumb=CRUMB)
